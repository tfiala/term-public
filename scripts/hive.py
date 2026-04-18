#!/usr/bin/env python3
"""hive.py - Multi-repo status, pull & create utility for the flow hive.

Discovers all git repos in the hive (parent of current repo's git root)
and reports status or pulls.

Subcommands:
  status       Show branch, sync, and working-tree status for all repos
  pull         Pull --rebase all repos (skips dirty ones) [--push]
  pr-check     Check PR status for repos on non-default branches [--clean]
  issues       List open Forgejo issues for each unique repo
  create       Clone a new repo into the hive with auto-numbered naming
  local        Manage local repo checkouts in .local/
    clone      Clone a repo into .local/ (org/repo format)
    pull       Pull all repos in .local/
  find-tmux-config  Print path to generated tmux config for current hive
  apiary       Manage the apiary (list/add/remove hives)
  shell        Start or attach to a dtach dev shell session
    list       List active dtach sessions
    cleanup    Remove stale sessions

Apiary mode (--apiary):
  Operates across all configured hives defined in ~/.config/hive/apiary.json.
  Implicit for read-only commands (status) when run from outside any hive.

Examples:
  hive.py status
  hive.py pull
  hive.py pull --push
  hive.py --apiary status --compact
  hive.py --apiary pull
  hive.py pull --resolve-branches
  hive.py --apiary pull --resolve-branches
  hive.py pr-check
  hive.py --apiary pr-check --clean
  hive.py issues
  hive.py --apiary issues
  hive.py create
  hive.py create --name-prefix my-project
  hive.py local clone hellenic-flow/corpus
  hive.py local pull
  hive.py find-tmux-config
  hive.py apiary list
  hive.py apiary add ~/src/flow
  hive.py apiary remove ~/src/flow
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse, urlunparse


# --- Color helpers ------------------------------------------------------------


class _Colors:
    """ANSI color codes, auto-disabled when not a TTY."""

    def __init__(self):
        self.enabled = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

    def force_enable(self):
        """Force colors on even when not a TTY."""
        self.enabled = True

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f'\033[{code}m{text}\033[0m'

    def green(self, t: str) -> str:
        return self._wrap('32', t)

    def bright_red(self, t: str) -> str:
        return self._wrap('1;91', t)

    def dim(self, t: str) -> str:
        return self._wrap('2', t)

    def yellow(self, t: str) -> str:
        return self._wrap('33', t)

    def cyan(self, t: str) -> str:
        return self._wrap('36', t)

    def strikethrough(self, t: str) -> str:
        return self._wrap('9', t)


C = _Colors()


def CHECK():
    return C.green('✓')


def CROSS():
    return C.bright_red('✗')

FLOW_APP_CLONE_URL = 'http://git.flow.internal:3000/hellenic-flow/flow-app.git'

_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _visual_len(s: str) -> int:
    """Length of string excluding ANSI escape sequences."""
    return len(_ANSI_RE.sub('', s))


# --- Spinner -----------------------------------------------------------------

_SPINNER_FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'


class _Spinner:
    """Terminal spinner that shows progress on stderr.

    Auto-disabled when stderr is not a TTY (e.g. in tests or piped output).
    """

    def __init__(self):
        self._active = False
        self._thread: threading.Thread | None = None
        self._message = ''
        self._frame = 0
        self._lock = threading.Lock()
        self._enabled = hasattr(sys.stderr, 'isatty') and sys.stderr.isatty()

    def _run(self) -> None:
        while self._active:
            with self._lock:
                msg = self._message
            frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            sys.stderr.write(f'\r\033[K  {C.cyan(frame)} {C.dim(msg)}')
            sys.stderr.flush()
            self._frame += 1
            time.sleep(0.08)

    def update(self, message: str) -> None:
        """Update the spinner message."""
        if not self._enabled:
            return
        with self._lock:
            self._message = message

    def start(self, message: str = '') -> None:
        """Start the spinner with an optional initial message."""
        if not self._enabled:
            return
        self._message = message
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the spinner and clear its line."""
        if not self._enabled:
            return
        self._active = False
        if self._thread:
            self._thread.join()
        sys.stderr.write('\r\033[K')
        sys.stderr.flush()


# --- Git helpers --------------------------------------------------------------


def _git(args: list[str], cwd: str | Path | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess.

    If timeout is given (seconds) and the command exceeds it,
    returns a synthetic CompletedProcess with returncode=-1.
    """
    try:
        return subprocess.run(
            ['git'] + args,
            capture_output=True, text=True, cwd=cwd, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ['git'] + args, returncode=-1, stdout='', stderr='timeout',
        )


def _git_out(args: list[str], cwd: str | Path | None = None) -> str | None:
    """Run a git command, return stripped stdout or None on failure."""
    r = _git(args, cwd=cwd)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# --- Pull cache ---------------------------------------------------------------


class SyncAction(Enum):
    """Possible sync actions for a repo."""

    NONE = 'none'
    PUSH = 'push'
    PULL = 'pull'
    SKIP_DIRTY = 'skip_dirty'
    SKIP_NOT_DEFAULT = 'skip_not_default'
    SKIP_NO_REMOTE = 'skip_no_remote'
    SKIP_NO_BRANCH_ON_REMOTE = 'skip_no_branch_on_remote'
    ERROR = 'error'


@dataclass(frozen=True)
class RemoteProfile:
    """Remote-specific behavior for sync operations."""

    name: str
    pull_args: tuple[str, ...]
    push_enabled: bool = False


@dataclass
class RepoStatus:
    """Structured status for a single repo sync operation."""

    path: Path
    branch: str
    remote_profile: RemoteProfile
    action: SyncAction
    remote_url: str | None = None
    dirty_count: int = 0
    up_to_date: bool = False
    pulled: bool = False
    cached: bool = False
    error_lines: list[str] = field(default_factory=list)
    pushed: bool = False
    push_failed: bool = False

    @property
    def skipped(self) -> bool:
        return self.action == SyncAction.SKIP_DIRTY

    @property
    def pull_failed(self) -> bool:
        return self.action == SyncAction.ERROR


@dataclass
class RemoteCache:
    """Cache remote state for one `hive pull` invocation."""

    remote_shas: dict[str, dict[str, str]] = field(default_factory=dict)
    synced_paths: dict[str, dict[str, Path]] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    local_pulls: int = 0

    def get_remote_sha(self, remote_url: str, branch: str) -> str | None:
        """Get a cached remote SHA, or None when not present."""
        return self.remote_shas.get(remote_url, {}).get(branch)

    def set_remote_sha(self, remote_url: str, branch: str, sha: str) -> None:
        """Cache the remote SHA for a remote URL + branch."""
        if remote_url not in self.remote_shas:
            self.remote_shas[remote_url] = {}
        self.remote_shas[remote_url][branch] = sha

    def get_synced_path(self, remote_url: str, branch: str) -> Path | None:
        """Get a local path known to be synced to the remote."""
        return self.synced_paths.get(remote_url, {}).get(branch)

    def set_synced_path(self, remote_url: str, branch: str, path: Path) -> None:
        """Record that a local path is synced to the remote."""
        if remote_url not in self.synced_paths:
            self.synced_paths[remote_url] = {}
        self.synced_paths[remote_url][branch] = path
        self.local_pulls += 1


def _normalize_origin_url(url: str) -> str:
    """Normalize a git remote URL for cache key deduplication.

    Strips trailing .git and /, and removes userinfo (user@) from HTTPS URLs
    so that https://user@host/repo and https://host/repo match.
    """
    url = url.rstrip('/').removesuffix('.git')
    if url.startswith('https://') or url.startswith('http://'):
        parsed = urlparse(url)
        if parsed.username:
            # Rebuild without userinfo
            netloc = parsed.hostname
            if parsed.port:
                netloc += f':{parsed.port}'
            url = urlunparse((parsed.scheme, netloc, parsed.path,
                              parsed.params, parsed.query, parsed.fragment))
    return url


def _get_origin_url(repo_path: Path) -> str | None:
    """Get the normalized origin remote URL for a repo."""
    url = _git_out(['config', '--get', 'remote.origin.url'], cwd=repo_path)
    if url:
        return _normalize_origin_url(url)
    return None


_FETCH_TIMEOUT = 5  # seconds — LAN/Tailscale remotes should be fast


def _fetch_all_parallel(repos: list[tuple[Path, list[Path]]]) -> None:
    """Fetch all repos (main + nested) in parallel with a per-repo timeout."""
    threads: list[threading.Thread] = []
    for repo_path, nested in repos:
        for p in [repo_path] + nested:
            t = threading.Thread(
                target=_git,
                args=(['fetch', 'origin', '--quiet'],),
                kwargs={'cwd': p, 'timeout': _FETCH_TIMEOUT},
            )
            threads.append(t)
            t.start()
    for t in threads:
        t.join()


def _default_branch(repo_path: Path) -> str:
    """Determine the default branch for a repo.

    Reads origin/HEAD (set by ``git clone`` or ``git remote set-head``).
    Falls back to 'main' if the ref is missing.
    """
    ref = _git_out(['symbolic-ref', 'refs/remotes/origin/HEAD'], cwd=repo_path)
    if ref:
        # 'refs/remotes/origin/infra-dev' → 'infra-dev'
        # 'refs/remotes/origin/release/2026' → 'release/2026'
        _prefix = 'refs/remotes/origin/'
        if ref.startswith(_prefix):
            return ref[len(_prefix):]
        return ref.rsplit('/', 1)[-1]
    return 'main'


# --- Apiary config ------------------------------------------------------------

_APIARY_CONFIG = Path('~/.config/hive/apiary.json').expanduser()


def _load_apiary() -> list[Path] | None:
    """Load apiary config. Returns list of hive root Paths, or None if absent."""
    if not _APIARY_CONFIG.is_file():
        return None
    try:
        data = json.loads(_APIARY_CONFIG.read_text())
        return [Path(p).expanduser() for p in data.get('hives', [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f'{CROSS()} Invalid apiary config: {_APIARY_CONFIG}', file=sys.stderr)
        sys.exit(1)


def _storable_path(path: Path) -> str:
    """Convert a path to a storable string, using ~/... when under home."""
    try:
        return f'~/{path.resolve().relative_to(Path.home())}'
    except ValueError:
        return str(path.resolve())


def _save_apiary(hives: list[Path]) -> None:
    """Write the apiary config to disk."""
    _APIARY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = {'hives': [_storable_path(h) for h in hives]}
    _APIARY_CONFIG.write_text(json.dumps(data, indent=2) + '\n')


def _display_path(path: Path) -> str:
    """Format a path for display, using ~/... when under home."""
    try:
        return f'~/{path.relative_to(Path.home())}'
    except ValueError:
        return str(path)


# --- Discovery ----------------------------------------------------------------


def _find_hive_root() -> Path:
    """Find the hive root using three-tier detection.

    1. Inside a hive member repo: return parent of git root.
    2. At or under a configured apiary hive root: return the hive root.
    3. Outside any hive: fail (caller handles apiary fallback).
    """
    # Tier 1: inside a git repo → parent is the hive root
    toplevel = _git_out(['rev-parse', '--show-toplevel'])
    if toplevel is not None:
        return Path(toplevel).parent

    # Tier 2: at or under a configured apiary hive root (most specific wins)
    cwd = Path.cwd()
    apiary_hives = _load_apiary()
    if apiary_hives:
        resolved_cwd = cwd.resolve()
        best: tuple[int, Path] | None = None
        for h in apiary_hives:
            resolved_h = h.resolve()
            if resolved_cwd == resolved_h or resolved_h in resolved_cwd.parents:
                depth = len(resolved_h.parts)
                if best is None or depth > best[0]:
                    best = (depth, h)
        if best is not None:
            return best[1]

    # Tier 3: outside any hive
    return None


def _discover_local_repos(repo_path: Path) -> list[Path]:
    """Discover git repos in a repo's .local/ directory."""
    local_dir = repo_path / '.local'
    if not local_dir.is_dir():
        return []
    repos = []
    for entry in sorted(local_dir.iterdir()):
        if entry.is_dir() and (entry / '.git').exists():
            repos.append(entry)
    return repos


def _discover_repos(hive: Path) -> list[tuple[Path, list[Path]]]:
    """Discover repos in the hive.

    Returns list of (main_repo_path, [nested_repo_paths]).
    Nested repos include any git repos found in .local/.
    Sorted by directory name.
    """
    repos = []
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / '.git').exists():
            continue
        nested = _discover_local_repos(entry)
        repos.append((entry, nested))
    return repos


