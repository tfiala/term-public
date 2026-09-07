#!/usr/bin/env python3
# Canonical source for the `hive` tool. Other repos vendor this file from
# here at install time — make changes in tfiala/term-public, never in a
# vendored copy.
"""hive.py - Multi-repo status, pull & create utility for the flow hive.

Discovers all git repos in the hive (parent of current repo's git root)
and reports status or pulls.

Subcommands:
  status       Show branch, sync, and working-tree status for all repos
  pull         Fast-forward all repos (skips dirty/diverged/held ones) [--push]
  pr-check     Check PR status for repos on non-default branches [--clean]
  issues       List open Forgejo issues for each unique repo
  create       Clone a new repo into the hive with auto-numbered naming
  local        Manage local repo checkouts in .local/
    clone      Clone a repo into .local/ (org/repo format)
    pull       Pull all repos in .local/
  apiary       Manage the apiary (list/add/remove hives)
  tmux         Start or attach to a tmux dev session for a hive
    --list         List configured hives with their assigned colors
    --new-window   Open a new window on an unused workspace
    pairs          Show implementer/reviewer turn detail for the session
    role ROLE      Declare implementer/reviewer role, or clear it

Apiary mode (--apiary):
  Operates across all configured hives defined in ~/.config/hive/apiary.json.
  Implicit for read-only commands (status) when run from outside any hive.

Hold marker:
  git config hive.hold "<reason>"    # pull sweeps skip this repo, printing the reason
  git config --unset hive.hold       # release the hold

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
  hive.py create https://git.example.com/acme/widget.git
  hive.py create --name-prefix my-project
  hive.py local clone acme/widget
  hive.py local pull
  hive.py apiary list
  hive.py apiary add ~/src/flow
  hive.py apiary remove ~/src/flow
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
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


def _git_out(args: list[str], cwd: str | Path | None = None,
             timeout: float | None = None) -> str | None:
    """Run a git command, return stripped stdout or None on failure."""
    r = (_git(args, cwd=cwd) if timeout is None
         else _git(args, cwd=cwd, timeout=timeout))
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
    SKIP_DIVERGED = 'skip_diverged'
    SKIP_IN_PROGRESS = 'skip_in_progress'
    SKIP_HELD = 'skip_held'
    SKIP_NOT_DEFAULT = 'skip_not_default'
    SKIP_NO_REMOTE = 'skip_no_remote'
    SKIP_NO_BRANCH_ON_REMOTE = 'skip_no_branch_on_remote'
    ERROR = 'error'


@dataclass(frozen=True)
class RemoteProfile:
    """Remote-specific behavior for sync operations."""

    name: str
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
    in_progress_op: str = ''
    hold_reason: str = ''

    @property
    def skipped(self) -> bool:
        return self.action == SyncAction.SKIP_DIRTY

    @property
    def diverged(self) -> bool:
        return self.action == SyncAction.SKIP_DIVERGED

    @property
    def in_progress(self) -> bool:
        return self.action == SyncAction.SKIP_IN_PROGRESS

    @property
    def held(self) -> bool:
        return self.action == SyncAction.SKIP_HELD

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


def _get_origin_url(repo_path: Path,
                    timeout: float | None = None) -> str | None:
    """Get the normalized origin remote URL for a repo."""
    args = ['config', '--get', 'remote.origin.url']
    url = (_git_out(args, cwd=repo_path) if timeout is None
           else _git_out(args, cwd=repo_path, timeout=timeout))
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


def _default_branch(repo_path: Path,
                    timeout: float | None = None) -> str:
    """Determine the default branch for a repo.

    Reads origin/HEAD (set by ``git clone`` or ``git remote set-head``).
    Falls back to 'main' if the ref is missing.
    """
    args = ['symbolic-ref', 'refs/remotes/origin/HEAD']
    ref = (_git_out(args, cwd=repo_path) if timeout is None
           else _git_out(args, cwd=repo_path, timeout=timeout))
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


def _looks_like_hive(path: Path) -> bool:
    """True if ``path`` has at least one subdirectory that is a git repo."""
    try:
        entries = list(path.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return False
    for entry in entries:
        if not entry.is_dir():
            continue
        if (entry / '.git').exists():
            return True
    return False


def _cwd_or_exit() -> Path:
    """Return Path.cwd(), or exit with a clear message if cwd is invalid."""
    try:
        return Path.cwd()
    except (FileNotFoundError, OSError) as exc:
        print(
            f'{CROSS()} Cannot determine current directory: {exc}',
            file=sys.stderr,
        )
        print(
            '  Your shell may be in a directory that was deleted or moved. '
            'Try `cd` to a valid directory first.',
            file=sys.stderr,
        )
        sys.exit(1)


def _find_hive_root() -> Path | None:
    """Find the hive root using four-tier detection.

    1. Inside a hive member repo: return parent of git root.
    2. Cwd itself is a hive base dir (contains git repo subdirs): return cwd.
    3. At or under a configured apiary hive root: return the hive root.
    4. Outside any hive: return None (caller handles apiary fallback).
    """
    # Tier 1: inside a git repo → parent is the hive root
    toplevel = _git_out(['rev-parse', '--show-toplevel'])
    if toplevel is not None:
        return Path(toplevel).parent

    cwd = _cwd_or_exit()

    # Tier 2: cwd itself looks like a hive base dir
    if _looks_like_hive(cwd):
        return cwd

    # Tier 3: at or under a configured apiary hive root (most specific wins)
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

    # Tier 4: outside any hive
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
)


def _hold_reason(repo_path: Path) -> str | None:
    """Return the hive.hold reason if set, else None.

    A set-but-empty value still counts as a hold — the marker is the claim,
    the reason is a courtesy.
    """
    reason = _git_out(['config', '--get', 'hive.hold'], cwd=repo_path)
    if reason is None:
        return None
    return reason or '(no reason given)'


def _operation_in_progress(repo_path: Path) -> str | None:
    """Detect an in-flight history rewrite (rebase/merge/cherry-pick).

    Returns the operation name, or None when no operation is in progress.
    A checkout mid-rebase can have a clean worktree, so the dirty check
    alone cannot protect it.
    """
    git_dir_out = _git_out(['rev-parse', '--git-dir'], cwd=repo_path)
    if not git_dir_out:
        return None
    git_dir = Path(git_dir_out)
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    if (git_dir / 'rebase-merge').exists() or (git_dir / 'rebase-apply').exists():
        return 'rebase'
    if (git_dir / 'MERGE_HEAD').exists():
        return 'merge'
    if (git_dir / 'CHERRY_PICK_HEAD').exists():
        return 'cherry-pick'
    return None


def _is_diverged(repo_path: Path, upstream: str) -> bool:
    """True when HEAD and upstream have each moved past the other.

    Only claims divergence when the upstream ref actually resolves and both
    ancestry checks answer definitively "no" (exit 1) — any other failure
    (missing ref, git error) is not evidence of divergence.

    Callers must only pass a ref whose freshness is established — e.g.
    FETCH_HEAD immediately after a successful fetch. A stale remote-tracking
    ref can misclassify a fetch failure as divergence.
    """
    if not _git_out(['rev-parse', '--verify', upstream], cwd=repo_path):
        return False
    behind = _git(['merge-base', '--is-ancestor', 'HEAD', upstream],
                  cwd=repo_path)
    ahead = _git(['merge-base', '--is-ancestor', upstream, 'HEAD'],
                 cwd=repo_path)
    return behind.returncode == 1 and ahead.returncode == 1


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

    # An explicit hold wins over everything else — an agent has claimed
    # this checkout across a multi-step operation.
    hold = _hold_reason(repo_path)
    if hold is not None:
        status.action = SyncAction.SKIP_HELD
        status.hold_reason = hold
        return status

    # An in-flight rebase/merge/cherry-pick can present a clean worktree,
    # so it must be checked before (not via) the dirty check.
    op = _operation_in_progress(repo_path)
    if op:
        status.action = SyncAction.SKIP_IN_PROGRESS
        status.in_progress_op = op
        return status

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

    # Fast-forward pull, split into fetch + ff-only integration so a fetch
    # failure can never be misread as divergence against a stale
    # remote-tracking ref. Never rebase or merge in a sweep: divergence means
    # an in-flight history rewrite or genuinely unpushed work — both are
    # report-worthy, neither is auto-fixable (issue #26).
    f = _git(['fetch', status.remote_profile.name, status.branch],
             cwd=status.path, timeout=30)
    if f.returncode != 0:
        # A failed fetch observed nothing — report its error unchanged.
        status.action = SyncAction.ERROR
        stderr = f.stderr.strip()
        if stderr:
            status.error_lines = stderr.splitlines()[:3]
        return status

    r = _git(['merge', '--ff-only', 'FETCH_HEAD'], cwd=status.path, timeout=30)
    if r.returncode != 0:
        # FETCH_HEAD is fresh from the fetch that just succeeded, so this
        # ancestry check is definitive.
        if _is_diverged(status.path, 'FETCH_HEAD'):
            status.action = SyncAction.SKIP_DIVERGED
            return status
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

    if result.held:
        print(f'{indent}{CROSS()} skipped — held: {result.hold_reason}')
        return False

    if result.in_progress:
        print(f'{indent}{CROSS()} skipped — {result.in_progress_op} in progress')
        return False

    if result.skipped:
        n = result.dirty_count
        print(f'{indent}{CROSS()} skipped — {n} uncommitted file{"s" if n != 1 else ""}')
        return False

    if result.diverged:
        print(f'{indent}{CROSS()} skipped — {result.branch} diverged from '
              f'{result.remote_profile.name}')
        return False

    if result.pull_failed:
        print(f'{indent}{CROSS()} pull failed on {result.branch}')
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

    Examples: "✓ main — up to date", "✗ skipped 3!", "✓ main — pulled + pushed",
    "✗ feature — diverged, skipped"
    """
    if result.held:
        return f'{CROSS()} {result.branch} — held: {result.hold_reason}'

    if result.in_progress:
        return f'{CROSS()} {result.branch} — {result.in_progress_op} in progress, skipped'

    if result.skipped:
        return f'{CROSS()} skipped {result.dirty_count}!'

    if result.diverged:
        return f'{CROSS()} {result.branch} — diverged, skipped'

    if result.pull_failed:
        return f'{CROSS()} {result.branch} — pull failed'

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
        or result.diverged
        or result.in_progress
        or result.held
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

    # Pull (fast-forward only — a sweep never resolves divergence). Fetch
    # separately so a fetch failure is never misread as divergence against
    # a stale remote-tracking ref.
    r = _git(['fetch', 'origin', default], cwd=repo_path, timeout=30)
    if r.returncode != 0:
        return {'success': False, 'error': f'fetch origin {default} failed'}
    r = _git(['merge', '--ff-only', 'FETCH_HEAD'], cwd=repo_path, timeout=30)
    if r.returncode != 0:
        if _is_diverged(repo_path, 'FETCH_HEAD'):
            return {'success': False, 'error': f'{default} diverged from origin'}
        return {'success': False, 'error': 'fast-forward pull failed'}

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
      https://git.example.com/acme/widget.git → git.example.com/acme/widget
      git@git.example.com:acme/widget.git → git.example.com/acme/widget
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


def _infer_clone_url_from_siblings(hive: Path) -> str | None:
    """If every sibling git workspace in ``hive`` shares one origin URL,
    return it. Returns None when there is no shared origin (empty hive, or
    workspaces pointing at different remotes) — the caller must supply a URL.
    """
    urls: set[str] = set()
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir() or not (entry / '.git').exists():
            continue
        url = _git_out(['remote', 'get-url', 'origin'], cwd=entry)
        if url:
            urls.add(url)
    return urls.pop() if len(urls) == 1 else None


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

    clone_url = getattr(args, 'url', None)
    if not clone_url:
        clone_url = _infer_clone_url_from_siblings(hive)
        if not clone_url:
            print(f'{CROSS()} No shared origin among hive workspaces — pass a '
                  f'URL explicitly: hive create <url>', file=sys.stderr)
            sys.exit(1)

    target = _infer_next_repo_dir(hive, getattr(args, 'name_prefix', None))

    print(f'Hive: {C.dim(str(hive))}')

    spinner = _Spinner()
    spinner.start(f'Cloning {clone_url} into {target.name}...')
    r = _git(['clone', clone_url, str(target)])
    spinner.stop()

    if r.returncode != 0:
        print(f'{CROSS()} Clone failed: {target.name}')
        stderr = r.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:3]:
                print(f'  {C.dim(line)}')
        sys.exit(1)

    print(f'{CHECK()} {target.name}')


# --- Hive name helpers --------------------------------------------------------


def _short_name(hive: Path) -> str:
    """Get the short name (leaf directory) for a hive."""
    return hive.resolve().name


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
        print(f'{CROSS()} Use org/repo format (e.g. acme/widget)', file=sys.stderr)
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