_NAME_RE = re.compile(r'^(.+)-(\d+)$')


def _infer_next_repo_dir(hive: Path, name_prefix: str | None) -> Path:
    """Infer the next numbered repo directory name in the hive.

    Scans for git repos, groups by prefix, and returns the path for the next
    numbered clone.  Raises SystemExit on ambiguity or missing prefix.
    """
    # Collect (prefix, number) for every git-containing dir
    groups: dict[str, list[int]] = {}
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / '.git').exists():
            continue
        m = _NAME_RE.match(entry.name)
        if m:
            prefix, num = m.group(1), int(m.group(2))
        else:
            prefix, num = entry.name, 1
        groups.setdefault(prefix, []).append(num)

    if name_prefix is not None:
        prefix = name_prefix
    elif len(groups) == 0:
        print(f'{CROSS()} No repos found to infer prefix from — use --name-prefix',
              file=sys.stderr)
        sys.exit(1)
    elif len(groups) == 1:
        prefix = next(iter(groups))
    else:
        found = ', '.join(sorted(groups))
        print(f'{CROSS()} Ambiguous prefixes found: {found} — use --name-prefix',
              file=sys.stderr)
        sys.exit(1)

    next_num = max(groups.get(prefix, [0])) + 1
    target = hive / f'{prefix}-{next_num}'

    if target.exists():
        print(f'{CROSS()} Target directory already exists: {target}', file=sys.stderr)
        sys.exit(1)

    return target


# --- Apiary runner ------------------------------------------------------------


def _run_apiary(hives: list[Path], fn) -> None:
    """Run a function across all apiary hives with grouped output."""
    valid = [h for h in hives if h.is_dir()]
    print(f'Apiary: {len(valid)} hive{"s" if len(valid) != 1 else ""}\n')
    for hive in valid:
        display = _display_path(hive)
        print(f'{"━" * 2} {display} {"━" * 2}')
        fn(hive)


# --- Status -------------------------------------------------------------------


def _get_repo_info(repo_path: Path) -> dict:
    """Gather git status data for a repo and return it as a dict.

    Keys: branch, default, ahead, behind, uncommitted, no_upstream, sync_unknown.
    """
    default = _default_branch(repo_path)
    branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path) or '(unknown)'

    ahead = 0
    behind = 0
    no_upstream = False
    sync_unknown = False

    upstream_check = _git(['rev-parse', '--abbrev-ref', f'{branch}@{{upstream}}'], cwd=repo_path)
    if upstream_check.returncode != 0:
        no_upstream = True
    else:
        counts = _git_out(
            ['rev-list', '--left-right', '--count', f'@{{upstream}}...HEAD'],
            cwd=repo_path,
        )
        if counts:
            behind_s, ahead_s = counts.split('\t')
            behind, ahead = int(behind_s), int(ahead_s)
        else:
            sync_unknown = True

    porcelain = _git_out(['status', '--porcelain'], cwd=repo_path)
    if porcelain is None:
        uncommitted = 0
    elif porcelain == '':
        uncommitted = 0
    else:
        uncommitted = len(porcelain.splitlines())

    return {
        'branch': branch,
        'default': default,
        'ahead': ahead,
        'behind': behind,
        'uncommitted': uncommitted,
        'no_upstream': no_upstream,
        'sync_unknown': sync_unknown,
    }


def _format_compact_segment(info: dict) -> str:
    """Format a compact one-line segment from repo info.

    Examples: "✓ main", "✗ feat/x  2↓ 3!", "✓ main  7↓ 1!"
    """
    on_default = info['branch'] == info['default']
    mark = CHECK() if on_default else CROSS()
    parts = [f'{mark} {info["branch"]}']

    indicators = []
    if info['no_upstream']:
        indicators.append('no-upstream')
    else:
        if info['behind']:
            indicators.append(f'{info["behind"]}↓')
        if info['ahead']:
            indicators.append(f'{info["ahead"]}↑')
    if info['uncommitted']:
        indicators.append(f'{info["uncommitted"]}!')

    if indicators:
        parts.append(' '.join(indicators))

    return '  '.join(parts)


def _report_repo_status(repo_path: Path, indent: str = '  ') -> None:
    """Print status for a single repo."""
    info = _get_repo_info(repo_path)

    # Branch status
    if info['branch'] == info['default']:
        print(f'{indent}{CHECK()} {info["branch"]}')
    else:
        default = info['default']
        print(f'{indent}{CROSS()} {info["branch"]} {C.dim(f"(default: {default})")}')

    # Ahead/behind upstream
    if info['no_upstream']:
        print(f'{indent}{CROSS()} no upstream tracking branch')
    elif info['sync_unknown']:
        print(f'{indent}{CROSS()} cannot determine sync status')
    elif info['ahead'] == 0 and info['behind'] == 0:
        print(f'{indent}{CHECK()} up to date')
    else:
        parts = []
        if info['ahead']:
            parts.append(f'{info["ahead"]} ahead')
        if info['behind']:
            parts.append(f'{info["behind"]} behind')
        print(f'{indent}{CROSS()} {", ".join(parts)}')

    # Working tree cleanliness
    porcelain = _git_out(['status', '--porcelain'], cwd=repo_path)
    if porcelain is None:
        print(f'{indent}{CROSS()} cannot determine working tree status')
    elif porcelain == '':
        print(f'{indent}{CHECK()} clean')
    else:
        n = len(porcelain.splitlines())
        print(f'{indent}{CROSS()} {n} uncommitted file{"s" if n != 1 else ""}')


def _status_single_hive(hive: Path, compact: bool) -> None:
    """Print status for a single hive."""
    repos = _discover_repos(hive)

    if not repos:
        print(f'  {CROSS()} No git repos found in hive')
        return

    if compact:
        spinner = _Spinner()

        # Fetch all repos in parallel
        spinner.start('Fetching all repos...')
        _fetch_all_parallel(repos)

        # Collect info for all repos
        rows: list[tuple[str, str, list[tuple[str, str]]]] = []
        for repo_path, nested in repos:
            spinner.update(f'Checking {repo_path.name}...')
            info = _get_repo_info(repo_path)
            segment = _format_compact_segment(info)
            nested_rows = []
            for n in nested:
                spinner.update(f'Checking {n.name}...')
                n_info = _get_repo_info(n)
                rel = str(n.relative_to(repo_path))
                nested_rows.append((rel, _format_compact_segment(n_info)))
            rows.append((repo_path.name, segment, nested_rows))

        spinner.stop()

        # Calculate column widths and print aligned
        max_name = max(len(name) for name, _, _ in rows)

        for name, segment, nested_rows in rows:
            name_pad = ' ' * (max_name - len(name))
            print(f'  {name}{name_pad}  {segment}')
            for nname, nseg in nested_rows:
                print(f'    {C.dim("↳")} {C.dim(nname)}  {nseg}')
    else:
        # Fetch all repos in parallel
        _fetch_all_parallel(repos)

        for repo_path, nested in repos:
            print(f'  {C.dim(repo_path.name)}')
            _report_repo_status(repo_path, indent='    ')

            for n in nested:
                rel = n.relative_to(repo_path)
                print(f'    {C.dim("↳")} {C.dim(str(rel))}')
                _report_repo_status(n, indent='      ')

            print()


def cmd_status(args: argparse.Namespace) -> None:
    """Execute the status subcommand."""
    compact = getattr(args, 'compact', False)
    apiary = getattr(args, 'apiary', False)

    if apiary:
        hives = _load_apiary()
        if not hives:
            print(f'{CROSS()} No apiary config found at {_APIARY_CONFIG}', file=sys.stderr)
            sys.exit(1)
        _run_apiary(hives, lambda h: _status_single_hive(h, compact))
        return

    hive = _find_hive_root()
    if hive is None:
        # Implicit apiary fallback for read-only status
        hives = _load_apiary()
        if hives:
            print(C.dim('(not in a hive — operating on apiary)'))
            print()
            _run_apiary(hives, lambda h: _status_single_hive(h, compact))
            return
        print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
        print(f'  Navigate to a repo, or create {_APIARY_CONFIG}', file=sys.stderr)
        sys.exit(1)

    print(f'Hive: {C.dim(str(hive))}\n')
    _status_single_hive(hive, compact)


# --- Pull ---------------------------------------------------------------------


_ORIGIN_REMOTE = RemoteProfile(
    name='origin',
    pull_args=('pull', '--rebase'),
)


def analyze_repo(repo_path: Path, remote_profile: RemoteProfile,
                 remote_cache: RemoteCache | None = None) -> RepoStatus:
    """Analyze a repo and return the sync action to execute."""
    branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path) or '(unknown)'
    remote_url = None
    if remote_cache is not None and remote_profile.name == 'origin':
        remote_url = _get_origin_url(repo_path)

    status = RepoStatus(
        path=repo_path,
        branch=branch,
        remote_profile=remote_profile,
        remote_url=remote_url,
        action=SyncAction.PULL,
    )

    # Check for uncommitted changes before anything else — a dirty repo
    # must always be reported as dirty, even if its HEAD matches the cache.
    porcelain = _git_out(['status', '--porcelain'], cwd=repo_path)
    if porcelain and porcelain != '':
        status.action = SyncAction.SKIP_DIRTY
        status.dirty_count = len(porcelain.splitlines())
        return status

    # Check pull cache — if HEAD matches last successful pull, skip
    if remote_cache is not None and remote_url:
        head_sha = _git_out(['rev-parse', 'HEAD'], cwd=repo_path)
        cached_sha = remote_cache.get_remote_sha(remote_url, branch)
        if head_sha and cached_sha == head_sha:
            remote_cache.cache_hits += 1
            status.cached = True
            status.up_to_date = True
            status.action = SyncAction.NONE
            return status
        remote_cache.cache_misses += 1

    return status


def execute_sync(status: RepoStatus, remote_cache: RemoteCache | None = None,
                 push: bool = False) -> RepoStatus:
    """Execute the planned sync action for a repo."""
    if status.action != SyncAction.PULL:
        return status

    # Pull --rebase
    r = _git(
        [*status.remote_profile.pull_args, status.remote_profile.name, status.branch],
        cwd=status.path,
        timeout=30,
    )
    if r.returncode != 0:
        _git(['rebase', '--abort'], cwd=status.path)
        status.action = SyncAction.ERROR
        stderr = r.stderr.strip()
        if stderr:
            status.error_lines = stderr.splitlines()[:3]
        return status

    # Determine pull outcome
    stdout = r.stdout.strip()
    if 'Already up to date' in stdout or 'Already up-to-date' in stdout:
        status.up_to_date = True
    else:
        status.pulled = True

    # Update pull cache with new HEAD
    if remote_cache is not None and status.remote_url:
        new_sha = _git_out(['rev-parse', 'HEAD'], cwd=status.path)
        if new_sha:
            remote_cache.set_remote_sha(status.remote_url, status.branch, new_sha)

    # Optional push
    if push:
        rp = _git(['push', status.remote_profile.name, status.branch],
                  cwd=status.path, timeout=30)
        if rp.returncode != 0:
            status.push_failed = True
            return status
        status.pushed = True

    return status


def _pull_repo(repo_path: Path, push: bool, indent: str = '  ',
               pull_cache: RemoteCache | None = None) -> bool:
    """Pull a single repo with verbose output. Returns True on success."""
    result = execute_sync(
        analyze_repo(repo_path, _ORIGIN_REMOTE, remote_cache=pull_cache),
        remote_cache=pull_cache,
        push=push,
    )

    if result.skipped:
        n = result.dirty_count
        print(f'{indent}{CROSS()} skipped — {n} uncommitted file{"s" if n != 1 else ""}')
        return False

    if result.pull_failed:
        print(f'{indent}{CROSS()} pull --rebase failed on {result.branch}')
        for line in result.error_lines:
            print(f'{indent}  {C.dim(line)}')
        return False

    if result.up_to_date:
        cached = ' (cached)' if result.cached else ''
        print(f'{indent}{CHECK()} {result.branch} — already up to date{cached}')
    else:
        print(f'{indent}{CHECK()} {result.branch} — pulled')

    if result.push_failed:
        print(f'{indent}{CROSS()} push failed')
        return False

    if result.pushed:
        print(f'{indent}{CHECK()} pushed')

    return True


def _format_pull_segment(result: RepoStatus) -> str:
    """Format a compact one-line segment from a pull result.

    Examples: "✓ main — up to date", "✗ skipped 3!", "✓ main — pulled + pushed"
    """
    if result.skipped:
        return f'{CROSS()} skipped {result.dirty_count}!'

    if result.pull_failed:
        return f'{CROSS()} {result.branch} — rebase failed'

    parts = []
    if result.up_to_date:
        label = 'up to date (cached)' if result.cached else 'up to date'
        parts.append(label)
    else:
        parts.append('pulled')

    if result.push_failed:
        return f'{CROSS()} {result.branch} — {parts[0]}, push failed'

    if result.pushed:
        parts.append('pushed')

    mark = CHECK()
    return f'{mark} {result.branch} — {" + ".join(parts)}'


def _is_notable(result: RepoStatus, default_branch: str) -> bool:
    """Return True when a pull result should be shown in quiet mode."""
    return (
        result.branch != default_branch
        or result.skipped
        or result.pull_failed
        or result.push_failed
    )


def _pull_single_hive(hive: Path, compact: bool, push: bool,
                      resolve_branches: bool = False,
                      pull_cache: RemoteCache | None = None,
                      quiet: bool = False,
                      render: bool = True) -> dict | None:
    """Pull all repos in a single hive."""
    repos = _discover_repos(hive)

    if not repos:
        print(f'  {CROSS()} No git repos found in hive')
        return None

    if compact:
        spinner = _Spinner()

        # Pull all repos with spinner progress
        rows: list[tuple[str, RepoStatus, list[tuple[str, RepoStatus]]]] = []
        for repo_path, nested in repos:
            spinner.start(f'Pulling {repo_path.name}...')
            result = execute_sync(
                analyze_repo(repo_path, _ORIGIN_REMOTE, remote_cache=pull_cache),
                remote_cache=pull_cache,
                push=push,
            )
            nested_rows = []
            for n in nested:
                spinner.update(f'Pulling {n.name}...')
                n_result = execute_sync(
                    analyze_repo(n, _ORIGIN_REMOTE, remote_cache=pull_cache),
                    remote_cache=pull_cache,
                    push=push,
                )
                rel = str(n.relative_to(repo_path))
                nested_rows.append((rel, n_result))
            rows.append((repo_path.name, result, nested_rows))

        spinner.stop()

        if quiet:
            visible_rows: list[tuple[str, RepoStatus | None, list[tuple[str, RepoStatus]]]] = []
            rendered_lines: list[str] = []
            clean_count = 0
            for (repo_path, nested_paths), (name, result, nested_rows) in zip(repos, rows):
                default_branch = _default_branch(repo_path)
                repo_notable = _is_notable(result, default_branch)
                visible_nested = []
                if repo_notable:
                    visible_rows.append((name, result, []))
                else:
                    clean_count += 1

                for nested_path, (nname, nresult) in zip(nested_paths, nested_rows):
                    nested_default = _default_branch(nested_path)
                    if _is_notable(nresult, nested_default):
                        visible_nested.append((nname, nresult))
                    else:
                        clean_count += 1

                if repo_notable:
                    visible_rows[-1] = (name, result, visible_nested)
                elif visible_nested:
                    visible_rows.append((name, None, visible_nested))

            if visible_rows:
                max_name = max(len(name) for name, _, _ in visible_rows)
                for name, result, nested_rows in visible_rows:
                    if result is not None:
                        name_pad = ' ' * (max_name - len(name))
                        segment = _format_pull_segment(result)
                        rendered_lines.append(f'  {name}{name_pad}  {segment}')
                    for nname, nresult in nested_rows:
                        nseg = _format_pull_segment(nresult)
                        # Include parent name when parent wasn't shown
                        display_name = nname if result is not None else f'{name}/{nname}'
                        rendered_lines.append(
                            f'    {C.dim("↳")} {C.dim(display_name)}  {nseg}')
            if clean_count:
                noun = 'repo' if clean_count == 1 else 'repos'
                rendered_lines.append(f'  {clean_count} {noun} clean / up to date')

            summary = {
                'repo_count': sum(1 + len(nested) for _, nested in repos),
                'clean_count': clean_count,
                'all_clean': len(visible_rows) == 0,
                'lines': rendered_lines,
            }
            if render:
                for line in rendered_lines:
                    print(line)
            if resolve_branches:
                _resolve_branches_for_hive(hive)
            return summary

        # Calculate column widths and print aligned
        max_name = max(len(name) for name, _, _ in rows)

        for name, result, nested_rows in rows:
            name_pad = ' ' * (max_name - len(name))
            segment = _format_pull_segment(result)
            print(f'  {name}{name_pad}  {segment}')
            for nname, nresult in nested_rows:
                nseg = _format_pull_segment(nresult)
                print(f'    {C.dim("↳")} {C.dim(nname)}  {nseg}')
    else:
        for repo_path, nested in repos:
            print(f'  {C.dim(repo_path.name)}')
            _pull_repo(repo_path, push=push, indent='    ',
                       pull_cache=pull_cache)

            for n in nested:
                rel = n.relative_to(repo_path)
                print(f'    {C.dim("↳")} {C.dim(str(rel))}')
                _pull_repo(n, push=push, indent='      ',
                           pull_cache=pull_cache)

            print()

    if resolve_branches:
        _resolve_branches_for_hive(hive)
    return None