# --- tmux dev sessions --------------------------------------------------------

_TMUX_DIR = Path('/tmp/hive-tmux')

# Per-hive color palette, assigned by position in the apiary config.
# `rgb`/`c256` drive the `--list` output and the HIVE_COLOR_* env vars; the
# hex fields (`primary`, `background`, `foreground`, `inactive_bg`) drive the
# generated tmux status bar. Hex values are normative in ADR-0045.
# The flat fields are the night (dark-background) variants; each `day`
# sub-dict overrides them for light backgrounds (_palette_for_mode).
_SHELL_PALETTE = [
    {'name': 'blue', 'rgb': '97;150;255', 'c256': '75',
     'primary': '#6196ff', 'background': '#1a2744',
     'foreground': '#82aaff', 'inactive_bg': '#2c3e6b',
     'day': {'rgb': '47;95;208', 'c256': '26',
             'primary': '#2f5fd0', 'background': '#dce6fa',
             'foreground': '#1c3f8f', 'inactive_bg': '#b9cdf2'}},
    {'name': 'teal', 'rgb': '45;212;168', 'c256': '43',
     'primary': '#2dd4a8', 'background': '#1a3a3a',
     'foreground': '#56d6c2', 'inactive_bg': '#2c5e5e',
     'day': {'rgb': '15;158;126', 'c256': '30',
             'primary': '#0f9e7e', 'background': '#d7f2ec',
             'foreground': '#0b5f4c', 'inactive_bg': '#aee0d6'}},
    {'name': 'green', 'rgb': '102;187;106', 'c256': '114',
     'primary': '#66bb6a', 'background': '#1a3320',
     'foreground': '#81c784', 'inactive_bg': '#2c5e3e',
     'day': {'rgb': '63;153;68', 'c256': '28',
             'primary': '#3f9944', 'background': '#ddf0de',
             'foreground': '#256b29', 'inactive_bg': '#b8dfba'}},
    {'name': 'purple', 'rgb': '179;157;219', 'c256': '141',
     'primary': '#b39ddb', 'background': '#2a1a44',
     'foreground': '#ce93d8', 'inactive_bg': '#4a3a6e',
     'day': {'rgb': '126;87;194', 'c256': '97',
             'primary': '#7e57c2', 'background': '#eae2f8',
             'foreground': '#4d2d8c', 'inactive_bg': '#d2c3ef'}},
    {'name': 'amber', 'rgb': '255;202;40', 'c256': '220',
     'primary': '#ffca28', 'background': '#3a2e1a',
     'foreground': '#ffd54f', 'inactive_bg': '#5e4e2e',
     'day': {'rgb': '184;134;11', 'c256': '136',
             'primary': '#b8860b', 'background': '#f9efd2',
             'foreground': '#7a5c00', 'inactive_bg': '#eeddad'}},
    {'name': 'rose', 'rgb': '239;83;80', 'c256': '203',
     'primary': '#ef5350', 'background': '#3a1a1a',
     'foreground': '#ef9a9a', 'inactive_bg': '#5e2e2e',
     'day': {'rgb': '211;47;47', 'c256': '160',
             'primary': '#d32f2f', 'background': '#fbe0e0',
             'foreground': '#8f1f1f', 'inactive_bg': '#f3bcbc'}},
    {'name': 'cyan', 'rgb': '38;198;218', 'c256': '44',
     'primary': '#26c6da', 'background': '#1a3344',
     'foreground': '#80deea', 'inactive_bg': '#2c4e5e',
     'day': {'rgb': '9;151;173', 'c256': '31',
             'primary': '#0997ad', 'background': '#d9f1f5',
             'foreground': '#086274', 'inactive_bg': '#ace0e8'}},
    {'name': 'orange', 'rgb': '255;167;38', 'c256': '214',
     'primary': '#ffa726', 'background': '#3a2a1a',
     'foreground': '#ffcc80', 'inactive_bg': '#5e4a2e',
     'day': {'rgb': '217;122;0', 'c256': '166',
             'primary': '#d97a00', 'background': '#fbe9d4',
             'foreground': '#8a4d00', 'inactive_bg': '#f2d3ab'}},
]


def _appearance_mode() -> str:
    """'day' or 'night' from the macOS appearance.

    Falls back to the term-theme state file on non-macOS hosts, and to
    night (the original, pre-day-mode look) when neither source answers.
    """
    try:
        r = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                           capture_output=True, text=True)
        return 'night' if r.returncode == 0 else 'day'
    except OSError:
        pass
    state = Path(os.environ.get('XDG_CACHE_HOME',
                                str(Path.home() / '.cache')))
    try:
        mode = (state / 'term-theme' / 'mode').read_text().strip()
    except OSError:
        return 'night'
    return mode if mode in ('day', 'night') else 'night'


def _palette_for_mode(color: dict, mode: str) -> dict:
    """Resolve a palette entry to the given appearance mode."""
    if mode != 'day':
        return color
    return {**color, **color['day']}


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


def _workspace_sort_key(workspace: Path) -> tuple[str, int, str]:
    """Sort numbered workspaces by numeric suffix, not digit text."""
    match = _NAME_RE.match(workspace.name)
    if match is None:
        return workspace.name, -1, workspace.name
    return match.group(1), int(match.group(2)), workspace.name


# --- tmux session helpers -----------------------------------------------------


def _tmux_available() -> bool:
    """True if the tmux binary is on PATH."""
    return shutil.which('tmux') is not None


def _tmux_sessions() -> list[str]:
    """Return the names of all current tmux sessions."""
    try:
        r = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name}'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []
    return r.stdout.split()


def _next_session_num(name: str, sessions: list[str]) -> int:
    """Find the next free session number for a hive (sessions are <name>-N)."""
    prefix = f'{name}-'
    nums = [
        int(s[len(prefix):])
        for s in sessions
        if s.startswith(prefix) and s[len(prefix):].isdigit()
    ]
    return max(nums) + 1 if nums else 0


def _group_exists(name: str, sessions: list[str]) -> bool:
    """True if a session for this hive already exists."""
    return any(s.startswith(f'{name}-') for s in sessions)