def cmd_pull(args: argparse.Namespace) -> None:
    """Execute the pull subcommand."""
    quiet = getattr(args, 'quiet', False)
    compact = getattr(args, 'compact', False) or quiet
    apiary = getattr(args, 'apiary', False)
    resolve_branches = getattr(args, 'resolve_branches', False)
    pull_cache = RemoteCache()  # deduplicates same-origin repos within this run

    if apiary:
        hives = _load_apiary()
        if not hives:
            print(f'{CROSS()} No apiary config found at {_APIARY_CONFIG}', file=sys.stderr)
            sys.exit(1)
        if quiet:
            valid = [h for h in hives if h.is_dir()]
            print(f'Apiary: {len(valid)} hive{"s" if len(valid) != 1 else ""}\n')
            for hive_path in valid:
                summary = _pull_single_hive(
                    hive_path,
                    compact,
                    args.push,
                    resolve_branches,
                    pull_cache=pull_cache,
                    quiet=quiet,
                    render=False,
                )
                display = _display_path(hive_path)
                if summary and summary['all_clean']:
                    print(f'━━ {display} ━━  (all {summary["clean_count"]} repos clean)')
                else:
                    print(f'━━ {display} ━━')
                    for line in (summary or {}).get('lines', []):
                        print(line)
            return
        _run_apiary(
            hives,
            lambda h: _pull_single_hive(h, compact, args.push,
                                        resolve_branches,
                                        pull_cache=pull_cache,
                                        quiet=quiet),
        )
        return

    hive = _find_hive_root()
    if hive is None:
        # No implicit apiary for mutating commands
        print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
        print(f'  Use --apiary to pull across all configured hives', file=sys.stderr)
        sys.exit(1)

    print(f'Hive: {C.dim(str(hive))}\n')
    _pull_single_hive(hive, compact, args.push, resolve_branches,
                      pull_cache=pull_cache, quiet=quiet)


# --- Branch Resolution (Claude-powered) --------------------------------------

_RESOLVE_TIMEOUT = 180  # seconds per repo for Claude analysis


def _build_resolve_prompt(branch: str, default: str) -> str:
    """Build the prompt for Claude to analyze and resolve a non-default branch."""
    return f"""\
Determine if this branch's work has been incorporated into the default branch \
(typically via squash merge), then take the appropriate action.

Current branch: {branch}
Default branch: {default}

## Analysis

1. `git fetch origin`
2. List files changed on this branch vs the merge-base:
   `git diff --name-only $(git merge-base origin/{default} HEAD)..HEAD`
3. For those files, compare branch to default:
   `git diff origin/{default} HEAD -- <files from step 2>`
   Empty diff means branch changes are in default (merged).
   Non-empty diff means branch has unique work not yet in default.
4. Check `git log --oneline origin/{default} -20` for squash merge commits \
mentioning "{branch}".
5. Review `git log --oneline origin/{default}..HEAD` to understand the \
branch's unique commits.

## Actions

**Merged** (work IS in default):
  git checkout {default}
  git pull --rebase origin {default}
Print exactly: OUTCOME:merged:<one-line reason>

**Not merged** (has unique unmerged changes):
  git rebase origin/{default}
If conflicts: git rebase --abort
Print: OUTCOME:rebased:<one-line reason>
Or: OUTCOME:rebase-failed:<one-line reason>

**Uncertain**:
Do nothing.
Print: OUTCOME:skipped:<one-line reason>

## Safety (CRITICAL)

- NEVER delete any branch
- NEVER git push
- NEVER checkout {default} unless CERTAIN branch work is already there
- When in doubt: OUTCOME:skipped
- The OUTCOME line must appear exactly once, on its own line\
"""


def _detect_post_run_state(
    repo_path: Path, original_branch: str, default: str,
    pre_sha: str | None,
) -> str | None:
    """Check what actually happened to the repo after Claude ran.

    Uses a before/after comparison of branch tip SHA and current branch
    name to detect mutations.  Returns 'merged', 'rebased', or None
    (no detectable change).

    Args:
        repo_path: Path to the git repo.
        original_branch: Branch name before Claude ran.
        default: Default branch name.
        pre_sha: HEAD commit SHA captured before Claude ran.
    """
    current = _git_out(
        ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path,
    )
    if current == default and current != original_branch:
        return 'merged'
    # Still on the original branch — check if the tip moved.
    if current == original_branch and pre_sha:
        post_sha = _git_out(['rev-parse', 'HEAD'], cwd=repo_path)
        if post_sha and post_sha != pre_sha:
            return 'rebased'
    return None


def _resolve_branch(repo_path: Path, branch: str, default: str) -> dict:
    """Spawn a Claude session to analyze and resolve branch state.

    Returns dict with keys: outcome, detail.
    outcome is one of: merged, rebased, rebase-failed, skipped, error.

    Snapshots the branch tip SHA before invoking Claude, then compares
    after to reconcile the reported OUTCOME marker with reality.  If
    Claude mutated the repo but omitted or misformatted the marker,
    the observed state wins.
    """
    # Snapshot state before Claude runs.
    pre_sha = _git_out(['rev-parse', 'HEAD'], cwd=repo_path)

    prompt = _build_resolve_prompt(branch, default)

    try:
        r = subprocess.run(
            ['claude', '-p', '--model', 'opus',
             '--allowedTools', 'Bash(git *),Bash(git),Read'],
            input=prompt,
            capture_output=True, text=True, cwd=repo_path,
            timeout=_RESOLVE_TIMEOUT,
        )
    except FileNotFoundError:
        return {'outcome': 'error', 'detail': 'claude CLI not found'}
    except subprocess.TimeoutExpired:
        return {'outcome': 'error', 'detail': 'timed out'}

    if r.returncode != 0:
        stderr = r.stderr.strip()
        return {'outcome': 'error', 'detail': stderr or f'exit code {r.returncode}'}

    output = r.stdout.strip()

    # Parse the OUTCOME line (search from end of output)
    claimed: dict | None = None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith('OUTCOME:'):
            parts = line.split(':', 2)
            outcome = parts[1].strip().lower() if len(parts) > 1 else ''
            detail = parts[2].strip() if len(parts) > 2 else ''
            if outcome in ('merged', 'rebased', 'rebase-failed', 'skipped'):
                claimed = {'outcome': outcome, 'detail': detail}
            break

    # Reconcile claimed outcome against actual git state.
    # This catches the case where Claude mutated the repo (checkout,
    # rebase) but then omitted or misformatted the OUTCOME marker.
    observed = _detect_post_run_state(
        repo_path, branch, default, pre_sha,
    )

    if claimed:
        # If Claude says "skipped" but the repo actually changed, trust
        # the observation — the UI must not hide a real mutation.
        if claimed['outcome'] == 'skipped' and observed:
            return {'outcome': observed,
                    'detail': f'{claimed["detail"]} '
                              f'(observed: {observed})'.strip()}
        return claimed

    # No valid OUTCOME marker — fall back to observed state.
    if observed:
        return {'outcome': observed,
                'detail': f'no OUTCOME marker (observed: {observed})'}

    return {'outcome': 'skipped', 'detail': 'no OUTCOME in claude output'}


def _resolve_branches_for_hive(hive: Path) -> None:
    """Analyze non-default branches and resolve them using Claude."""
    repos = _discover_repos(hive)
    if not repos:
        return

    # Find repos on non-default branches with clean working trees
    candidates: list[tuple[Path, str, str, str]] = []
    for repo_path, nested in repos:
        for p in [repo_path] + nested:
            branch = _git_out(
                ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=p,
            )
            default = _default_branch(p)
            if not branch or branch == default or branch == 'HEAD':
                continue
            porcelain = _git_out(['status', '--porcelain'], cwd=p)
            if porcelain:
                continue  # skip dirty repos
            if p == repo_path:
                name = p.name
            else:
                name = f'{repo_path.name}/{p.relative_to(repo_path)}'
            candidates.append((p, name, branch, default))

    if not candidates:
        print(f'\n  {CHECK()} All clean repos on default branch'
              ' — nothing to resolve\n')
        return

    # Check claude CLI availability
    try:
        subprocess.run(
            ['claude', '--version'], capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print(f'\n  {CROSS()} claude CLI not found'
              ' — required for --resolve-branches\n',
              file=sys.stderr)
        return

    n = len(candidates)
    print(f'\n  Resolving {n} non-default'
          f' branch{"es" if n != 1 else ""}...\n')

    max_name = max(len(name) for _, name, _, _ in candidates)
    spinner = _Spinner()

    for repo_path, name, branch, default in candidates:
        spinner.start(f'Analyzing {name} ({branch})...')
        result = _resolve_branch(repo_path, branch, default)
        spinner.stop()

        outcome = result['outcome']
        detail = result['detail']
        name_pad = ' ' * (max_name - len(name))
        detail_suffix = f' — {detail}' if detail else ''

        if outcome == 'merged':
            print(f'  {name}{name_pad}  {CHECK()} '
                  f'{branch} → {default}{detail_suffix}')
        elif outcome == 'rebased':
            print(f'  {name}{name_pad}  {CHECK()} '
                  f'{branch} rebased onto {default}{detail_suffix}')
        elif outcome == 'rebase-failed':
            print(f'  {name}{name_pad}  {CROSS()} '
                  f'{branch} rebase failed{detail_suffix}')
        elif outcome == 'skipped':
            print(f'  {name}{name_pad}  {C.dim("—")} '
                  f'{branch} skipped{detail_suffix}')
        elif outcome == 'error':
            print(f'  {name}{name_pad}  {CROSS()} '
                  f'{branch} error: {detail}')

    print()


# --- PR Check -----------------------------------------------------------------

_PR_TIMEOUT = 15  # seconds — API calls may be slower than local git


def _classify_pr(pr: dict) -> str:
    """Derive a canonical state from a single PR response object."""
    if pr.get('merged') or pr.get('merged_at'):
        return 'merged'
    if pr.get('state') == 'open':
        return 'open'
    return 'closed'


def _get_pr_info(repo_path: Path, branch: str) -> dict | None:
    """Query fj for PR info for the given branch.

    Returns dict with keys: number, title, state ('open', 'merged', 'closed')
    or None if no PR found or fj unavailable.

    When multiple PRs match the same branch name (reuse), an open PR always
    wins — ``--clean`` must never delete a branch that still has a live PR.
    If none are open, the most recent closed/merged PR is returned.
    """
    try:
        r = subprocess.run(
            ['fj', 'pr', 'list', '--head', branch, '--state', 'all',
             '--limit', '5', '--json'],
            capture_output=True, text=True, cwd=repo_path, timeout=_PR_TIMEOUT,
        )
        if r.returncode != 0:
            return None
        stdout = r.stdout.strip()
        if not stdout or stdout == 'null':
            return None
        prs = json.loads(stdout)
        if not prs:
            return None

        # If any PR for this branch is open, report the open one.
        # This prevents --clean from deleting a reused branch that has a
        # new, live PR even if older closed/merged entries also exist.
        chosen = prs[0]
        chosen_state = _classify_pr(chosen)
        if chosen_state != 'open':
            for pr in prs[1:]:
                if _classify_pr(pr) == 'open':
                    chosen = pr
                    chosen_state = 'open'
                    break

        return {
            'number': chosen.get('number', 0),
            'title': chosen.get('title', ''),
            'state': chosen_state,
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError,
            TypeError, AttributeError, FileNotFoundError, OSError):
        return None


def _clean_pr_branch(repo_path: Path, branch: str) -> dict:
    """Switch to default branch, pull, and delete the stale PR branch.

    Returns dict with keys: success (bool), error (str | None).
    """
    default = _default_branch(repo_path)

    # Refuse to clean dirty repos
    porcelain = _git_out(['status', '--porcelain'], cwd=repo_path)
    if porcelain is None:
        return {'success': False, 'error': 'cannot determine working tree status'}
    if porcelain:
        n = len(porcelain.splitlines())
        return {'success': False,
                'error': f'{n} uncommitted file{"s" if n != 1 else ""}'}

    # Checkout default branch
    r = _git(['checkout', default], cwd=repo_path)
    if r.returncode != 0:
        return {'success': False, 'error': f'checkout {default} failed'}

    # Pull
    r = _git(['pull', '--rebase', 'origin', default], cwd=repo_path, timeout=30)
    if r.returncode != 0:
        _git(['rebase', '--abort'], cwd=repo_path)
        return {'success': False, 'error': 'pull --rebase failed'}

    # Delete old branch
    r = _git(['branch', '-D', branch], cwd=repo_path)
    if r.returncode != 0:
        return {'success': False, 'error': f'delete branch {branch} failed'}

    return {'success': True, 'error': None}


def _pr_check_single_hive(hive: Path, clean: bool) -> None:
    """Check PR status for all repos in a single hive."""
    repos = _discover_repos(hive)

    if not repos:
        print(f'  {CROSS()} No git repos found in hive')
        return

    # Phase 1: find repos on non-default branches (fast, git-only)
    candidates: list[tuple[Path, str]] = []
    for repo_path, _nested in repos:
        branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path)
        default = _default_branch(repo_path)
        if branch and branch != default:
            candidates.append((repo_path, branch))

    if not candidates:
        print(f'  {CHECK()} All {len(repos)} repos on default branch')
        print()
        return

    # Phase 2: query PR status in parallel
    spinner = _Spinner()
    spinner.start('Checking PR status...')

    pr_results: dict[str, dict | None] = {}
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def _check(rp: Path, br: str) -> None:
        info = _get_pr_info(rp, br)
        with lock:
            pr_results[str(rp)] = info

    for rp, br in candidates:
        t = threading.Thread(target=_check, args=(rp, br))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    spinner.stop()

    # Phase 3: display
    max_name = max(len(rp.name) for rp, _ in candidates)

    for rp, branch in candidates:
        pr = pr_results.get(str(rp))
        name_pad = ' ' * (max_name - len(rp.name))

        if pr:
            pr_label = f'#{pr["number"]} {pr["title"]}'
            if pr['state'] == 'open':
                indicator = C.green('●')
                line = f'  {rp.name}{name_pad}  {indicator} {pr_label}'
            else:
                struck = C.strikethrough(pr_label)
                state_label = C.dim(f'({pr["state"]})')
                line = f'  {rp.name}{name_pad}  {struck} {state_label}'
        else:
            indicator = C.yellow('?')
            line = f'  {rp.name}{name_pad}  {indicator} {branch} {C.dim("(no PR)")}'

        print(line)

    print()

    # Phase 4: clean if requested
    if clean:
        cleanable = [
            (rp, br) for rp, br in candidates
            if pr_results.get(str(rp))
            and pr_results[str(rp)]['state'] in ('merged', 'closed')
        ]
        if not cleanable:
            print(f'  {CHECK()} Nothing to clean')
            print()
            return

        n = len(cleanable)
        print(f'  Cleaning {n} stale branch{"es" if n != 1 else ""}...')
        print()
        for rp, branch in cleanable:
            result = _clean_pr_branch(rp, branch)
            default = _default_branch(rp)
            if result['success']:
                print(f'  {CHECK()} {rp.name}: {branch} → {default}')
            else:
                print(f'  {CROSS()} {rp.name}: {result["error"]}')
        print()


def cmd_pr_check(args: argparse.Namespace) -> None:
    """Execute the pr-check subcommand."""
    apiary = getattr(args, 'apiary', False)
    clean = getattr(args, 'clean', False)

    if apiary:
        hives = _load_apiary()
        if not hives:
            print(f'{CROSS()} No apiary config found at {_APIARY_CONFIG}', file=sys.stderr)
            sys.exit(1)
        _run_apiary(hives, lambda h: _pr_check_single_hive(h, clean))
        return

    hive = _find_hive_root()
    if hive is None:
        if clean:
            # No implicit apiary for mutating operations
            print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
            print(f'  Use --apiary to clean across all configured hives', file=sys.stderr)
            sys.exit(1)
        # Implicit apiary fallback for read-only pr-check
        hives = _load_apiary()
        if hives:
            print(C.dim('(not in a hive — operating on apiary)'))
            print()
            _run_apiary(hives, lambda h: _pr_check_single_hive(h, clean))
            return
        print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
        print(f'  Navigate to a repo, or create {_APIARY_CONFIG}', file=sys.stderr)
        sys.exit(1)

    print(f'Hive: {C.dim(str(hive))}\n')
    _pr_check_single_hive(hive, clean)


# --- Issues -------------------------------------------------------------------

_ISSUE_TIMEOUT = 15  # seconds


def _get_repo_slug(repo_path: Path) -> str | None:
    """Extract host/org/repo slug from git remote origin URL.

    Includes the host so repos on different Forgejo instances with the
    same org/repo path are not collapsed during deduplication.

    Handles HTTPS and SSH URLs:
      https://git.home.invezt.io/infra/home-dc.git → git.home.invezt.io/infra/home-dc
      git@git.home.invezt.io:infra/home-dc.git → git.home.invezt.io/infra/home-dc
    """
    url = _git_out(['remote', 'get-url', 'origin'], cwd=repo_path)
    if not url:
        return None
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    if '://' in url:
        parts = url.split('/')
        # parts: ['https:', '', 'user@host' or 'host', 'org', 'repo']
        if len(parts) >= 4:
            host = parts[2].split('@')[-1]
            path = '/'.join(parts[3:])
            return f'{host}/{path}'
    elif ':' in url:
        # git@host:org/repo
        host_part, path = url.split(':', 1)
        host = host_part.split('@')[-1]
        return f'{host}/{path}'
    return None


def _get_issues(repo_path: Path) -> list[dict] | None:
    """Query fj for open issues in the repo at repo_path.

    Returns list of dicts with keys: number, title, or None on error.
    """
    try:
        r = subprocess.run(
            ['fj', 'issue', 'list', '--state', 'open', '--json'],
            capture_output=True, text=True, cwd=repo_path,
            timeout=_ISSUE_TIMEOUT,
        )
        if r.returncode != 0:
            return None
        stdout = r.stdout.strip()
        if not stdout or stdout == 'null':
            return []
        issues = json.loads(stdout)
        if not isinstance(issues, list):
            return None
        return [
            {'number': i.get('number', 0), 'title': i.get('title', '')}
            for i in issues
        ]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError,
            TypeError, AttributeError, FileNotFoundError, OSError):
        return None


def _issues_display(hives: list[Path]) -> None:
    """Collect unique repos across hives, query open issues, and display."""
    # Collect all repos and deduplicate by remote slug
    slug_to_path: dict[str, Path] = {}
    for hive in hives:
        if not hive.is_dir():
            continue
        repos = _discover_repos(hive)
        for repo_path, _nested in repos:
            slug = _get_repo_slug(repo_path)
            if slug and slug not in slug_to_path:
                slug_to_path[slug] = repo_path

    if not slug_to_path:
        print(f'  {CROSS()} No git repos found')
        return

    # Query issues in parallel
    spinner = _Spinner()
    spinner.start('Fetching issues...')

    results: dict[str, list[dict] | None] = {}
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def _fetch(slug: str, rp: Path) -> None:
        spinner.update(f'Fetching {slug}...')
        issues = _get_issues(rp)
        with lock:
            results[slug] = issues

    for slug, rp in sorted(slug_to_path.items()):
        t = threading.Thread(target=_fetch, args=(slug, rp))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    spinner.stop()

    # Display repos that have open issues
    total_issues = 0
    repos_with_issues = 0
    errors = 0

    for slug in sorted(results):
        issues = results[slug]
        if issues is None:
            errors += 1
            print(f'  {C.yellow("?")} {C.dim(slug)} {C.dim("(query failed)")}')
            continue
        if not issues:
            continue
        repos_with_issues += 1
        total_issues += len(issues)
        print(f'  {slug}')
        for issue in issues:
            print(f'    {C.green("#" + str(issue["number"]))}  {issue["title"]}')

    # Summary
    checked = len(results) - errors
    if total_issues == 0 and errors == 0:
        print(f'  {CHECK()} No open issues ({checked} repos checked)')
    elif total_issues > 0:
        print()
        s_issues = 'issue' if total_issues == 1 else 'issues'
        s_repos = 'repo' if repos_with_issues == 1 else 'repos'
        print(f'  {total_issues} open {s_issues} across '
              f'{repos_with_issues} {s_repos} ({checked} repos checked)')
    print()