def _current_session() -> str | None:
    """Return the current tmux session name, or None if not inside tmux."""
    if not os.environ.get('TMUX'):
        return None
    try:
        r = subprocess.run(
            ['tmux', 'display-message', '-p', '#{session_name}'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _windows_in_session(session: str) -> list[str]:
    """Return the pane paths of every window in a session."""
    try:
        r = subprocess.run(
            ['tmux', 'list-windows', '-t', session,
             '-F', '#{pane_current_path}'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _discover_workspaces(hive: Path) -> list[Path]:
    """Return git workspaces in natural numeric-suffix order."""
    workspaces = [main for main, _nested in _discover_repos(hive)]
    return sorted(workspaces, key=_workspace_sort_key)


def _resolve_tmux_hive(hive_arg: str | None) -> Path | None:
    """Resolve a hive from --hive (short name or path), or detect from cwd."""
    if hive_arg is None:
        return _find_hive_root()
    apiary = _load_apiary() or []
    # Try as a path first.
    path = Path(hive_arg).expanduser()
    if path.is_dir():
        resolved = path.resolve()
        for h in apiary:
            if h.resolve() == resolved:
                return h
        return path  # a real dir not in the apiary — allow it anyway
    # Try as a short name.
    for h in apiary:
        if _short_name(h) == hive_arg:
            return h
    return None


# --- tmux config generation ---------------------------------------------------


def _style_option_pairs(color: dict) -> list[tuple[str, str, str]]:
    """(comment, option, value) triples for the palette-derived session
    styling — shared by the generated config and `hive tmux restyle` so
    the two can never drift. An empty comment continues the prior group.
    """
    return [
        ('Status bar (session-scoped)', 'status-style',
         f'bg={color["background"]},fg={color["foreground"]}'),
        ('Pane borders', 'pane-border-style',
         f'fg={color["inactive_bg"]}'),
        ('', 'pane-active-border-style', f'fg={color["primary"]}'),
        ('Status left (session name badge)', 'status-left',
         f'#[bg={color["primary"]},fg={color["background"]},bold]'
         ' #{session_name} #[default] '),
    ]


def _generate_tmux_config(hive: Path, color: dict) -> str:
    """Generate the per-hive tmux config sourced at session creation.

    All settings are session-scoped (no -g) so multiple hive sessions
    coexist. The base ~/.tmux/tmux.conf is sourced first — it must carry
    the Claude-CLI-safe settings (allow-passthrough on, synchronized
    output, extended keys) per ADR-0063.
    """
    name = _short_name(hive)
    hive_root = str(hive.resolve())
    lines = [
        f'# Generated tmux config for hive: {name}',
        f'# Color theme: {color["name"]}',
        '',
        '# Base config — carries the Claude-CLI-safe settings (ADR-0063)',
        'source-file ~/.tmux/tmux.conf',
        '',
        '# Environment (session-scoped)',
        f'set-environment HIVE_ROOT "{hive_root}"',
        f'set-environment HIVE_NAME "{name}"',
        f'set-environment HIVE_COLOR "{color["name"]}"',
        f'set-environment HIVE_COLOR_RGB "{color["rgb"]}"',
        f'set-environment HIVE_COLOR_256 "{color["c256"]}"',
    ]
    for comment, opt, val in _style_option_pairs(color):
        if comment:
            lines += ['', f'# {comment}']
        lines.append(f'set {opt} "{val}"')
    lines += [
        'set status-left-length 20',
        '',
        '# Status right (turn pair | compact branch [sync] | time).',
        'set status-right-length 60',
        'set status-right " #(hive tmux turn-refresh '
        '\\"#{session_name}\\")'
        ' #(hive tmux status-context '
        '\\"#{pane_current_path}\\" \\"#{client_width}\\" '
        '\\"#{session_name}\\" \\"#{session_windows}\\")'
        '%H:%M PT "',
        '',
        '# Window naming',
        'set automatic-rename off',
        'set allow-rename off',
        '',
        '# Window label update hooks (session-scoped)',
        'set-hook after-new-window   "run-shell -b \'hive tmux label-window \\"#{pane_current_path}\\" \\"#{window_id}\\"\'"',
        'set-hook after-select-window "run-shell -b \'hive tmux label-window \\"#{pane_current_path}\\" \\"#{window_id}\\"\'"',
        'set-hook after-select-pane   "run-shell -b \'hive tmux label-window \\"#{pane_current_path}\\" \\"#{window_id}\\"\'"',
        '',
        '# Keybindings — tmux keybindings are global (not session-scoped), so',
        '# every hive-specific binding guards on $HIVE_ROOT and falls back to',
        '# the default behavior for non-hive sessions.',
        '',
        '# backtick + 0: window 10 in hives; use tmux\'s stock :=0 elsewhere',
        'bind 0 if-shell \'[ -n "$HIVE_ROOT" ]\' '
        '\'select-window -t :=10\' \'select-window -t :=0\'',
        '',
        '# backtick + r: reload config (hive config or base tmux.conf)',
        'bind r run-shell -b \''
        'if [ -n "$HIVE_NAME" ]; then'
        '  CONF="/tmp/hive-tmux/$HIVE_NAME.conf";'
        '  [ -f "$CONF" ] && tmux source-file "$CONF" &&'
        '    hive tmux refresh-labels "#{session_name}" &&'
        '    tmux display-message "Reloaded: $CONF"'
        '    || tmux display-message "Reload failed: $CONF";'
        'else'
        '  tmux source-file "$HOME/.tmux/tmux.conf" && tmux display-message "Reloaded!";'
        'fi\'',
        '',
        '# backtick + c: new window (hive workspace picker, or plain new-window)',
        'bind c run-shell \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  hive tmux --hive "$HIVE_ROOT" --new-window;'
        'else'
        '  tmux new-window;'
        'fi\'',
        '',
        '# backtick + b: CI status popup (hive only)',
        'bind b run-shell \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  hive-ci-popup --hive-root "$HIVE_ROOT";'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        '',
        '# backtick + a: run-dsl status popup (hive only)',
        'bind a run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  hive tmux popup --client "#{client_name}" --pane "#{pane_id}" --cwd "#{pane_current_path}" hive tmux runs --hive-root "$HIVE_ROOT";'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        '',
        '# backtick + p: implementer/reviewer turn pairs (hive only)',
        'bind p run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  hive tmux popup --client "#{client_name}" --pane "#{pane_id}" --cwd "#{pane_current_path}" hive tmux pairs "#{session_name}";'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        '',
        '# backtick + g/G/C-g: hive multi-repo management (hive only)',
        'bind g run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  tmux display-message "Hive: fetching status..." &&'
        '  hive tmux popup --client "#{client_name}" --pane "#{pane_id}" --cwd "#{pane_current_path}" hive --color status --compact;'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        'bind G run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  tmux display-message "Hive: pulling repos..." &&'
        '  hive tmux popup --client "#{client_name}" --pane "#{pane_id}" --cwd "#{pane_current_path}" hive --color pull --compact;'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        'bind C-g run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  tmux display-message "Hive: pulling + pushing repos..." &&'
        '  hive tmux popup --client "#{client_name}" --pane "#{pane_id}" --cwd "#{pane_current_path}" hive --color pull --compact --push;'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
        '',
        '# backtick + R: force-refresh all window labels (hive only)',
        'bind R run-shell -b \''
        'if [ -n "$HIVE_ROOT" ]; then'
        '  hive tmux turn-refresh "#{session_name}" --bust-cache >/dev/null &&'
        '    hive tmux refresh-labels "#{session_name}" &&'
        '    tmux display-message "Labels refreshed" ||'
        '    tmux display-message "Label refresh failed";'
        'else'
        '  tmux display-message "Not in a hive session";'
        'fi\'',
    ]
    return '\n'.join(lines) + '\n'


def _write_tmux_config(hive: Path, color: dict) -> Path:
    """Write the generated tmux config to /tmp/hive-tmux/<name>.conf."""
    _TMUX_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _TMUX_DIR / f'{_short_name(hive)}.conf'
    config_path.write_text(_generate_tmux_config(hive, color))
    return config_path


# --- Window labeling ----------------------------------------------------------

_LABEL_CACHE_TTL = 300  # seconds — window hooks fire often; don't hammer fj
_DEFAULT_BRANCHES = {'main', 'master', 'develop', 'dev',
                     'flow-dev', 'flow-prod', 'infra-dev', 'infra-prod'}
_WINDOW_STATUS_FORMAT = (
    ' #I#{?@hive_run_suffix, #{@hive_run_suffix},}'
    '#{?@hive_turn_suffix, #{@hive_turn_suffix},} ')
_WINDOW_STATUS_CURRENT_FORMAT = (
    '#[reverse,bold][#I]#[default]'
    '#{?@hive_run_suffix, #{@hive_run_suffix},}'
    '#{?@hive_turn_suffix, #{@hive_turn_suffix},}')


def _label_cache_key(workspace: Path) -> str:
    """Cache filename stem for a workspace (leaf name + path hash).

    The path hash prevents collisions between workspaces with the same
    leaf name in different hives.
    """
    import hashlib
    full = str(workspace.resolve())
    digest = hashlib.sha256(full.encode()).hexdigest()[:12]
    return f'label-{workspace.resolve().name}-{digest}'


def _shorten_branch(branch: str) -> str:
    """Shorten a branch name stored in tmux's internal window name."""
    short = branch.split('/', 1)[1] if '/' in branch else branch
    if len(short) > 16:
        short = short[:14] + '..'
    return short


def _compact_branch(branch: str, max_length: int) -> str:
    """Elide a branch while retaining its kind and task initials when possible.

    ``fix/automatic-publication-receipt-ledger`` becomes ``fix/aprl`` at
    ordinary widths, ``f/aprl`` when tighter, and ``aprl`` at the narrowest
    useful width. Short branch names pass through unchanged.
    """
    if max_length <= 0:
        return ''
    if len(branch) <= max_length:
        return branch

    parts = [part for part in branch.split('/') if part]
    leaf = parts[-1] if parts else branch
    prefix = parts[-2] if len(parts) > 1 else ''
    words = [word for word in re.split(r'[-_.]+', leaf) if word]
    initialism = ''.join(word[0] for word in words) if len(words) > 1 else ''
    compact_leaf = initialism or leaf

    candidates = []
    if prefix:
        candidates.extend((f'{prefix}/{compact_leaf}',
                           f'{prefix[0]}/{compact_leaf}'))
    candidates.extend((compact_leaf, leaf))
    for candidate in candidates:
        if candidate and len(candidate) <= max_length:
            return candidate
    return compact_leaf[:max_length]


def _branch_field_width(client_width: str, session_name: str,
                        window_count: str) -> int:
    """Choose a fixed branch field width that leaves numeric tabs visible."""
    try:
        width = max(0, int(client_width))
        windows = max(0, int(window_count))
    except (TypeError, ValueError):
        return 4

    # Session badge + numeric tabs (including one run and one turn glyph each)
    # + the leading space and clock at right. Sequential windows above 9 need
    # one more index column. Reserve the compact pair line, the largest ordinary
    # sync indicator, and tmux's list arrows/spacing.
    tab_width = (windows * 6) + max(0, windows - 9)
    available = (
        width - (len(session_name) + 3) - tab_width - 18 - 13 - 9 - 2)
    for field_width in (10, 6, 4):
        if available >= field_width:
            return field_width
    return 0


def _tmux_status_context(pane_path: str, client_width: str,
                         session_name: str, window_count: str) -> None:
    """Print compact branch/sync context and its separator for status-right."""
    field_width = _branch_field_width(
        client_width, session_name, window_count)
    if field_width == 0 or not pane_path:
        return
    toplevel = _git_out(['rev-parse', '--show-toplevel'], cwd=pane_path)
    branch = None
    if toplevel:
        branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=toplevel)
    label = _compact_branch(branch or 'no-git', field_width)
    sys.stdout.write(label.ljust(field_width))
    _tmux_git_sync(pane_path)
    sys.stdout.write(' | ')


def _compute_window_label(workspace: Path) -> dict:
    """Compute label data for a workspace: branch, default, pr, label.

    Produces <workspace>#<pr> when an open PR exists, <workspace>/<branch>
    on a feature branch, and <workspace> on the default branch.
    """
    branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'],
                      cwd=workspace) or 'unknown'
    default = _default_branch(workspace)
    feature = branch != default and branch not in _DEFAULT_BRANCHES
    pr = None
    if feature:
        info = _get_pr_info(workspace, branch)
        if info and info.get('state') == 'open':
            pr = info.get('number')

    name = workspace.name
    if pr:
        label = f'{name}#{pr}'
    elif feature:
        label = f'{name}/{_shorten_branch(branch)}'
    else:
        label = name
    return {'branch': branch, 'default': default, 'pr': pr, 'label': label}


# --- turn indicator -----------------------------------------------------------

_TURN_CACHE_TTL = 60
_TURN_CACHE_VERSION = 2
_TURN_ACTIVE_TTL = 30
_TURN_LOCAL_TIMEOUT = 1.0
_TURN_LOOKUP_TIMEOUT = 3.0
_TURN_PRODUCER_DEADLINE = 6.0
_TURN_MAX_PARALLEL = 8
_TURN_TERMINAL_LIMIT = 30
_TURN_PROCESS_SNAPSHOT_MAX = 1024 * 1024
_TURN_PR_JSON_MAX = 256 * 1024
_TURN_STATE_DIR = Path(os.environ.get(
    'XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'hive' / 'turn'

_TURN_MARKERS = {
    'Handoff: approve': 'approve',
    'Handoff: changes-requested': 'changes requested',
    'Handoff: no-new-delta': 'no new delta',
    'Handoff: addressed': 'completion',
}
_TURN_NEGATION_RE = re.compile(
    r"\b(?:not|cannot|can't|isn't|aren't|won't|don't|never|withheld|"
    r'blocked|pending)\b', re.IGNORECASE)
_TURN_REVIEW_PREFIX_RE = re.compile(
    r'^(?:(?:[a-z0-9_-]+\s+){0,3}(?:review|re-review)'
    r'(?:\s+(?:at|on|of)\s+[0-9a-f]{7,40})?'
    r'(?:\s+disposition)?(?:\s*[:—-]\s*|\s+)'
    r'(?:[0-9a-f]{7,40}\s*[:—-]\s*)?)', re.IGNORECASE)


@dataclass
class _TurnWindow:
    """One tmux window and the bounded turn facts derived for it."""

    window_id: str
    index: int
    pane_id: str
    pane_pid: int
    pane_tty: str
    pane_path: str
    pane_command: str
    activity: float
    declared: str = ''
    pane_title: str = ''
    agent: str = ''
    workspace: Path | None = None
    remote: str = ''
    branch: str = ''
    head: str = ''
    role: str = 'unknown'
    role_source: str = 'none'
    eligible: bool = False
    question: bool = False
    reason: str = ''
    clear_declaration: bool = False

    @property
    def pair_key(self) -> tuple[str, str] | None:
        """The group identity when the window has enough local git facts."""
        if not self.remote or not self.branch:
            return None
        return self.remote, self.branch


def _turn_process_snapshot(ttys: set[str]) -> dict[int, dict] | None:
    """Read one process snapshot limited to the supplied pane TTYs.

    Raw argv is retained only in this return value. Callers reduce it to an
    agent kind before any cache or tmux option is written.
    """
    names = sorted({tty.removeprefix('/dev/') for tty in ttys if tty})
    if not names:
        return {}
    try:
        result = subprocess.run(
            ['ps', '-t', ','.join(names), '-o',
             'pid=,ppid=,tty=,comm=,args='],
            capture_output=True, text=True, timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if len(result.stdout.encode(errors='replace')) > _TURN_PROCESS_SNAPSHOT_MAX:
        return None

    records: dict[int, dict] = {}
    try:
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 4)
            if len(fields) < 4:
                raise ValueError('short ps row')
            pid, ppid = int(fields[0]), int(fields[1])
            records[pid] = {
                'pid': pid,
                'ppid': ppid,
                'tty': fields[2],
                'comm': fields[3],
                'args': fields[4] if len(fields) == 5 else fields[3],
            }
    except (TypeError, ValueError):
        return None
    return records


def _turn_descendants(root_pid: int, records: dict[int, dict]) -> list[dict]:
    """Return the root and every descendant in a process snapshot."""
    children: dict[int, list[int]] = {}
    for record in records.values():
        children.setdefault(record['ppid'], []).append(record['pid'])
    found = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        record = records.get(pid)
        if record is not None:
            found.append(record)
        pending.extend(children.get(pid, []))
    return found


def _turn_codex_package_path(path: Path) -> bool:
    """True when a resolved path sits under node_modules/@openai/codex."""
    parts = path.parts
    return any(parts[i:i + 3] == ('node_modules', '@openai', 'codex')
               for i in range(max(0, len(parts) - 2)))


def _turn_resolved_path(value: str) -> Path | None:
    """Resolve one process argv path without guessing relative commands."""
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _turn_process_agent(root_pid: int, records: dict[int, dict]) -> str | None:
    """Identify exactly one supported agent below a tmux pane shell."""
    agents: set[str] = set()
    for record in _turn_descendants(root_pid, records):
        comm = Path(record.get('comm', '')).name
        try:
            argv = shlex.split(record.get('args', ''))
        except ValueError:
            continue
        argv0 = Path(argv[0]).name if argv else ''
        if comm == 'claude' or argv0 == 'claude':
            agents.add('claude')
            continue

        if comm == 'node' or argv0 == 'node':
            if len(argv) < 2:
                continue
            entry = _turn_resolved_path(argv[1])
            if (entry is not None and _turn_codex_package_path(entry)
                    and entry.name == 'codex.js'
                    and entry.parent.name == 'bin'):
                agents.add('codex')
            continue

        executable = _turn_resolved_path(argv[0]) if argv else None
        if (comm == 'codex' or argv0 == 'codex') and executable is not None:
            if _turn_codex_package_path(executable):
                agents.add('codex')
    return next(iter(agents)) if len(agents) == 1 else None


def _turn_pair_binding(pair_key: tuple[str, str]) -> str:
    """Stable declaration binding for a normalized remote and branch."""
    return f'{pair_key[0]}#{pair_key[1]}'


def _turn_remote_host(remote: str) -> str:
    """Extract a normalized host from URL and scp-style git remotes."""
    if '://' in remote:
        return (urlparse(remote).hostname or '').lower()
    authority = remote.split(':', 1)[0]
    return authority.rsplit('@', 1)[-1].lower()


def _turn_declared_role(value: str,
                        pair_key: tuple[str, str]) -> str | None:
    """Return a declaration only when its embedded pair key still matches."""
    try:
        role, binding = value.split(' ', 1)
    except ValueError:
        return None
    if role not in ('implementer', 'reviewer'):
        return None
    return role if binding == _turn_pair_binding(pair_key) else None


def _derive_turn_role(workspace: Path, remote: str, branch: str,
                      default: str) -> str | None:
    """Derive checkout provenance from the branch's oldest reflog subject."""
    reflog = _git_out(
        ['reflog', 'show', '--format=%gs', f'refs/heads/{branch}'],
        cwd=workspace, timeout=_TURN_LOCAL_TIMEOUT)
    if not reflog:
        return None
    oldest = next((line.strip() for line in reversed(reflog.splitlines())
                   if line.strip()), '')
    if oldest in (f'branch: Created from {default}',
                  f'branch: Created from refs/heads/{default}',
                  'branch: Created from HEAD'):
        return 'implementer'

    reviewer_sources = (
        f'branch: Created from origin/{branch}',
        f'branch: Created from refs/remotes/origin/{branch}',
    )
    if oldest in reviewer_sources:
        return 'reviewer'
    lowered = oldest.lower()
    if (_turn_remote_host(remote) == 'github.com'
            and lowered.startswith('fetch ')
            and (f'/{branch.lower()}' in lowered
                 or 'refs/pull/' in lowered)):
        return 'reviewer'
    return None


def _turn_marker_lines(body: str) -> list[str]:
    """Return exact, non-fenced marker lines among the last three lines."""
    lines: list[tuple[str, bool]] = []
    in_fence = False
    fence = ''
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith(('```', '~~~')):
            token = stripped[:3]
            if not in_fence:
                in_fence, fence = True, token
            elif token == fence:
                in_fence, fence = False, ''
            if stripped:
                lines.append((raw, True))
            continue
        if stripped:
            lines.append((raw, in_fence))
    markers = []
    for raw, fenced in lines[-3:]:
        if not fenced and raw == raw.strip() and raw in _TURN_MARKERS:
            markers.append(raw)
    return markers


def _turn_first_line(body: str) -> str:
    """Return the first non-empty comment line, bounded for storage/display."""
    return _turn_bounded_text(next((line for line in body.splitlines()
                                    if line.strip()), ''), 80)


def _turn_bounded_text(value: object, limit: int = 80) -> str:
    """Make remote or pane text safe for a terminal and bounded storage."""
    printable = ''.join(character if character.isprintable() else ' '
                        for character in str(value or ''))
    return ' '.join(printable.split())[:limit]


def _turn_normalize_line(line: str) -> str:
    """Normalize Markdown decoration without changing the prose contract."""
    line = re.sub(r'^#{1,6}\s*', '', line.strip())
    return line.replace('**', '').replace('__', '').replace('`', '').lower()


def _turn_strip_review_heading(line: str) -> str:
    """Remove one bounded review heading, if present."""
    match = _TURN_REVIEW_PREFIX_RE.match(line)
    return line[match.end():].lstrip() if match else line


def _turn_approval_form(line: str) -> bool:
    """Recognize only the ADR's finite set of complete approval lines."""
    if re.fullmatch(r'no blocking (?:findings|issues)\.?', line):
        return True
    if re.fullmatch(
            r'reviewed locally\. no blocking (?:findings|issues)\.?', line):
        return True
    if re.fullmatch(
            r'reviewed the pr diff in \S+\. no blocking '
            r'(?:findings|issues)\.?', line):
        return True

    verdict_line = _turn_strip_review_heading(line)
    match = re.match(r'^(approved\s*/\s*lgtm|approved|approve|lgtm)\b',
                     verdict_line)
    if not match:
        return False
    tail = verdict_line[match.end():]
    if re.fullmatch(r'[\s.,;:!)/-]*', tail):
        return True
    if tail == ', confirming the standing approval at this head.':
        return True
    nit_tail = (r'(?:,|\.|\s+with)\s+one\s+(?:non-blocking\s+)?nit'
                r'(?:\s+below)?[.!]?')
    if re.fullmatch(nit_tail, tail):
        return True
    head_tail = (
        r'\s+(?:at|on)\s+(?:exact\s+)?(?:rebased\s+)?(?:head\s+)?'
        r'[0-9a-f]{7,40}(?:\s+against\s+base\s+[0-9a-f]{7,40})?'
        r'[.,;:!)]*')
    return (re.fullmatch(head_tail, tail) is not None
            or re.fullmatch(head_tail + nit_tail, tail) is not None)


def _turn_changes_form(line: str) -> bool:
    """Recognize an anchored changes-requested disposition."""
    line = _turn_strip_review_heading(line)
    return re.match(
        r'^changes(?:\s+\S+){0,2}\s+requested(?:$|[\s.,;:!)/])', line) \
        is not None


def _turn_no_delta_form(line: str) -> bool:
    """Recognize an anchored no-new-delta disposition."""
    line = _turn_strip_review_heading(line)
    return re.match(r'^no new delta(?:$|[\s.,;:!)/])', line) is not None


def _turn_completion_form(line: str) -> bool:
    """Recognize the bounded completion forms observed in the PR corpus."""
    sha = r'[0-9a-f]{7,40}'
    end = r'(?=$|[\s.,;:!)/])'
    patterns = (
        rf'^(?:\S+\s+){{0,6}}addressed\s+in\s+{sha}{end}',
        rf'^addressed(?:\s+\S+){{0,6}}\s+in\s+{sha}{end}',
        rf'^fixed\s+in\s+{sha}{end}',
        rf'^pushed\s+{sha}{end}',
        rf'\band\s+pushed\s+{sha}{end}',
        rf'\bnew\s+head\s+{sha}(?:$|[,;. )]|\s+ci\b)',
    )
    if any(re.search(pattern, line) for pattern in patterns):
        return True
    return (line.startswith('addressed') and len(line) <= 80
            and re.search(r'\bin the latest push(?:$|[.,;:!])', line)
            is not None)


def _classify_turn_comment(body: str) -> str:
    """Classify one PR comment without allowing ambiguous merge directions."""
    markers = _turn_marker_lines(body)
    if len(markers) > 1:
        return 'unrecognized'
    if len(markers) == 1:
        return _TURN_MARKERS[markers[0]]

    first = next((line.strip()[:2000] for line in body.splitlines()
                  if line.strip()), '')
    if not first:
        return 'unrecognized'
    line = _turn_normalize_line(first)
    if _TURN_NEGATION_RE.search(line):
        return 'unrecognized'

    families = {
        'approval': bool(re.search(
            r'\b(?:approve|approved|approval|lgtm)\b|'
            r'\bno blocking (?:findings|issues)\b', line)),
        'changes': bool(re.search(
            r'\bchanges(?:\s+\S+){0,2}\s+requested\b', line)),
        'no-delta': bool(re.search(r'\bno new delta\b', line)),
        'completion': bool(re.search(
            r'\b(?:addressed|fixed|pushed)\b|\bnew head\b', line)),
    }
    if sum(families.values()) != 1:
        return 'unrecognized'
    if families['approval'] and _turn_approval_form(line):
        return 'approve'
    if families['changes'] and _turn_changes_form(line):
        return 'changes requested'
    if families['no-delta'] and _turn_no_delta_form(line):
        return 'no new delta'
    if families['completion'] and _turn_completion_form(line):
        return 'completion'
    return 'unrecognized'


def _ensure_turn_state_dir() -> bool:
    """Create and re-tighten the private turn-state directory."""
    try:
        _TURN_STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(_TURN_STATE_DIR, 0o700)
        return True
    except OSError:
        return False


def _turn_cache_path(pair_key: tuple[str, str]) -> Path:
    """Return the non-revealing cache path for one pair key."""
    encoded = json.dumps(pair_key, separators=(',', ':')).encode()
    return _TURN_STATE_DIR / f'turn-{hashlib.sha256(encoded).hexdigest()}.json'


def _read_turn_cache(pair_key: tuple[str, str]) -> dict | None:
    """Read a cache entry only when its full key matches the requested key."""
    path = _turn_cache_path(pair_key)
    try:
        if path.stat().st_size > 65536:
            return None
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get('pair') != list(pair_key):
        return None
    return data


def _write_private_json(path: Path, data: dict) -> bool:
    """Atomically write one 0600 JSON file under the 0700 state directory."""
    if not _ensure_turn_state_dir():
        return False
    temporary = path.with_name(
        f'.{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}')
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            fd = None
            json.dump(data, stream, separators=(',', ':'), sort_keys=True)
            stream.write('\n')
            stream.flush()
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return True
    except OSError:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def _bounded_turn_pr(result: dict) -> dict:
    """Reduce a remote/cache result to the fields allowed on disk."""
    try:
        number = int(result.get('number') or 0)
    except (TypeError, ValueError):
        number = 0
    state = str(result.get('state') or 'unknown').lower()
    if state not in ('open', 'closed', 'merged', 'none', 'unknown'):
        state = 'unknown'
    bounded = {
        'state': state,
        'reason': _turn_bounded_text(result.get('reason', '')),
        'number': number,
        'title': _turn_bounded_text(result.get('title', '')),
        'head_sha': str(result.get('head_sha') or '')[:40],
    }
    comment = result.get('comment')
    if isinstance(comment, dict):
        classification = str(
            comment.get('classification') or 'unrecognized')
        if classification not in (
                'approve', 'changes requested', 'no new delta',
                'completion', 'unrecognized'):
            classification = 'unrecognized'
        bounded['comment'] = {
            'id': str(comment.get('id') or '')[:100],
            'classification': classification,
            'first_line': _turn_bounded_text(comment.get('first_line', '')),
            'created_at': str(comment.get('created_at') or '')[:40],
        }
    return bounded


def _cached_turn_pr(pair_key: tuple[str, str], heads: tuple[str, ...],
                    now: float) -> dict | None:
    """Resolve an exact merged receipt or a fresh mutable observation."""
    data = _read_turn_cache(pair_key)
    if data is None or data.get('version') != _TURN_CACHE_VERSION:
        return None
    if len(heads) == 1:
        receipt = data.get('merged_receipt')
        if (isinstance(receipt, dict)
                and receipt.get('head_sha') == heads[0]
                and receipt.get('state') == 'merged'):
            return _bounded_turn_pr(receipt)
    mutable = data.get('mutable')
    if not isinstance(mutable, dict):
        return None
    try:
        age = now - float(mutable['observed_at'])
    except (KeyError, TypeError, ValueError):
        return None
    if age < 0 or age >= _TURN_CACHE_TTL:
        return None
    if mutable.get('heads') != list(heads):
        return None
    result = mutable.get('result')
    if not isinstance(result, dict):
        return None
    bounded = _bounded_turn_pr(result)
    bounded['_observed_at'] = float(mutable['observed_at'])
    return bounded


def _save_turn_pr(pair_key: tuple[str, str], heads: tuple[str, ...],
                  result: dict, now: float) -> None:
    """Persist one mutable result or one immutable merged receipt."""
    bounded = _bounded_turn_pr(result)
    old = _read_turn_cache(pair_key) or {}
    data: dict = {'version': _TURN_CACHE_VERSION, 'pair': list(pair_key)}
    old_receipt = old.get('merged_receipt')
    if (old.get('version') == _TURN_CACHE_VERSION
            and len(heads) == 1 and isinstance(old_receipt, dict)
            and old_receipt.get('head_sha') == heads[0]
            and old_receipt.get('state') == 'merged'):
        data['merged_receipt'] = old_receipt
    if bounded['state'] == 'merged' and len(heads) == 1:
        data['merged_receipt'] = bounded
    elif result.get('_cacheable', True):
        data['mutable'] = {
            'observed_at': now,
            'heads': list(heads),
            'result': bounded,
        }
    _write_private_json(_turn_cache_path(pair_key), data)


def _bust_turn_mutable_cache() -> None:
    """Remove mutable observations while preserving immutable receipts."""
    if not _ensure_turn_state_dir():
        return
    try:
        paths = list(_TURN_STATE_DIR.glob('turn-*.json'))
    except OSError:
        return
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                continue
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or 'mutable' not in data:
            continue
        data.pop('mutable', None)
        _write_private_json(path, data)


def _turn_cli_for_remote(remote: str) -> str:
    """Choose the repository's native PR CLI without crossing host APIs."""
    return 'gh' if _turn_remote_host(remote) == 'github.com' else 'fj'


def _run_turn_pr_cli(cli: str, repo_path: Path, args: list[str],
                     deadline: float) -> list | dict | None:
    """Run one JSON PR command within both lookup deadlines."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(
            [cli, 'pr', *args], cwd=repo_path, capture_output=True, text=True,
            timeout=min(_TURN_LOOKUP_TIMEOUT, remaining))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if len(result.stdout.encode(errors='replace')) > _TURN_PR_JSON_MAX:
        return None
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, (list, dict)) else None


def _turn_has_merge_timestamp(value: object) -> bool:
    """Accept a real merge time while rejecting Forgejo's year-one zero."""
    if not value:
        return False
    try:
        return datetime.fromisoformat(str(value)).year > 1
    except (TypeError, ValueError):
        return False


def _turn_pr_state(pr: dict) -> str:
    """Normalize GitHub/Forgejo state fields."""
    if pr.get('merged') is True:
        return 'merged'
    state = str(pr.get('state') or '').lower()
    merged_at = pr.get('mergedAt') or pr.get('merged_at')
    if (pr.get('merged') is not False
            and _turn_has_merge_timestamp(merged_at)):
        return 'merged'
    return state if state in ('open', 'closed', 'merged') else 'unknown'


def _latest_turn_comment(comments: object) -> dict | None:
    """Reduce the latest API comment without retaining its raw body."""
    if not isinstance(comments, list) or not comments:
        return None
    usable = [comment for comment in comments if isinstance(comment, dict)]
    if not usable:
        return None
    latest = max(usable, key=lambda comment: str(
        comment.get('createdAt') or comment.get('created_at') or ''))
    body = str(latest.get('body') or '')
    return {
        'id': latest.get('id') or latest.get('databaseId') or '',
        'classification': _classify_turn_comment(body),
        'first_line': _turn_first_line(body),
        'created_at': latest.get('createdAt')
        or latest.get('created_at') or '',
    }


def _lookup_turn_pr(repo_path: Path, pair_key: tuple[str, str],
                    heads: tuple[str, ...], deadline: float) -> dict:
    """Resolve live PR state conservatively for one remote/branch group."""
    cli = _turn_cli_for_remote(pair_key[0])
    fields = 'number,title,state,headRefOid,updatedAt,mergedAt'
    if cli == 'fj':
        fields += ',merged'
    open_prs = _run_turn_pr_cli(
        cli, repo_path,
        ['list', '--head', pair_key[1], '--state', 'open', '--limit', '2',
         '--json', fields], deadline)
    if not isinstance(open_prs, list):
        return {'state': 'unknown', 'reason': 'lookup failed',
                '_cacheable': False}
    if len(open_prs) >= 2:
        return {'state': 'unknown', 'reason': 'multiple open PRs'}
    if len(open_prs) == 1:
        number = open_prs[0].get('number')
        if not number:
            return {'state': 'unknown', 'reason': 'invalid open PR',
                    '_cacheable': False}
        view_fields = 'number,title,state,headRefOid,mergedAt,comments'
        view_jq = ('{number,title,state,headRefOid,mergedAt,'
                   'comments:(.comments[-1:] // [])}')
        if cli == 'fj':
            view_fields += ',merged'
            view_jq = ('{number,title,state,headRefOid,mergedAt,merged,'
                       'comments:(.comments[-1:] // [])}')
        viewed = _run_turn_pr_cli(
            cli, repo_path,
            ['view', str(number), '--json', view_fields, '--jq', view_jq],
            deadline)
        if not isinstance(viewed, dict):
            return {'state': 'unknown', 'reason': 'lookup failed',
                    '_cacheable': False}
        if _turn_pr_state(viewed) != 'open':
            return {'state': 'unknown', 'reason': 'PR changed during lookup',
                    '_cacheable': False}
        return {
            'state': 'open',
            'number': viewed.get('number') or number,
            'title': viewed.get('title') or open_prs[0].get('title') or '',
            'head_sha': viewed.get('headRefOid') or '',
            'comment': _latest_turn_comment(viewed.get('comments')),
        }

    if len(heads) != 1:
        return {'state': 'unknown', 'reason': 'mixed local HEADs'}
    terminal_args = ['list']
    if cli == 'gh':
        terminal_args += ['--head', pair_key[1]]
    terminal_args += [
        '--state', 'all', '--limit', str(_TURN_TERMINAL_LIMIT),
        '--json', fields]
    if cli == 'gh':
        terminal_args += ['--search', 'sort:updated-desc']
    terminal = _run_turn_pr_cli(
        cli, repo_path, terminal_args, deadline)
    if not isinstance(terminal, list):
        return {'state': 'unknown', 'reason': 'lookup failed',
                '_cacheable': False}
    matching = [pr for pr in terminal
                if isinstance(pr, dict) and pr.get('headRefOid') == heads[0]]
    if cli == 'fj' and len(matching) > 1:
        return {'state': 'unknown', 'reason': 'multiple PRs at local HEAD'}
    for pr in matching:
        state = _turn_pr_state(pr)
        if state == 'open':
            return {'state': 'unknown', 'reason': 'PR changed during lookup',
                    '_cacheable': False}
        if state not in ('closed', 'merged'):
            return {'state': 'unknown', 'reason': 'invalid terminal PR',
                    '_cacheable': False}
        return {
            'state': state,
            'number': pr.get('number') or 0,
            'title': pr.get('title') or '',
            'head_sha': heads[0],
        }
    if len(terminal) >= _TURN_TERMINAL_LIMIT:
        return {'state': 'unknown', 'reason': 'terminal history truncated'}
    return {'state': 'none'}


def _collect_turn_windows(session: str) -> list[_TurnWindow] | None:
    """Read the active pane facts for every window in one tmux session."""
    fmt = '\t'.join((
        '#{window_id}', '#{window_index}', '#{pane_id}', '#{pane_pid}',
        '#{pane_tty}', '#{pane_current_path}', '#{pane_current_command}',
        '#{window_activity}', '#{@hive_turn_role_declared}', '#{pane_title}',
    ))
    try:
        result = subprocess.run(
            ['tmux', 'list-windows', '-t', session, '-F', fmt],
            capture_output=True, text=True, timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    windows = []
    try:
        for line in result.stdout.splitlines():
            fields = line.split('\t', 9)
            if len(fields) != 10:
                raise ValueError('short tmux row')
            windows.append(_TurnWindow(
                window_id=fields[0],
                index=int(fields[1]),
                pane_id=fields[2],
                pane_pid=int(fields[3]),
                pane_tty=fields[4],
                pane_path=fields[5],
                pane_command=fields[6],
                activity=float(fields[7] or 0),
                declared=fields[8],
                pane_title=fields[9][:80],
            ))
    except (TypeError, ValueError):
        return None
    return windows


def _populate_turn_window(window: _TurnWindow,
                          records: dict[int, dict] | None) -> None:
    """Attach agent, git, and role facts to one collected tmux window."""
    if records is None:
        window.reason = 'process snapshot failed'
        return
    window.agent = _turn_process_agent(window.pane_pid, records) or ''
    if not window.agent:
        window.reason = 'no supported agent'
        return
    toplevel = _git_out(
        ['rev-parse', '--show-toplevel'], cwd=window.pane_path,
        timeout=_TURN_LOCAL_TIMEOUT)
    if not toplevel:
        window.reason = 'not a git checkout'
        return
    window.workspace = Path(toplevel)
    window.remote = _get_origin_url(
        window.workspace, timeout=_TURN_LOCAL_TIMEOUT) or ''
    window.branch = _git_out(
        ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=window.workspace,
        timeout=_TURN_LOCAL_TIMEOUT) or ''
    window.head = _git_out(
        ['rev-parse', 'HEAD'], cwd=window.workspace,
        timeout=_TURN_LOCAL_TIMEOUT) or ''
    if (not window.remote or not window.branch or window.branch == 'HEAD'
            or not window.head):
        window.reason = 'incomplete git identity'
        return
    default = _default_branch(window.workspace, timeout=_TURN_LOCAL_TIMEOUT)
    if window.branch == default:
        window.reason = 'default branch'
        return
    window.eligible = True

    pair_key = window.pair_key
    assert pair_key is not None
    declared = _turn_declared_role(window.declared, pair_key)
    if declared:
        window.role = declared
        window.role_source = 'declared'
        return
    if window.declared:
        window.clear_declaration = True
    derived = _derive_turn_role(
        window.workspace, window.remote, window.branch, default)
    if derived:
        window.role = derived
        window.role_source = 'reflog'


def _turn_group_shape(windows: list[_TurnWindow]) -> str:
    """Classify a key group without silently selecting a pair subset."""
    if len(windows) == 1:
        return 'singleton'
    roles = sorted(window.role for window in windows)
    if len(windows) == 2 and roles == ['implementer', 'reviewer']:
        return 'pair'
    return 'malformed'


def _turn_unknown_decision(windows: list[_TurnWindow], reason: str) -> dict:
    """Build a no-glyph decision with a bounded diagnostic."""
    return {
        'suffixes': {window.window_id: '' for window in windows},
        'target': '',
        'verb': '',
        'kind': 'unknown',
        'reason': reason[:80],
    }


def _decide_turn(windows: list[_TurnWindow], pr: dict, now: float) -> dict:
    """Apply the ADR-0003 baton table to one group."""
    suffixes = {window.window_id: '' for window in windows}
    state = pr.get('state', 'unknown')
    if state in ('merged', 'closed'):
        return {
            'suffixes': {window.window_id: '↩' for window in windows},
            'target': '', 'verb': 'put back', 'kind': 'terminal',
            'reason': state,
        }

    questions = [window for window in windows if window.question]
    if questions:
        for window in questions:
            suffixes[window.window_id] = '?'
        return {
            'suffixes': suffixes, 'target': questions[0].window_id,
            'verb': 'answer question', 'kind': 'question',
            'reason': 'question',
        }

    if state == 'unknown':
        return _turn_unknown_decision(
            windows, str(pr.get('reason') or 'PR state unknown'))

    shape = _turn_group_shape(windows)
    comment = pr.get('comment') if state == 'open' else None
    classification = (comment or {}).get('classification') if comment else None

    target_role = ''
    glyph = ''
    verb = ''
    kind = classification or 'idle'
    if classification == 'approve':
        target_role, glyph, verb = 'implementer', '✓', 'merge pr'
    elif classification in ('changes requested', 'no new delta'):
        target_role, glyph, verb = (
            'implementer', '▶', 'address review feedback')
    elif classification == 'completion':
        target_role, glyph, verb = 'reviewer', '▶', 're-review'
    elif classification:
        return _turn_unknown_decision(windows, 'unrecognized handoff')

    if target_role:
        if shape == 'malformed':
            return _turn_unknown_decision(windows, f'{shape} group')
        targets = [window for window in windows if window.role == target_role]
        if len(targets) != 1:
            return _turn_unknown_decision(
                windows, f'next: {target_role} — no window')
        target = targets[0]
        suffixes[target.window_id] = glyph
        return {
            'suffixes': suffixes, 'target': target.window_id,
            'verb': verb, 'kind': kind, 'reason': '',
        }

    if shape == 'malformed':
        return _turn_unknown_decision(windows, 'malformed group')
    if shape == 'singleton':
        return {
            'suffixes': suffixes, 'target': '', 'verb': '',
            'kind': 'idle', 'reason': 'unpaired',
        }
    active = [window for window in windows
              if max(0, now - window.activity) < _TURN_ACTIVE_TTL]
    if active:
        return {
            'suffixes': suffixes, 'target': '', 'verb': '',
            'kind': 'idle', 'reason': 'active',
        }
    earliest = min(window.activity for window in windows)
    targets = [window for window in windows if window.activity == earliest]
    if len(targets) != 1:
        return _turn_unknown_decision(windows, 'equal quiet-since')
    target = targets[0]
    suffixes[target.window_id] = '▶'
    return {
        'suffixes': suffixes, 'target': target.window_id,
        'verb': 'continue', 'kind': 'quiet', 'reason': '',
    }


def _resolve_turn_prs(groups: dict[tuple[str, str], list[_TurnWindow]],
                      started: float, now: float) -> dict[tuple[str, str], dict]:
    """Resolve uncached group PRs in parallel under one producer deadline."""
    resolved: dict[tuple[str, str], dict] = {}
    misses: dict[tuple[str, str], tuple[tuple[str, ...], Path]] = {}
    for pair_key, windows in groups.items():
        heads = tuple(sorted({window.head for window in windows}))
        cached = _cached_turn_pr(pair_key, heads, now)
        if cached is not None:
            resolved[pair_key] = cached
            continue
        workspace = next((window.workspace for window in windows
                          if window.workspace is not None), None)
        if workspace is None:
            resolved[pair_key] = {
                'state': 'unknown', 'reason': 'workspace unavailable'}
            continue
        misses[pair_key] = heads, workspace

    if not misses:
        return resolved
    deadline = started + _TURN_PRODUCER_DEADLINE
    executor = ThreadPoolExecutor(
        max_workers=min(_TURN_MAX_PARALLEL, len(misses)))
    futures = {
        executor.submit(_lookup_turn_pr, workspace, pair_key, heads, deadline):
        (pair_key, heads)
        for pair_key, (heads, workspace) in misses.items()
    }
    remaining = max(0, deadline - time.monotonic())
    done, pending = wait(futures, timeout=remaining)
    for future in done:
        pair_key, heads = futures[future]
        try:
            result = future.result()
        except Exception:
            result = {'state': 'unknown', 'reason': 'lookup failed',
                      '_cacheable': False}
        cacheable = result.get('_cacheable', True)
        result = _bounded_turn_pr(result)
        if not cacheable:
            result['_cacheable'] = False
        result['_observed_at'] = now
        resolved[pair_key] = result
        _save_turn_pr(pair_key, heads, result, now)
    for future in pending:
        pair_key, _heads = futures[future]
        future.cancel()
        resolved[pair_key] = {
            'state': 'unknown', 'reason': 'producer deadline',
            '_cacheable': False, '_observed_at': now}
    executor.shutdown(wait=False, cancel_futures=True)
    return resolved


def _build_turn_session(session: str, now: float | None = None) -> dict | None:
    """Collect and decide all turn state for one tmux session."""
    started = time.monotonic()
    observed_at = time.time() if now is None else now
    _ensure_turn_state_dir()
    windows = _collect_turn_windows(session)
    if windows is None:
        return None
    records = _turn_process_snapshot({window.pane_tty for window in windows})
    for window in windows:
        _populate_turn_window(window, records)

    groups: dict[tuple[str, str], list[_TurnWindow]] = {}
    for window in windows:
        if window.eligible and window.pair_key is not None:
            groups.setdefault(window.pair_key, []).append(window)
    prs = _resolve_turn_prs(groups, started, observed_at)
    decisions = {}
    for pair_key, members in groups.items():
        pr = prs[pair_key]
        if pr.get('state') in ('closed', 'merged'):
            for window in members:
                if window.declared:
                    window.clear_declaration = True
        decisions[pair_key] = _decide_turn(members, pr, observed_at)
    return {
        'session': session,
        'observed_at': observed_at,
        'windows': windows,
        'groups': groups,
        'prs': prs,
        'decisions': decisions,
    }


def _set_turn_window_options(window: _TurnWindow, suffix: str,
                             target: str) -> None:
    """Rewrite all turn outputs for one window and clear stale declarations."""
    role = window.role if window.eligible else 'unknown'
    source = window.role_source if window.eligible else 'none'
    options = (
        ('@hive_turn_suffix', suffix),
        ('@hive_turn_role', role),
        ('@hive_turn_role_source', source),
        ('@hive_turn_target', target),
    )
    if window.clear_declaration:
        options += (('@hive_turn_role_declared', ''),)
    for option, value in options:
        subprocess.run(
            ['tmux', 'set-option', '-w', '-t', window.window_id,
             option, value], capture_output=True)


def _apply_turn_session(snapshot: dict) -> None:
    """Write one complete producer tick, including clears, to tmux."""
    by_window: dict[str, tuple[str, str]] = {}
    for pair_key, windows in snapshot['groups'].items():
        decision = snapshot['decisions'][pair_key]
        target = decision['target']
        target_window = next(
            (window for window in windows if window.window_id == target), None)
        target_value = ''
        if target_window is not None:
            target_value = f'{target_window.index}:{decision["verb"]}'[:80]
        elif decision['verb']:
            target_value = decision['verb'][:80]
        for window in windows:
            by_window[window.window_id] = (
                decision['suffixes'].get(window.window_id, ''), target_value)
    for window in snapshot['windows']:
        suffix, target = by_window.get(window.window_id, ('', ''))
        _set_turn_window_options(window, suffix, target)


def _turn_pair_line(snapshot: dict) -> str:
    """Render the first actionable group as a compact status-right line."""
    ordered = sorted(snapshot['groups'].items(),
                     key=lambda item: min(w.index for w in item[1]))
    for pair_key, windows in ordered:
        decision = snapshot['decisions'][pair_key]
        glyph_windows = [
            (window, decision['suffixes'].get(window.window_id, ''))
            for window in windows
            if decision['suffixes'].get(window.window_id, '')]
        if not glyph_windows:
            continue
        indices = '⇄'.join(str(window.index)
                           for window in sorted(windows, key=lambda w: w.index))
        pr = snapshot['prs'][pair_key]
        pr_part = f' #{pr["number"]}' if pr.get('number') else ''
        if len(glyph_windows) == 1:
            window, glyph = glyph_windows[0]
            return f'{indices}{pr_part} {glyph}{window.index}'
        glyph = glyph_windows[0][1]
        return f'{indices}{pr_part} {glyph}'
    return ''


def _tmux_turn_refresh(session: str, bust_cache: bool = False,
                       print_line: bool = True) -> bool:
    """Run one bounded session-wide turn producer tick."""
    if bust_cache:
        _bust_turn_mutable_cache()
    snapshot = _build_turn_session(session)
    if snapshot is None:
        return False
    _apply_turn_session(snapshot)
    if print_line:
        line = _turn_pair_line(snapshot)
        if line:
            sys.stdout.write(line)
    return True


def _format_turn_duration(age: float) -> str:
    """Format a nonnegative duration compactly for the pairs popup."""
    age = max(0, age)
    if age < 60:
        return f'{int(age)}s'
    if age < 3600:
        return f'{int(age // 60)}m'
    if age < 86400:
        return f'{int(age // 3600)}h'
    return f'{int(age // 86400)}d'


def _format_turn_age(created_at: str, now: float) -> str:
    """Format the age of an ISO comment timestamp for the pairs popup."""
    epoch = _parse_iso_timestamp(created_at)
    return '' if epoch is None else _format_turn_duration(now - epoch)


def _turn_shape_detail(windows: list[_TurnWindow]) -> str:
    """Name a group shape precisely enough to diagnose malformed groups."""
    shape = _turn_group_shape(windows)
    if shape != 'malformed':
        return shape
    if len(windows) > 2:
        return f'{len(windows)} windows'
    if any(window.role == 'unknown' for window in windows):
        return 'roles unknown'
    return ' + '.join(window.role for window in windows)


def _turn_liveness_text(window: _TurnWindow, now: float) -> str:
    """Describe liveness using only the sampled last-output timestamp."""
    seconds = max(0, now - window.activity)
    age = _format_turn_duration(seconds)
    if seconds < _TURN_ACTIVE_TTL:
        return f'{window.index}:active (output {age} ago)'
    return f'{window.index}:quiet since {age} ago'


def _turn_handoff_source(windows: list[_TurnWindow],
                         classification: str) -> str:
    """Name the unique window role that authors a recognized handoff."""
    source_role = {
        'approve': 'reviewer',
        'changes requested': 'reviewer',
        'no new delta': 'reviewer',
        'completion': 'implementer',
    }.get(classification)
    if not source_role:
        return ''
    sources = [window for window in windows if window.role == source_role]
    if len(sources) == 1:
        return f'{sources[0].index}:{source_role}'
    return source_role


def _tmux_pairs(session: str | None = None) -> bool:
    """Print a detailed, freshly evaluated pair table for one session."""
    session = session or _current_session()
    if not session:
        return False
    snapshot = _build_turn_session(session)
    if snapshot is None:
        return False
    _apply_turn_session(snapshot)
    print(f'Turn pairs in {_turn_bounded_text(session)}')
    print()
    grouped_ids = {
        window.window_id for windows in snapshot['groups'].values()
        for window in windows}
    if not snapshot['groups']:
        print('  (no participating groups)')
    ordered = sorted(snapshot['groups'].items(),
                     key=lambda item: min(w.index for w in item[1]))
    for pair_key, windows in ordered:
        windows = sorted(windows, key=lambda window: window.index)
        shape = _turn_shape_detail(windows)
        roles = ' + '.join(
            f'{window.index}:{window.role}({window.role_source})'
            for window in windows)
        pr = snapshot['prs'][pair_key]
        title = _turn_bounded_text(pr.get('title', ''))
        pr_text = (f' #{pr["number"]} {title}'
                   if pr.get('number') else '')
        print(f'  {shape} — {roles}{pr_text}'.rstrip())
        decision = snapshot['decisions'][pair_key]
        if decision['target']:
            target = next(window for window in windows
                          if window.window_id == decision['target'])
            print(f'            next {target.index}:{target.role} — '
                  f'{decision["verb"]}')
        elif decision['verb']:
            print(f'            next all — {decision["verb"]}')
        else:
            reason = decision['reason'] or decision['kind']
            observed_at = pr.get('_observed_at')
            if pr.get('state') == 'unknown' and observed_at is not None:
                age = _format_turn_duration(
                    snapshot['observed_at'] - observed_at)
                reason = f'unknown ({reason} {age} ago)'
            print(f'            {reason}')
        print('            ' + ', '.join(
            _turn_liveness_text(window, snapshot['observed_at'])
            for window in windows))
        comment = pr.get('comment')
        if comment:
            age = _format_turn_age(
                comment.get('created_at', ''), snapshot['observed_at'])
            suffix = f' ({age} ago)' if age else ''
            classification = comment.get('classification', 'unrecognized')
            source = _turn_handoff_source(windows, classification)
            source = f'{source} ' if source else ''
            first_line = _turn_bounded_text(comment.get('first_line', ''))
            print(f'            handoff {source}{classification}{suffix}: '
                  f'{first_line}')
    for window in sorted(snapshot['windows'], key=lambda item: item.index):
        if window.window_id in grouped_ids:
            continue
        summary = _turn_bounded_text(
            window.pane_title or window.pane_command, 50)
        reason = _turn_bounded_text(window.reason)
        print(f'  not participating — {window.index}:{summary} — {reason}')
    return True


def _tmux_role(role: str) -> bool:
    """Declare or clear the current window's turn role."""
    try:
        result = subprocess.run(
            ['tmux', 'display-message', '-p',
             '#{session_name}\t#{window_id}\t#{pane_current_path}'],
            capture_output=True, text=True, timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    fields = result.stdout.rstrip('\n').split('\t', 2)
    if len(fields) != 3:
        return False
    session, window_id, pane_path = fields
    value = ''
    branch = ''
    if role != 'clear':
        toplevel = _git_out(
            ['rev-parse', '--show-toplevel'], cwd=pane_path,
            timeout=_TURN_LOCAL_TIMEOUT)
        if not toplevel:
            return False
        workspace = Path(toplevel)
        remote = _get_origin_url(workspace, timeout=_TURN_LOCAL_TIMEOUT)
        branch = _git_out(
            ['rev-parse', '--abbrev-ref', 'HEAD'], cwd=workspace,
            timeout=_TURN_LOCAL_TIMEOUT) or ''
        if (not remote or not branch or branch == 'HEAD'
                or branch == _default_branch(
                    workspace, timeout=_TURN_LOCAL_TIMEOUT)):
            return False
        value = f'{role} {_turn_pair_binding((remote, branch))}'
    written = subprocess.run(
        ['tmux', 'set-option', '-w', '-t', window_id,
         '@hive_turn_role_declared', value], capture_output=True)
    if written.returncode != 0:
        return False
    _tmux_turn_refresh(session, print_line=False)
    if role == 'clear':
        print('Turn role declaration cleared.')
    else:
        print(f'Turn role declared: {role} ({branch})')
    return True


# --- run-dsl status integration -----------------------------------------------
#
# Surfaces run-dsl agent-job state per workspace inside `hive tmux`:
#   - the backtick+a popup (`hive tmux runs`) — a full per-workspace table
#   - a single-char suffix on each window label — only for attention-worthy
#     states (running / failed / interrupted), so the label stays quiet
#     when there's nothing to flag

_ACC_RUNS_DIR = Path.home() / '.local' / 'state' / 'acc-runs'
_RUN_HEARTBEAT_TTL = 300  # seconds — heartbeat older than this = interrupted
# Widen the live-scan mtime prefilter past the TTL: over-including a sidecar
# costs one JSON read, excluding a live one would drop it from the status bar.
_LIVE_SCAN_MTIME_MARGIN = 2


def _parse_iso_timestamp(ts: str | None) -> float | None:
    """Parse an ISO-8601 timestamp (with optional 'Z') into a Unix epoch."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _classify_run_state(sidecar: Path) -> str:
    """Classify a run-dsl sidecar as running / succeeded / failed / interrupted.

    - status.json present + success=true  → 'succeeded'
    - status.json present + success=false → 'failed'
    - status.json absent + heartbeat fresh → 'running'
    - status.json absent + heartbeat stale → 'interrupted'
    """
    status_file = sidecar / 'status.json'
    if status_file.is_file():
        try:
            status = json.loads(status_file.read_text())
        except (json.JSONDecodeError, OSError):
            return 'interrupted'
        return 'succeeded' if status.get('success') else 'failed'

    runtime_file = sidecar / 'runtime.json'
    if not runtime_file.is_file():
        return 'interrupted'
    try:
        runtime = json.loads(runtime_file.read_text())
    except (json.JSONDecodeError, OSError):
        return 'interrupted'
    hb = _parse_iso_timestamp(runtime.get('last_heartbeat_at'))
    if hb is None:
        return 'interrupted'
    return 'running' if (time.time() - hb) <= _RUN_HEARTBEAT_TTL else 'interrupted'


def _read_run_manifest(sidecar: Path, ws_resolved: Path) -> dict | None:
    """Read one sidecar's manifest, keeping it only if its work_dir sits at or
    under ``ws_resolved``. Returns None for anything unreadable or unrelated.

    The subtree test is what makes .local/<repo> clones visible: an exact
    work_dir match sees only runs launched at the workspace root, which is a
    minority of the work in a hive workspace.
    """
    manifest = sidecar / 'manifest.json'
    try:
        data = json.loads(manifest.read_text())
        mtime = manifest.stat().st_mtime
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    wd = data.get('work_dir')
    if not wd:
        return None
    try:
        wd_resolved = Path(wd).resolve()
    except OSError:
        return None
    if wd_resolved != ws_resolved and ws_resolved not in wd_resolved.parents:
        return None
    try:
        subpath = str(wd_resolved.relative_to(ws_resolved))
    except ValueError:
        return None
    objective = data.get('objective') or ''
    return {
        'work_dir': wd_resolved,
        'subpath': '' if subpath == '.' else subpath,
        'program': data.get('program', ''),
        'objective': objective.splitlines()[0] if objective else '',
        'sidecar': sidecar,
        'mtime': mtime,
    }


def _workspace_live_runs(workspace: Path) -> list[dict]:
    """Every run-dsl run currently live at or under ``workspace``.

    Cheap by construction, because this runs on every tmux window relabel: a
    live run rewrites runtime.json on each heartbeat, so its mtime is never
    older than its own last_heartbeat_at. A sidecar whose runtime.json mtime
    is already past the TTL therefore cannot be live, and is skipped on one
    stat() without opening either JSON file. The margin below only widens the
    candidate set — last_heartbeat_at stays the authority — so a slow write or
    a little clock skew costs a wasted read, never a missed run.
    """
    if not _ACC_RUNS_DIR.is_dir():
        return []
    try:
        ws_resolved = workspace.resolve()
    except OSError:
        return []
    cutoff = time.time() - (_RUN_HEARTBEAT_TTL * _LIVE_SCAN_MTIME_MARGIN)
    live = []
    try:
        # os.scandir over iterdir: this runs on every window relabel and the
        # sidecar dir holds every run ever recorded, so the per-entry Path
        # construction is the bulk of the work. Nothing here needs ordering.
        with os.scandir(_ACC_RUNS_DIR) as entries:
            for entry in entries:
                try:
                    if os.stat(os.path.join(entry.path, 'runtime.json')
                               ).st_mtime < cutoff:
                        continue
                except OSError:
                    continue  # no runtime.json — cannot be running
                sidecar = Path(entry.path)
                if _classify_run_state(sidecar) != 'running':
                    continue
                run = _read_run_manifest(sidecar, ws_resolved)
                if run is not None:
                    run['state'] = 'running'
                    live.append(run)
    except OSError:
        return []
    return live


def _subtree_run_states(workspace: Path) -> list[dict]:
    """Most-recent run-dsl run for every work_dir at or under ``workspace``.

    A hive workspace is not one work_dir: runs routinely happen in its
    ``.local/<repo>`` clones, and that work belongs to the workspace as much
    as work at its root does. Grouping by work_dir keeps each clone its own
    row rather than letting the busiest one speak for the whole subtree.

    Full scan of the sidecar dir — for the on-demand popup only. The window
    label uses _workspace_live_runs, which prunes first.
    """
    if not _ACC_RUNS_DIR.is_dir():
        return []
    try:
        ws_resolved = workspace.resolve()
    except OSError:
        return []
    try:
        sidecars = sorted(_ACC_RUNS_DIR.iterdir())
    except OSError:
        return []

    best: dict[Path, tuple[float, dict]] = {}
    for sidecar in sidecars:
        run = _read_run_manifest(sidecar, ws_resolved)
        if run is None:
            continue
        prior = best.get(run['work_dir'])
        if prior is None or run['mtime'] > prior[0]:
            run['state'] = _classify_run_state(sidecar)
            best[run['work_dir']] = (run['mtime'], run)
    return [run for _, run in sorted(best.values(), key=lambda e: -e[0])]


# Plain unicode (no ANSI): tmux rename-window takes plain text.
_RUN_LIVE_INDICATOR = '●'


def _run_state_label_suffix(workspace: Path) -> str:
    """Window-label suffix: '●' when exactly one run is live anywhere under
    ``workspace``, '●N' for N concurrent ones, '' when nothing is running.

    Only *live* state earns a place in the status bar. It is heartbeat-backed,
    so it self-clears and can never go stale; terminal states are history, and
    a single character cannot say which of a workspace's clones failed or why.
    That belongs in the backtick+a popup, which names both.
    """
    count = len(_workspace_live_runs(workspace))
    if count == 0:
        return ''
    return _RUN_LIVE_INDICATOR if count == 1 else f'{_RUN_LIVE_INDICATOR}{count}'


def _format_run_age(state: dict) -> str:
    """How long since the run last did something — compact: 30s / 8m / 2h / 3d.

    Uses status.json.timestamp for terminal states, last_heartbeat_at /
    started_at for live ones. Returns '' if no usable timestamp.
    """
    sidecar = state['sidecar']
    ts = None
    for name, key in (('status.json', 'timestamp'),
                      ('runtime.json', 'last_heartbeat_at'),
                      ('runtime.json', 'started_at')):
        if ts:
            break
        f = sidecar / name
        if f.is_file():
            try:
                ts = json.loads(f.read_text()).get(key)
            except (json.JSONDecodeError, OSError):
                ts = None
    epoch = _parse_iso_timestamp(ts)
    if epoch is None:
        return ''
    age = max(0, time.time() - epoch)
    if age < 60:
        return f'{int(age)}s'
    if age < 3600:
        return f'{int(age / 60)}m'
    if age < 86400:
        return f'{int(age / 3600)}h'
    return f'{int(age / 86400)}d'


def _tmux_runs(hive: Path) -> None:
    """Print a per-work_dir run-dsl status table — the backtick+a popup.

    One row per work_dir, not per workspace: a workspace's ``.local/<repo>``
    clones each get their own line, named by the subpath, so a failure points
    at the clone it happened in. Workspaces with no run-dsl record are skipped
    to keep the popup tight.

    This is where terminal states live. The window label deliberately carries
    only live runs (see _run_state_label_suffix) — a status-bar character
    cannot say which clone failed or why, and this table can.
    """
    rows = []
    for ws in _discover_workspaces(hive):
        for run in _subtree_run_states(ws):
            rows.append((ws, run))

    # The popup pipes through cat/less; ANSI passes through, so force colour on.
    C.force_enable()

    print(f'Runs in {_display_path(hive)}')
    print()
    if not rows:
        print(C.dim('  (no run-dsl runs found for any workspace)'))
        return

    icons = {
        'running':     ('●', C.cyan),
        'succeeded':   ('✓', C.green),
        'failed':      ('✗', C.bright_red),
        'interrupted': ('…', C.yellow),
    }

    def where(ws, run):
        return f'{ws.name}/{run["subpath"]}' if run['subpath'] else ws.name

    name_w = max(len(where(ws, run)) for ws, run in rows)
    prog_w = min(max(len(run['program']) for _, run in rows), 32)
    for ws, run in rows:
        char, colour = icons.get(run['state'], ('?', C.dim))
        prog = (run['program'] or '')[:prog_w]
        obj = run['objective'][:80] if run['objective'] else ''
        print(f'  {where(ws, run):<{name_w}}  {colour(char)} {run["state"]:<11} '
              f'{prog:<{prog_w}}  {_format_run_age(run):>4}  {C.dim(obj)}')


def _tmux_label_window(pane_path: str, window_id: str) -> None:
    """Rename a tmux window to reflect its workspace's git state.

    Results are cached under /tmp/hive-tmux/ for _LABEL_CACHE_TTL seconds
    and invalidated on branch change — the after-select-window/pane hooks
    fire often. Also refreshes the per-workspace PR cache the prompt
    segments read. Invoked by tmux hooks; fails silently.

    The label-base (workspace + branch/PR) is cached; the run-dsl suffix
    is recomputed each call (run state changes more dynamically than
    branches, and per-call scanning is cheap).
    """
    if not pane_path or not window_id:
        return
    pane = Path(pane_path)
    if not pane.is_dir():
        return

    toplevel = _git_out(['rev-parse', '--show-toplevel'], cwd=pane)
    if not toplevel:
        # Not a git repo — fall back to the directory basename.
        _set_tmux_window_status(window_id, '')
        subprocess.run(['tmux', 'rename-window', '-t', window_id, pane.name],
                       capture_output=True)
        return
    workspace = Path(toplevel)

    _TMUX_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _TMUX_DIR / f'{_label_cache_key(workspace)}.json'
    current_branch = _git_out(['rev-parse', '--abbrev-ref', 'HEAD'],
                              cwd=workspace)

    data = None
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            cached = None
        if cached is not None:
            fresh = time.time() - cached.get('ts', 0) <= _LABEL_CACHE_TTL
            if fresh and cached.get('branch') == current_branch:
                data = cached

    if data is None:
        data = _compute_window_label(workspace)
        data['ts'] = time.time()
        try:
            cache_file.write_text(json.dumps(data))
        except OSError:
            pass

    suffix = _run_state_label_suffix(workspace)
    label = f'{data["label"]} {suffix}' if suffix else data['label']
    _set_tmux_window_status(window_id, suffix)
    subprocess.run(['tmux', 'rename-window', '-t', window_id, label],
                   capture_output=True)

    # Refresh the per-workspace PR cache the prompt segments consume.
    number = _workspace_number(workspace.name)
    if number:
        hive_name = _short_name(workspace.parent)
        pr_cache = _TMUX_DIR / f'{hive_name}-{number}.pr'
        try:
            pr_cache.write_text(str(data['pr']) if data.get('pr') else '')
        except OSError:
            pass


def _set_tmux_window_status(window_id: str, run_suffix: str) -> None:
    """Apply compact tab formats to one concrete tmux window.

    These are window options in tmux, not session options. Applying them to
    each window avoids the old behavior where only the window current during
    a config reload received the hive-specific format.
    """
    options = (
        ('window-status-format', _WINDOW_STATUS_FORMAT),
        ('window-status-current-format', _WINDOW_STATUS_CURRENT_FORMAT),
        ('@hive_run_suffix', run_suffix),
    )
    for option, value in options:
        subprocess.run(
            ['tmux', 'set-window-option', '-t', window_id, option, value],
            capture_output=True)


def _tmux_refresh_labels(session: str) -> bool:
    """Refresh every window in one session; return false if it cannot be read."""
    if not session:
        return False
    r = subprocess.run(
        ['tmux', 'list-windows', '-t', session,
         '-F', '#{window_id}\t#{pane_current_path}'],
        capture_output=True, text=True)
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if '\t' not in line:
            continue
        window_id, pane_path = line.split('\t', 1)
        _tmux_label_window(pane_path, window_id)
    return True


# --- tmux subcommand ----------------------------------------------------------


def cmd_tmux(args: argparse.Namespace) -> None:
    """Execute the tmux subcommand."""
    action = getattr(args, 'tmux_action', None)

    # Hidden helper actions, invoked by the generated tmux config.
    if action == 'label-window':
        _tmux_label_window(args.pane_path, args.window_id)
        return
    if action == 'refresh-labels':
        if not _tmux_refresh_labels(args.session):
            sys.exit(1)
        return
    if action == 'turn-refresh':
        if not _tmux_turn_refresh(
                args.session, bust_cache=getattr(args, 'bust_cache', False)):
            sys.exit(1)
        return
    if action == 'pairs':
        if not _tmux_pairs(args.session):
            sys.exit(1)
        return
    if action == 'role':
        if not _tmux_role(args.role):
            print(f'{CROSS()} Could not set turn role', file=sys.stderr)
            sys.exit(1)
        return
    if action == 'status-context':
        _tmux_status_context(args.pane_path, args.client_width,
                             args.session_name, args.window_count)
        return
    if action == 'git-sync':
        _tmux_git_sync(args.pane_path)
        return
    if action == 'popup':
        _tmux_popup(
            getattr(args, 'cwd', None), args.command,
            client=getattr(args, 'client', None),
            pane=getattr(args, 'pane', None))
        return
    if action == 'runs':
        hive = _resolve_tmux_hive(getattr(args, 'hive_root', None))
        if hive is None:
            print(f'{CROSS()} Not inside a hive — pass --hive-root',
                  file=sys.stderr)
            sys.exit(1)
        _tmux_runs(hive)
        return

    if not _tmux_available():
        print(f'{CROSS()} tmux is not installed', file=sys.stderr)
        sys.exit(1)

    if action == 'restyle':
        _tmux_restyle()
        return

    if getattr(args, 'list_hives', False):
        _tmux_list()
        return

    hive_arg = getattr(args, 'hive', None)
    hive = _resolve_tmux_hive(hive_arg)
    if hive is None:
        if hive_arg:
            print(f'{CROSS()} Hive not found: {hive_arg}', file=sys.stderr)
        else:
            print(f'{CROSS()} Not inside a hive. Use --hive to specify one, '
                  f'or --list to see configured hives.', file=sys.stderr)
        sys.exit(1)

    _tmux_start(hive, _palette_for_mode(_hive_color(hive), _appearance_mode()),
                new_window=getattr(args, 'new_window', False))


def _tmux_list() -> None:
    """List configured hives with their assigned colors."""
    apiary = _load_apiary()
    if not apiary:
        print('No hives configured in the apiary.')
        print('Add one with: hive apiary add <path>')
        return
    print('Configured hives:')
    print()
    mode = _appearance_mode()
    for hive in apiary:
        color = _palette_for_mode(_hive_color(hive), mode)
        name = _short_name(hive)
        if C.enabled:
            badge = f'\033[38;2;{color["rgb"]}m{name:16}\033[0m'
        else:
            badge = f'{name:16}'
        print(f'  {badge}  {_display_path(hive)}  ({color["name"]})')


def _tmux_env_args(hive: Path, hive_name: str, color: dict,
                   number: str | None) -> list[str]:
    """Build `tmux new-session`/`new-window` `-e` args that seed the pane env.

    tmux's `set-environment` only updates the session environment — it cannot
    mutate the environment of an already-started shell. So the HIVE_* vars
    must be passed at pane-creation time with `-e` to actually reach the
    shell process. `HIVE_NUMBER` is per-window (the workspace number); the
    rest are constant across the hive.
    """
    args = [
        '-e', f'HIVE_ROOT={hive.resolve()}',
        '-e', f'HIVE_NAME={hive_name}',
        '-e', f'HIVE_COLOR={color["name"]}',
        '-e', f'HIVE_COLOR_RGB={color["rgb"]}',
        '-e', f'HIVE_COLOR_256={color["c256"]}',
    ]
    if number:
        args += ['-e', f'HIVE_NUMBER={number}']
    return args


def _used_workspaces(session: str, workspaces: list[Path]) -> set[Path]:
    """Workspaces in `workspaces` that already have a window in `session`.

    Pane paths are normalized to their containing workspace, so a pane
    sitting in a subdirectory of a workspace (e.g. after the user `cd`s
    into `widget-1/scripts`) still counts that workspace as used.
    """
    resolved = {ws: ws.resolve() for ws in workspaces}
    used: set[Path] = set()
    for pane in _windows_in_session(session):
        pane_resolved = Path(pane).resolve()
        for ws, ws_resolved in resolved.items():
            if pane_resolved == ws_resolved or ws_resolved in pane_resolved.parents:
                used.add(ws)
                break
    return used


def _tmux_restyle() -> None:
    """Re-apply mode-appropriate styling to every live hive tmux session.

    Invoked by `term-theme` after flipping the macOS appearance, so status
    bars, window tabs and pane borders follow the day/night switch without
    recreating sessions. The generated /tmp/hive-tmux/<name>.conf is
    rewritten too — the backtick+r reload binding sources it, so without
    the rewrite a reload would revert to the palette from session
    creation. New panes also see the mode's HIVE_COLOR_* env; shells that
    already exist keep the values they started with.
    """
    mode = _appearance_mode()
    r = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    rewritten: set[str] = set()
    for session in r.stdout.splitlines():
        env = subprocess.run(
            ['tmux', 'show-environment', '-t', session, 'HIVE_ROOT'],
            capture_output=True, text=True)
        if env.returncode != 0 or '=' not in env.stdout:
            continue
        hive = Path(env.stdout.strip().split('=', 1)[1])
        color = _palette_for_mode(_hive_color(hive), mode)
        if _short_name(hive) not in rewritten:
            _write_tmux_config(hive, color)
            rewritten.add(_short_name(hive))
        for _, opt, val in _style_option_pairs(color):
            subprocess.run(['tmux', 'set', '-t', session, opt, val])
        subprocess.run(['tmux', 'set-environment', '-t', session,
                        'HIVE_COLOR_RGB', color['rgb']])
        subprocess.run(['tmux', 'set-environment', '-t', session,
                        'HIVE_COLOR_256', color['c256']])


def _tmux_start(hive: Path, color: dict, new_window: bool) -> None:
    """Start a tmux session for the hive, or add a window to the current one."""
    name = _short_name(hive)
    sessions = _tmux_sessions()
    workspaces = _discover_workspaces(hive)

    def _env(ws: Path | None) -> list[str]:
        number = _workspace_number(ws.name) if ws is not None else None
        return _tmux_env_args(hive, name, color, number)

    current = _current_session()
    if current and current.startswith(f'{name}-'):
        # Already inside a session for this hive.
        if new_window:
            used = _used_workspaces(current, workspaces)
            for ws in workspaces:
                if ws not in used:
                    subprocess.run(['tmux', 'new-window', '-t', current,
                                    '-c', str(ws), *_env(ws)])
                    return
            # Every workspace already has a window — open one in the hive root.
            subprocess.run(['tmux', 'new-window', '-t', current,
                            '-c', str(hive), *_env(None)])
            return
        print(f'Already in {name} session: {current}')
        print('Use backtick+c for a new window, or detach first.')
        return

    config_path = _write_tmux_config(hive, color)
    session_name = f'{name}-{_next_session_num(name, sessions)}'
    first_ws = workspaces[0] if workspaces else None
    start_dir = str(first_ws) if first_ws else str(hive)

    if _group_exists(name, sessions):
        # Join the existing group — windows are shared, so don't recreate them.
        target = next(s for s in sessions if s.startswith(f'{name}-'))
        subprocess.run(
            ['tmux', 'new-session', '-d', '-t', target, '-s', session_name])
    else:
        subprocess.run(['tmux', 'new-session', '-d', '-s', session_name,
                        '-c', start_dir, *_env(first_ws)])
        for ws in workspaces[1:]:
            subprocess.run(['tmux', 'new-window', '-t', session_name,
                            '-c', str(ws), *_env(ws)])

    subprocess.run(['tmux', 'source-file', '-t', session_name, str(config_path)])

    _tmux_refresh_labels(session_name)
    _tmux_turn_refresh(session_name, print_line=False)

    subprocess.run(['tmux', 'select-window', '-t', f'{session_name}:1'],
                   capture_output=True)
    os.execlp('tmux', 'tmux', 'attach-session', '-t', session_name)


def _tmux_git_sync(pane_path: str) -> None:
    """Print an ahead/behind indicator for the tmux status bar.

    Output is a tmux-formatted string with a leading space (e.g. ' ↑2↓3'),
    or nothing when in sync / no upstream. Side effect: triggers a
    background `git fetch` at most once every 2 minutes per repo so the
    counts stay reasonably fresh. Invoked from status-right; fails silently.
    """
    if not pane_path:
        return
    toplevel = _git_out(['rev-parse', '--show-toplevel'], cwd=pane_path)
    if not toplevel:
        return
    # Needs an upstream to compare against.
    if _git_out(['rev-parse', '--abbrev-ref', '@{upstream}'],
                cwd=toplevel) is None:
        return
    counts = _git_out(
        ['rev-list', '--count', '--left-right', '@{upstream}...HEAD'],
        cwd=toplevel)
    if not counts:
        return
    parts = counts.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return
    behind, ahead = int(parts[0]), int(parts[1])
    if _appearance_mode() == 'day':
        diverged_c, behind_c, ahead_c = '#b45309', '#c2334d', '#3f6212'
    else:
        diverged_c, behind_c, ahead_c = '#ff9e64', '#ff7a93', '#a9dc76'
    if ahead and behind:
        sys.stdout.write(f' #[fg={diverged_c}]↑{ahead}↓{behind}#[default]')
    elif behind:
        sys.stdout.write(f' #[fg={behind_c}]↓{behind}#[default]')
    elif ahead:
        sys.stdout.write(f' #[fg={ahead_c}]↑{ahead}#[default]')

    # Background fetch if the per-repo marker is older than 2 minutes.
    _TMUX_DIR.mkdir(parents=True, exist_ok=True)
    key = str(Path(toplevel).resolve()).replace('/', '_')
    marker = _TMUX_DIR / f'.fetch{key}'
    now = time.time()
    last = 0.0
    if marker.is_file():
        try:
            last = float(marker.read_text().strip() or 0)
        except (ValueError, OSError):
            last = 0.0
    if now - last > 120:
        try:
            marker.write_text(str(now))
            subprocess.Popen(
                ['git', '-C', str(toplevel), 'fetch', '--quiet'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


def _tmux_popup(cwd: str | None, command: list[str],
                client: str | None = None, pane: str | None = None) -> None:
    """Run a command and show its output in a dynamically-sized tmux popup.

    Captures the command's output to a temp file, sizes the popup to fit
    (clamped to 80% of the window), and uses `less -R` when the content
    overflows. Invoked by the backtick keybindings.
    """
    target_args = []
    if client:
        target_args += ['-c', client]
    if pane:
        target_args += ['-t', pane]
    if not command:
        subprocess.run(
            ['tmux', 'display-message', *target_args,
             'hive tmux popup: no command'],
            capture_output=True)
        return

    import tempfile
    fd, tmpname = tempfile.mkstemp(prefix='hive-tmux-popup.')
    os.close(fd)
    tmpfile = Path(tmpname)
    with open(tmpfile, 'w') as out:
        try:
            subprocess.run(command, cwd=cwd or None,
                           stdout=out, stderr=subprocess.STDOUT)
        except (OSError, FileNotFoundError) as exc:
            out.write(f'hive tmux popup: {exc}\n')

    def _dim(fmt: str, default: int) -> int:
        r = subprocess.run(
            ['tmux', 'display-message', '-p', *target_args, fmt],
                           capture_output=True, text=True)
        try:
            return int(r.stdout.strip())
        except (ValueError, AttributeError):
            return default

    win_w = _dim('#{window_width}', 120)
    win_h = _dim('#{window_height}', 40)
    pop_w = max(40, min(120, win_w * 80 // 100))
    line_count = len(tmpfile.read_text(errors='replace').splitlines())
    pop_h = max(5, line_count + 2)
    max_h = max(5, win_h * 80 // 100)
    if pop_h > max_h:
        # Content overflows — page it with less so the popup can scroll.
        subprocess.run(['tmux', 'display-popup', *target_args,
                        '-w', str(pop_w),
                        '-h', str(max_h), '-E',
                        f"less -R '{tmpfile}'; rm -f '{tmpfile}'"])
    else:
        subprocess.run(['tmux', 'display-popup', *target_args,
                        '-w', str(pop_w),
                        '-h', str(pop_h),
                        f"cat '{tmpfile}'; rm -f '{tmpfile}'"])


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

    pull_parser = sub.add_parser('pull', help='Fast-forward all repos')
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

    create_parser = sub.add_parser(
        'create',
        help='Clone a new repo into the hive (URL or inferred from siblings)',
    )
    create_parser.add_argument(
        'url', nargs='?',
        help='Git clone URL. If omitted, inferred from existing sibling '
             'workspaces (when they all share one origin)',
    )
    create_parser.add_argument(
        '--name-prefix',
        help='Explicit prefix (skips inference from existing repos)',
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

    tmux_parser = sub.add_parser(
        'tmux',
        help='Start or attach to a tmux dev session for a hive',
    )
    tmux_parser.add_argument(
        '--hive',
        help='Hive short name or path (default: detect from cwd)',
    )
    tmux_parser.add_argument(
        '--list', action='store_true', dest='list_hives',
        help='List configured hives with their assigned colors',
    )
    tmux_parser.add_argument(
        '--new-window', action='store_true',
        help='Open a new window on an unused workspace in the current session',
    )
    tmux_sub = tmux_parser.add_subparsers(dest='tmux_action')
    # Hidden helper actions, invoked by the generated tmux config.
    tmux_label = tmux_sub.add_parser('label-window')
    tmux_label.add_argument('pane_path')
    tmux_label.add_argument('window_id')
    tmux_refresh = tmux_sub.add_parser('refresh-labels')
    tmux_refresh.add_argument('session')
    tmux_turn = tmux_sub.add_parser('turn-refresh')
    tmux_turn.add_argument('session')
    tmux_turn.add_argument('--bust-cache', action='store_true')
    tmux_pairs = tmux_sub.add_parser('pairs')
    tmux_pairs.add_argument('session', nargs='?')
    tmux_role = tmux_sub.add_parser('role')
    tmux_role.add_argument(
        'role', choices=('implementer', 'reviewer', 'clear'))
    tmux_context = tmux_sub.add_parser('status-context')
    tmux_context.add_argument('pane_path')
    tmux_context.add_argument('client_width')
    tmux_context.add_argument('session_name')
    tmux_context.add_argument('window_count')
    tmux_gitsync = tmux_sub.add_parser('git-sync')
    tmux_gitsync.add_argument('pane_path')
    tmux_popup = tmux_sub.add_parser('popup')
    tmux_popup.add_argument('--client')
    tmux_popup.add_argument('--pane')
    tmux_popup.add_argument('--cwd')
    tmux_popup.add_argument('command', nargs=argparse.REMAINDER)
    tmux_runs = tmux_sub.add_parser('runs')
    tmux_runs.add_argument('--hive-root', dest='hive_root',
                           help='Hive root path (default: detect from cwd)')
    tmux_sub.add_parser(
        'restyle',
        help='Re-apply day/night styling to live hive sessions')

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
    elif args.command == 'apiary':
        cmd_apiary(args)
    elif args.command == 'tmux':
        cmd_tmux(args)


if __name__ == '__main__':
    main()