def cmd_issues(args: argparse.Namespace) -> None:
    """Execute the issues subcommand."""
    apiary = getattr(args, 'apiary', False)

    if apiary:
        hives = _load_apiary()
        if not hives:
            print(f'{CROSS()} No apiary config found at {_APIARY_CONFIG}',
                  file=sys.stderr)
            sys.exit(1)
        _issues_display(hives)
        return

    hive = _find_hive_root()
    if hive is None:
        # Implicit apiary fallback for read-only command
        hives = _load_apiary()
        if hives:
            print(C.dim('(not in a hive — operating on apiary)'))
            print()
            _issues_display(hives)
            return
        print(f'{CROSS()} Not inside a git repository or hive root',
              file=sys.stderr)
        print(f'  Navigate to a repo, or create {_APIARY_CONFIG}',
              file=sys.stderr)
        sys.exit(1)

    print(f'Hive: {C.dim(str(hive))}\n')
    _issues_display([hive])


# --- Create -------------------------------------------------------------------


def cmd_create(args: argparse.Namespace) -> None:
    """Execute the create subcommand."""
    if getattr(args, 'apiary', False):
        print(f'{CROSS()} create is not supported in apiary mode', file=sys.stderr)
        print(f'  Navigate to a hive member repo first', file=sys.stderr)
        sys.exit(1)
    hive = _find_hive_root()
    if hive is None:
        print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
        sys.exit(1)
    target = _infer_next_repo_dir(hive, getattr(args, 'name_prefix', None))

    print(f'Hive: {C.dim(str(hive))}')

    # Clone flow-app
    spinner = _Spinner()
    spinner.start(f'Cloning flow-app into {target.name}...')
    r = _git(['clone', FLOW_APP_CLONE_URL, str(target)])
    spinner.stop()

    if r.returncode != 0:
        print(f'{CROSS()} Clone failed: {target.name}')
        stderr = r.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:3]:
                print(f'  {C.dim(line)}')
        sys.exit(1)

    print(f'{CHECK()} {target.name}')


# --- Find Tmux Config ---------------------------------------------------------

_TMUX_CONF_DIR = Path('/tmp/hive-tmux')


def _short_name(hive: Path) -> str:
    """Get the short name (leaf directory) for a hive."""
    return hive.resolve().name


def cmd_find_tmux_config(args: argparse.Namespace) -> None:
    """Find the generated tmux config for the current hive.

    Generated configs are stored at /tmp/hive-tmux/<name>.conf and are
    created by hive-tmux-start when launching a session.
    """
    hive = _find_hive_root()
    if hive is None:
        print(f'{CROSS()} Not inside a git repository or hive root', file=sys.stderr)
        sys.exit(1)

    name = _short_name(hive)
    config_path = _TMUX_CONF_DIR / f'{name}.conf'

    if not config_path.is_file():
        print(f'{CROSS()} No generated config found at {config_path}', file=sys.stderr)
        print(f'  Run hive-tmux-start to generate the config.', file=sys.stderr)
        sys.exit(1)

    print(config_path)


# --- Apiary management --------------------------------------------------------


def cmd_apiary(args: argparse.Namespace) -> None:
    """Execute the apiary subcommand (list/add/remove)."""
    action = args.apiary_action

    if action == 'list':
        hives = _load_apiary()
        if not hives:
            print(f'No hives configured in {_APIARY_CONFIG}')
            return
        for h in hives:
            display = _display_path(h)
            if h.is_dir():
                print(f'  {CHECK()} {display}')
            else:
                print(f'  {CROSS()} {display} {C.dim("(not found)")}')

    elif action == 'add':
        path = Path(args.path).resolve() if args.path else Path.cwd().resolve()
        if not path.is_dir():
            print(f'{CROSS()} Not a directory: {path}', file=sys.stderr)
            sys.exit(1)
        hives = _load_apiary() or []
        for h in hives:
            resolved_h = h.resolve()
            if path == resolved_h:
                print(f'{CROSS()} Already in apiary: {_display_path(path)}',
                      file=sys.stderr)
                sys.exit(1)
            if resolved_h in path.parents:
                print(f'{CROSS()} Overlaps existing hive {_display_path(h)} '
                      f'(parent of {_display_path(path)})',
                      file=sys.stderr)
                sys.exit(1)
            if path in resolved_h.parents:
                print(f'{CROSS()} Overlaps existing hive {_display_path(h)} '
                      f'(child of {_display_path(path)})',
                      file=sys.stderr)
                sys.exit(1)
        hives.append(path)
        _save_apiary(hives)
        print(f'{CHECK()} Added {_display_path(path)}')

    elif action == 'remove':
        path = Path(args.path).resolve() if args.path else Path.cwd().resolve()
        hives = _load_apiary()
        if not hives:
            print(f'{CROSS()} No apiary config found at {_APIARY_CONFIG}',
                  file=sys.stderr)
            sys.exit(1)
        remaining = [h for h in hives if h.resolve() != path]
        if len(remaining) == len(hives):
            print(f'{CROSS()} Not in apiary: {_display_path(path)}',
                  file=sys.stderr)
            sys.exit(1)
        _save_apiary(remaining)
        print(f'{CHECK()} Removed {_display_path(path)}')


# --- Local (clone / pull in .local/) ------------------------------------------


def _require_hive_member_root() -> Path:
    """Require CWD is inside a top-level hive member repo.

    Returns the git root.  Exits if not in a repo, not in a hive, or
    inside a nested .local/ repo.
    """
    git_root_str = _git_out(['rev-parse', '--show-toplevel'])
    if git_root_str is None:
        print(f'{CROSS()} Not inside a git repository', file=sys.stderr)
        sys.exit(1)
    git_root = Path(git_root_str)

    if git_root.parent.name == '.local':
        print(f'{CROSS()} Cannot run from a .local/ repo — navigate to the parent hive member',
              file=sys.stderr)
        sys.exit(1)

    hive = _find_hive_root()
    if hive is None:
        print(f'{CROSS()} Not inside a hive', file=sys.stderr)
        sys.exit(1)

    return git_root


def _ensure_local_gitignored(git_root: Path) -> None:
    """Ensure .local/ is listed in the repo's .gitignore."""
    gitignore = git_root / '.gitignore'
    if gitignore.is_file():
        content = gitignore.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped in ('.local/', '.local'):
                return
        if not content.endswith('\n'):
            content += '\n'
        content += '\n# Local repo checkouts for cross-repo changes\n.local/\n'
        gitignore.write_text(content)
    else:
        gitignore.write_text('# Local repo checkouts for cross-repo changes\n.local/\n')


def _build_clone_url(git_root: Path, org_repo: str) -> str | None:
    """Build a clone URL from the current repo's origin and an org/repo path."""
    url = _git_out(['remote', 'get-url', 'origin'], cwd=git_root)
    if not url:
        return None
    suffix = '.git' if url.rstrip('/').endswith('.git') else ''
    if '://' in url:
        # https://user@host/org/repo → https://user@host/{org_repo}
        # ssh://git@host/org/repo.git → ssh://git@host/{org_repo}.git
        parts = url.split('/')
        base = '/'.join(parts[:3])
        return f'{base}/{org_repo}{suffix}'
    if ':' in url and not url.startswith('/'):
        # git@host:org/repo.git → git@host:{org_repo}.git
        host_part = url.split(':', 1)[0]
        return f'{host_part}:{org_repo}{suffix}'
    return None


def _local_clone(args: argparse.Namespace) -> None:
    """Clone a repo into .local/ of the current hive member."""
    git_root = _require_hive_member_root()
    repo_name = args.repo

    if '/' not in repo_name:
        print(f'{CROSS()} Use org/repo format (e.g. hellenic-flow/corpus)', file=sys.stderr)
        sys.exit(1)

    clone_dir_name = repo_name.rsplit('/', 1)[-1]
    local_dir = git_root / '.local'
    target = local_dir / clone_dir_name

    if target.exists():
        print(f'{CHECK()} .local/{clone_dir_name} already exists — skipping')
        return

    clone_url = _build_clone_url(git_root, repo_name)
    if clone_url is None:
        print(f'{CROSS()} Cannot determine clone URL from origin remote', file=sys.stderr)
        sys.exit(1)

    local_dir.mkdir(exist_ok=True)

    spinner = _Spinner()
    spinner.start(f'Cloning {repo_name} into .local/{clone_dir_name}...')
    r = _git(['clone', clone_url, str(target)])
    spinner.stop()

    if r.returncode != 0:
        print(f'{CROSS()} Clone failed: {clone_dir_name}')
        stderr = r.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:3]:
                print(f'  {C.dim(line)}')
        sys.exit(1)

    _ensure_local_gitignored(git_root)
    print(f'{CHECK()} .local/{clone_dir_name}')


def _local_pull(args: argparse.Namespace) -> None:
    """Pull all repos in .local/ of the current hive member."""
    git_root = _require_hive_member_root()
    local_repos = _discover_local_repos(git_root)

    if not local_repos:
        print(f'  {CHECK()} No repos in .local/')
        return

    for repo_path in local_repos:
        rel = repo_path.relative_to(git_root)
        print(f'  {C.dim(str(rel))}')
        _pull_repo(repo_path, push=False, indent='    ')
        print()


def cmd_local(args: argparse.Namespace) -> None:
    """Execute the local subcommand (clone / pull)."""
    action = args.local_action
    if action == 'clone':
        _local_clone(args)
    elif action == 'pull':
        _local_pull(args)


# --- Shell (dtach dev sessions) -----------------------------------------------

_DTACH_DIR = Path('/tmp/hive-dtach')

# Color palette (matches hive-tmux-start.py PALETTE)
_SHELL_PALETTE = [
    {'name': 'blue', 'rgb': '97;150;255', 'c256': '75'},
    {'name': 'teal', 'rgb': '45;212;168', 'c256': '43'},
    {'name': 'green', 'rgb': '102;187;106', 'c256': '114'},
    {'name': 'purple', 'rgb': '179;157;219', 'c256': '141'},
    {'name': 'amber', 'rgb': '255;202;40', 'c256': '220'},
    {'name': 'rose', 'rgb': '239;83;80', 'c256': '203'},
    {'name': 'cyan', 'rgb': '38;198;218', 'c256': '44'},
    {'name': 'orange', 'rgb': '255;167;38', 'c256': '214'},
]


def _hive_color(hive: Path) -> dict:
    """Get the color dict for a hive based on apiary position."""
    apiary = _load_apiary()
    if apiary:
        resolved = hive.resolve()
        for i, h in enumerate(apiary):
            if h.resolve() == resolved:
                return _SHELL_PALETTE[i % len(_SHELL_PALETTE)]
    return _SHELL_PALETTE[0]


def _workspace_number(name: str) -> str | None:
    """Extract the number suffix from a workspace directory name."""
    m = _NAME_RE.match(name)
    return m.group(2) if m else None


def _socket_path(hive_name: str, number: str) -> Path:
    """Return the dtach socket path for a hive workspace."""
    return _DTACH_DIR / f'{hive_name}-{number}.sock'


def _sidecar_path(hive_name: str, number: str) -> Path:
    """Return the metadata sidecar path for a hive workspace."""
    return _DTACH_DIR / f'{hive_name}-{number}.json'


def _socket_alive(sock: Path) -> bool:
    """Check if a dtach socket is connectable (session alive)."""
    if not sock.exists():
        return False
    # Try a non-interactive attach that immediately detaches.
    # dtach -p copies stdin to session — with empty stdin it exits immediately.
    # If the socket is dead, this returns non-zero.
    r = subprocess.run(
        ['dtach', '-p', str(sock)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=2,
    )
    return r.returncode == 0


def _find_workspace_for_reattach(hive: Path, hive_name: str) -> tuple[Path, str] | None:
    """If cwd is inside a hive workspace with an active dtach session, return it."""
    cwd = Path.cwd().resolve()
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir() or not (entry / '.git').exists():
            continue
        resolved = entry.resolve()
        if cwd == resolved or resolved in cwd.parents:
            num = _workspace_number(entry.name)
            if num and _socket_alive(_socket_path(hive_name, num)):
                return entry, num
    return None


def _find_clean_workspace(hive: Path, hive_name: str) -> tuple[Path, str] | None:
    """Find a hive member on the default branch with a clean worktree and no active session."""
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir() or not (entry / '.git').exists():
            continue
        num = _workspace_number(entry.name)
        if not num:
            continue
        # Skip if active dtach session
        if _socket_alive(_socket_path(hive_name, num)):
            continue
        # Check default branch
        default = _default_branch(entry)
        current = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=entry)
        if current != default:
            continue
        # Check clean worktree
        porcelain = _git_out(['status', '--porcelain'], cwd=entry)
        if porcelain is not None and porcelain == '':
            return entry, num
    return None


def _create_new_workspace(hive: Path) -> tuple[Path, str]:
    """Create a new hive workspace by cloning from an existing member's origin."""
    # Find an existing repo to get the clone URL
    existing = None
    for entry in sorted(hive.iterdir()):
        if entry.is_dir() and (entry / '.git').exists():
            existing = entry
            break
    if existing is None:
        print(f'{CROSS()} No existing repos in hive to clone from', file=sys.stderr)
        sys.exit(1)

    origin_url = _git_out(['remote', 'get-url', 'origin'], cwd=existing)
    if not origin_url:
        print(f'{CROSS()} Cannot determine origin URL from {existing.name}',
              file=sys.stderr)
        sys.exit(1)

    target = _infer_next_repo_dir(hive, None)
    spinner = _Spinner()
    spinner.start(f'Cloning into {target.name}...')
    r = _git(['clone', origin_url, str(target)])
    spinner.stop()

    if r.returncode != 0:
        print(f'{CROSS()} Clone failed: {target.name}', file=sys.stderr)
        stderr = r.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:3]:
                print(f'  {C.dim(line)}', file=sys.stderr)
        sys.exit(1)

    num = _workspace_number(target.name)
    if not num:
        num = '1'
    print(f'{CHECK()} Created {target.name}')
    return target, num


def _write_sidecar(hive_name: str, number: str, workspace: Path) -> None:
    """Write session metadata sidecar."""
    _DTACH_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        'hive': hive_name,
        'workspace': str(workspace),
        'number': int(number),
        'created': datetime.now(timezone.utc).isoformat(),
        'branch_at_checkout': _git_out(
            ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=workspace) or 'unknown',
    }
    _sidecar_path(hive_name, number).write_text(json.dumps(data, indent=2) + '\n')


def _launch_dtach(hive: Path, hive_name: str, workspace: Path, number: str) -> None:
    """Launch or reattach to a dtach session."""
    _DTACH_DIR.mkdir(parents=True, exist_ok=True)
    sock = _socket_path(hive_name, number)

    # Write sidecar if creating new session
    if not _socket_alive(sock):
        _write_sidecar(hive_name, number, workspace)

    # Pre-populate PR cache so the prompt has it on first render.
    # Always run (not just new sessions) — zshexit cleans the cache,
    # so reattaching would find an empty cache without this.
    pr_cache = _DTACH_DIR / f'{hive_name}-{number}.pr'
    branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=workspace)
    default = _default_branch(workspace)
    if branch and branch != default:
        fj = shutil.which('fj')
        if fj:
            try:
                r = subprocess.run(
                    [fj, 'pr', 'list', '--json', '--state', 'open',
                     '--head', branch],
                    capture_output=True, text=True, cwd=workspace, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    prs = json.loads(r.stdout)
                    if prs:
                        pr_cache.write_text(str(prs[0].get('number', '')))
                    else:
                        pr_cache.write_text('')
            except (subprocess.TimeoutExpired, Exception):
                pass

    # Resolve prompt color
    color = _hive_color(hive)

    # Find the prompt zsh file — check ~/bin first (installed), then script dir
    prompt_zsh = Path.home() / 'bin' / 'hive-shell-prompt.zsh'
    if not prompt_zsh.is_file():
        prompt_zsh = Path(__file__).resolve().parent / 'hive-shell-prompt.zsh'

    env = os.environ.copy()
    env['HIVE_ROOT'] = str(hive)
    env['HIVE_NAME'] = hive_name
    env['HIVE_WORKSPACE'] = workspace.name
    env['HIVE_NUMBER'] = number
    env['HIVE_COLOR_RGB'] = color['rgb']
    env['HIVE_COLOR_256'] = color['c256']

    # Launch zsh with the hive prompt sourced via ZDOTDIR wrapper.
    # We set ZDOTDIR to a temp dir so zsh loads our .zshenv and .zshrc.
    # Our .zshenv sources the user's real .zshenv (without resetting
    # ZDOTDIR — that would cause zsh to load the real .zshrc instead
    # of ours). Our .zshrc sources the user's real .zshrc, then layers
    # the hive prompt on top.
    # Resolve the user's real ZDOTDIR — ignore hive wrapper paths so
    # launching a hive shell from inside another hive shell works.
    original_zdotdir = env.get('ZDOTDIR', str(Path.home()))
    if original_zdotdir.startswith(str(_DTACH_DIR / '.zdotdir-')):
        original_zdotdir = env.get('HIVE_REAL_ZDOTDIR', str(Path.home()))
    zdotdir = _DTACH_DIR / f'.zdotdir-{hive_name}-{number}'
    zdotdir.mkdir(parents=True, exist_ok=True)

    # .zshenv runs first — source the user's .zshenv for PATH, env
    # vars, etc. We do NOT reset ZDOTDIR here: zsh uses ZDOTDIR at
    # each startup-file stage, so resetting it in .zshenv would cause
    # zsh to skip our .zshrc and load the real one instead.
    zshenv = zdotdir / '.zshenv'
    zshenv.write_text(
        '# Hive shell — source user .zshenv without resetting ZDOTDIR\n'
        f'HIVE_REAL_ZDOTDIR="{original_zdotdir}"\n'
        f'[[ -f "$HIVE_REAL_ZDOTDIR/.zshenv" ]] && source "$HIVE_REAL_ZDOTDIR/.zshenv"\n'
        '# Re-assert our ZDOTDIR in case the user .zshenv changed it\n'
        f'export ZDOTDIR="{zdotdir}"\n'
    )

    # .zshrc runs after .zshenv and .zprofile. Since ZDOTDIR still
    # points to our temp dir, zsh loads this .zshrc (not the real one).
    # We source the real .zshrc explicitly, then layer the prompt.
    zshrc = zdotdir / '.zshrc'
    zshrc.write_text(
        '# Hive shell — source user .zshrc then hive prompt overlay\n'
        '# Suppress p10k instant prompt warning — hive-shell-prompt.zsh\n'
        '# sources after the user .zshrc, which p10k flags as console I/O.\n'
        'typeset -g POWERLEVEL9K_INSTANT_PROMPT=quiet\n'
        f'[[ -f "$HIVE_REAL_ZDOTDIR/.zshrc" ]] && source "$HIVE_REAL_ZDOTDIR/.zshrc"\n'
        '# Fix HISTFILE — zsh defaults it to $ZDOTDIR/.zsh_history, which\n'
        '# lands in our temp wrapper dir. Point it back to the real location.\n'
        f'export HISTFILE="$HIVE_REAL_ZDOTDIR/.zsh_history"\n'
        f'source "{prompt_zsh}"\n'
    )
    env['ZDOTDIR'] = str(zdotdir)

    action = 'Reattaching to' if _socket_alive(sock) else 'Starting'
    print(f'{CHECK()} {action} {C.cyan(f"{hive_name}-{number}")} '
          f'in {C.dim(str(workspace))}')

    os.chdir(workspace)
    os.execvpe('dtach', [
        'dtach', '-A', str(sock),
        '-Ez', '-r', 'winch',
        'zsh',
    ], env)


def cmd_shell(args: argparse.Namespace) -> None:
    """Execute the shell subcommand."""
    shell_action = getattr(args, 'shell_action', None)

    if shell_action == 'list':
        _shell_list()
        return
    elif shell_action == 'cleanup':
        _shell_cleanup()
        return

    # Resolve hive
    hive_arg = getattr(args, 'hive', None)
    if hive_arg:
        # Try as short name first
        apiary = _load_apiary()
        hive = None
        if apiary:
            for h in apiary:
                if _short_name(h) == hive_arg:
                    hive = h
                    break
        if hive is None:
            # Try as path
            candidate = Path(hive_arg).expanduser().resolve()
            if candidate.is_dir():
                hive = candidate
        if hive is None:
            print(f'{CROSS()} Hive not found: {hive_arg}', file=sys.stderr)
            sys.exit(1)
    else:
        hive = _find_hive_root()
        if hive is None:
            print(f'{CROSS()} Not inside a hive. Use --hive to specify one.',
                  file=sys.stderr)
            sys.exit(1)

    hive_name = _short_name(hive)
    number_arg = getattr(args, 'number', None)

    if number_arg is not None:
        # Explicit workspace number — attach or create
        number = str(number_arg)
        # Find the workspace directory
        workspace = None
        for entry in sorted(hive.iterdir()):
            if not entry.is_dir() or not (entry / '.git').exists():
                continue
            num = _workspace_number(entry.name)
            if num == number:
                workspace = entry
                break
        if workspace is None:
            print(f'{CROSS()} No workspace #{number} found in {hive_name}',
                  file=sys.stderr)
            sys.exit(1)
        _launch_dtach(hive, hive_name, workspace, number)
        return

    # Find a clean workspace (no active session, default branch, clean worktree).
    # Note: we intentionally do NOT reattach based on cwd — terminal emulators
    # inherit the working directory when opening new tabs/windows, which would
    # cause spurious reattaches.  Use `hive shell N` to reattach explicitly.
    result = _find_clean_workspace(hive, hive_name)
    if result:
        workspace, number = result
        _launch_dtach(hive, hive_name, workspace, number)
        return

    # No clean workspace found — create a new one.
    workspace, number = _create_new_workspace(hive)
    _launch_dtach(hive, hive_name, workspace, number)


def _shell_list() -> None:
    """List active dtach sessions."""
    if not _DTACH_DIR.is_dir():
        print(C.dim('No active sessions'))
        return

    sockets = sorted(_DTACH_DIR.glob('*.sock'))
    if not sockets:
        print(C.dim('No active sessions'))
        return

    found = False
    for sock in sockets:
        name = sock.stem  # e.g., "infra-3"
        alive = _socket_alive(sock)
        sidecar = _DTACH_DIR / f'{name}.json'

        workspace_display = ''
        branch_display = ''
        if sidecar.is_file():
            try:
                data = json.loads(sidecar.read_text())
                ws = data.get('workspace', '')
                if ws:
                    workspace_display = _display_path(Path(ws))
                    # Get current branch if workspace exists
                    ws_path = Path(ws)
                    if ws_path.is_dir():
                        branch = _git_out(
                            ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=ws_path)
                        if branch:
                            branch_display = branch
            except (json.JSONDecodeError, KeyError):
                pass

        if alive:
            found = True
            status = CHECK()
            parts = [f'{status} {C.cyan(name)}']
            if branch_display:
                parts.append(branch_display)
            if workspace_display:
                parts.append(C.dim(workspace_display))
            print('  '.join(parts))
        else:
            found = True
            print(f'  {CROSS()} {name}  {C.dim("(dead)")}')

    if not found:
        print(C.dim('No active sessions'))


def _shell_cleanup() -> None:
    """Remove stale dtach sockets and orphaned sidecars."""
    if not _DTACH_DIR.is_dir():
        print(C.dim('Nothing to clean up'))
        return

    cleaned = 0

    # Dead sockets
    for sock in sorted(_DTACH_DIR.glob('*.sock')):
        if not _socket_alive(sock):
            sock.unlink()
            name = sock.stem
            sidecar = _DTACH_DIR / f'{name}.json'
            if sidecar.is_file():
                sidecar.unlink()
            pr_cache = _DTACH_DIR / f'{name}.pr'
            if pr_cache.is_file():
                pr_cache.unlink()
            print(f'  {CHECK()} Removed dead session: {name}')
            cleaned += 1

    # Orphaned sidecars
    for sidecar in sorted(_DTACH_DIR.glob('*.json')):
        sock = _DTACH_DIR / f'{sidecar.stem}.sock'
        if not sock.exists():
            sidecar.unlink()
            print(f'  {CHECK()} Removed orphaned sidecar: {sidecar.stem}')
            cleaned += 1

    # Orphaned PR caches
    for pr_cache in sorted(_DTACH_DIR.glob('*.pr')):
        sock = _DTACH_DIR / f'{pr_cache.stem}.sock'
        if not sock.exists():
            pr_cache.unlink()
            cleaned += 1

    # Stale zdotdir directories
    for zdotdir in sorted(_DTACH_DIR.glob('.zdotdir-*')):
        if not zdotdir.is_dir():
            continue
        # Extract name from .zdotdir-<hive>-<num>
        suffix = zdotdir.name[len('.zdotdir-'):]
        sock = _DTACH_DIR / f'{suffix}.sock'
        if not sock.exists() or not _socket_alive(sock):
            shutil.rmtree(zdotdir)
            cleaned += 1

    if cleaned == 0:
        print(C.dim('Nothing to clean up'))
    else:
        print(f'\nCleaned {cleaned} item(s)')


# --- Main ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description='Multi-repo status & pull utility for the flow hive.',
    )
    parser.add_argument(
        '--color', action='store_true',
        help='Force colored output even when not a TTY',
    )
    parser.add_argument(
        '--apiary', action='store_true',
        help='Operate across all configured hives (~/.config/hive/apiary.json)',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    status_parser = sub.add_parser(
        'status',
        help='Show branch, sync, and working-tree status',
        epilog=(
            'symbols:\n'
            '  ✓/✗   on/off default branch\n'
            '  N↓    N commits behind upstream\n'
            '  N↑    N commits ahead of upstream\n'
            '  N!    N uncommitted files'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status_parser.add_argument(
        '--compact', action='store_true',
        help='One-line-per-repo summary',
    )

    pull_parser = sub.add_parser('pull', help='Pull --rebase all repos')
    pull_parser.add_argument(
        '--push', action='store_true',
        help='Push to origin after successful pull',
    )
    pull_parser.add_argument(
        '--compact', action='store_true',
        help='One-line-per-repo summary',
    )
    pull_parser.add_argument(
        '-q', '--quiet', action='store_true',
        help='Show only notable repos and a clean summary (implies --compact)',
    )
    pull_parser.add_argument(
        '--resolve-branches', action='store_true',
        help='Use Claude to analyze non-default branches and resolve them '
             '(checkout default if merged, rebase onto default if not)',
    )

    pr_check_parser = sub.add_parser(
        'pr-check',
        help='Check PR status for repos on non-default branches',
    )
    pr_check_parser.add_argument(
        '--clean', action='store_true',
        help='Clean up branches with merged/closed PRs (checkout default, pull, delete)',
    )

    sub.add_parser('issues', help='List open Forgejo issues for each unique repo')

    create_parser = sub.add_parser('create', help='Clone a new repo into the hive')
    create_parser.add_argument(
        '--name-prefix',
        help='Explicit prefix (skips inference from existing repos)',
    )

    sub.add_parser(
        'find-tmux-config',
        help='Find the hive tmux config at /tmp/hive-tmux/<name>.conf',
    )

    local_parser = sub.add_parser('local', help='Manage local repo checkouts in .local/')
    local_sub = local_parser.add_subparsers(dest='local_action', required=True)
    local_clone_parser = local_sub.add_parser('clone', help='Clone a repo into .local/')
    local_clone_parser.add_argument('repo', help='Repo to clone (org/repo format)')
    local_sub.add_parser('pull', help='Pull all repos in .local/')

    apiary_parser = sub.add_parser(
        'apiary',
        help='Manage the apiary (list/add/remove hives)',
    )
    apiary_sub = apiary_parser.add_subparsers(dest='apiary_action', required=True)
    apiary_sub.add_parser('list', help='List configured hives')
    apiary_add = apiary_sub.add_parser('add', help='Add a hive to the apiary')
    apiary_add.add_argument('path', nargs='?', help='Path to add (default: cwd)')
    apiary_rm = apiary_sub.add_parser('remove', help='Remove a hive from the apiary')
    apiary_rm.add_argument('path', nargs='?', help='Path to remove (default: cwd)')

    shell_parser = sub.add_parser(
        'shell',
        help='Start or attach to a dtach dev shell session',
    )
    shell_parser.add_argument(
        '--hive',
        help='Hive short name or path (default: detect from cwd)',
    )
    shell_parser.add_argument(
        '--number', '-n', type=int,
        help='Workspace number (skip clean-workspace search)',
    )
    shell_sub = shell_parser.add_subparsers(dest='shell_action')
    shell_sub.add_parser('list', help='List active dtach sessions')
    shell_sub.add_parser('cleanup', help='Remove stale sessions')

    args = parser.parse_args()

    if args.color:
        C.force_enable()

    if args.command == 'status':
        cmd_status(args)
    elif args.command == 'pull':
        cmd_pull(args)
    elif args.command == 'pr-check':
        cmd_pr_check(args)
    elif args.command == 'issues':
        cmd_issues(args)
    elif args.command == 'create':
        cmd_create(args)
    elif args.command == 'local':
        cmd_local(args)
    elif args.command == 'find-tmux-config':
        cmd_find_tmux_config(args)
    elif args.command == 'apiary':
        cmd_apiary(args)
    elif args.command == 'shell':
        cmd_shell(args)


if __name__ == '__main__':
    main()
