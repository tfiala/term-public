#!/usr/bin/env python3
# Canonical source for the `hive-ci-popup` tool. Other repos vendor this
# file from here at install time — make changes in tfiala/term-public,
# never in a vendored copy.
"""hive-ci-popup.py - Compact CI status across all repos in a hive.

Queries Forgejo API for recent workflow runs across all repos in a hive
and displays a compact summary grouped by repo. Bound to Alt+B in the
hive shell prompt overlay; can also be invoked directly.

Usage:
  hive-ci-popup [--hive-root PATH] [--width N] [--height-file PATH]

Options:
  --hive-root PATH    Hive root directory (auto-detected from cwd if not provided)
  --width N           Terminal width (defaults to $COLUMNS or 80)
  --height-file PATH  Write output line count to a file (rendering size hint)

The script discovers Forgejo repos by reading git remotes from each workspace
in the hive, deduplicates by owner/repo identity, queries CI runs for each
unique repo, and groups output by repo name.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    # Real Pacific time — DST-aware, so summer renders as UTC-7, winter UTC-8.
    PT = ZoneInfo('America/Los_Angeles')
except ZoneInfoNotFoundError:
    # No system tz database available — fall back to a fixed offset (no DST).
    PT = timezone(timedelta(hours=-8))

WF_ABBREV = {
    'ci.yml': 'ci',
    'ci.yaml': 'ci',
    'test.yml': 'test',
    'test.yaml': 'test',
    'test-scripts.yml': 'test',
    'pr-preview.yml': 'preview',
    'deploy-ec2.yml': 'deploy',
    'deploy.yml': 'deploy',
    'deploy.yaml': 'deploy',
    'infra.yml': 'infra',
    'lint.yml': 'lint',
    'lint.yaml': 'lint',
    'build.yml': 'build',
    'build.yaml': 'build',
    'build-runner.yaml': 'runner',
    'helm-drift.yaml': 'helm',
}

STATUS_ICON = {
    'success': '\033[32m+\033[0m',
    'failure': '\033[31mx\033[0m',
    'running': '\033[33m~\033[0m',
    'waiting': '\033[34m.\033[0m',
    'cancelled': '\033[2m-\033[0m',
    'skipped': '\033[2m-\033[0m',
}

_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


# --- Spinner ------------------------------------------------------------------


_SPINNER_FRAMES = '\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f'


class _Spinner:
    def __init__(self):
        self._active = False
        self._thread: threading.Thread | None = None
        self._message = ''
        self._frame = 0

    def _run(self) -> None:
        while self._active:
            frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            sys.stderr.write(f'\r  \033[36m{frame}\033[0m \033[2m{self._message}\033[0m')
            sys.stderr.flush()
            self._frame += 1
            time.sleep(0.08)

    def start(self, message: str = '') -> None:
        self._message = message
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        if self._thread:
            self._thread.join()
        sys.stderr.write('\r\033[K')
        sys.stderr.flush()


# --- Git helpers --------------------------------------------------------------


def _git(*args: str, cwd: str | Path | None = None) -> str | None:
    """Run a git command, return stripped stdout or None on failure."""
    try:
        r = subprocess.run(
            ['git', *args],
            capture_output=True, text=True, cwd=cwd,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except FileNotFoundError:
        return None


_APIARY_CONFIG = Path('~/.config/hive/apiary.json').expanduser()


def _load_apiary() -> dict:
    """Load apiary config. Returns dict with 'hives' list."""
    if not _APIARY_CONFIG.is_file():
        return {'hives': []}
    try:
        data = json.loads(_APIARY_CONFIG.read_text())
        hives = [Path(p).expanduser() for p in data.get('hives', [])]
        return {'hives': hives}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {'hives': []}


def _find_hive_root() -> Path | None:
    """Find the hive root using three-tier detection.

    1. Inside a hive member repo: return parent of git root.
    2. At or under a configured apiary hive root: return the hive root.
    3. Outside any hive: return None.
    """
    # Tier 1: inside a git repo -> parent is the hive root
    toplevel = _git('rev-parse', '--show-toplevel')
    if toplevel:
        return Path(toplevel).parent

    # Tier 2: at or under a configured apiary hive root (most specific wins)
    cwd = Path.cwd()
    apiary = _load_apiary()
    if apiary['hives']:
        resolved_cwd = cwd.resolve()
        best: tuple[int, Path] | None = None
        for h in apiary['hives']:
            resolved_h = h.resolve()
            if resolved_cwd == resolved_h or resolved_h in resolved_cwd.parents:
                depth = len(resolved_h.parts)
                if best is None or depth > best[0]:
                    best = (depth, h)
        if best is not None:
            return best[1]

    # Tier 3: outside any hive
    return None


def _discover_workspaces(hive: Path) -> list[Path]:
    """Discover git workspaces in the hive."""
    workspaces = []
    if not hive.is_dir():
        return workspaces
    for entry in sorted(hive.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / '.git').exists():
            workspaces.append(entry)
    return workspaces


# --- Remote parsing -----------------------------------------------------------


def _parse_forgejo_remote(url: str) -> tuple[str, str, str] | None:
    """Parse a Forgejo remote URL into (base_url, owner, repo).

    Supports:
        https://git.example.com/acme/widget.git
        http://user:pass@forge:3000/org/repo.git
        git@git.example.com:acme/widget.git   (SCP-style SSH)
        ssh://git@git.example.com/acme/widget.git
    """
    # SCP-style SSH: git@host:owner/repo.git
    scp_match = re.match(r'^(?:[^@]+@)?([^:]+):(.+)$', url)
    if scp_match and '://' not in url:
        hostname = scp_match.group(1)
        path = scp_match.group(2)
        if '/' in hostname:
            # Not a hostname — a local path that happens to contain ':'.
            return None
        base = f'https://{hostname}'
    else:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https', 'ssh') or not parsed.hostname:
            # Not a Forgejo URL — local path, bare path, unsupported scheme.
            return None
        base = f'{parsed.scheme}://{parsed.hostname}'
        if parsed.port:
            base += f':{parsed.port}'
        path = parsed.path

    path = path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]

    parts = path.strip('/').split('/')
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    return base, owner, repo


def _discover_repos(hive: Path) -> list[tuple[str, str, str]]:
    """Discover unique Forgejo repos in the hive.

    Returns list of (base_url, owner, repo) tuples, deduplicated by identity.
    """
    workspaces = _discover_workspaces(hive)
    seen = set()
    repos = []

    for ws in workspaces:
        origin = _git('remote', 'get-url', 'origin', cwd=ws)
        if not origin:
            continue

        remote = _parse_forgejo_remote(origin)
        if not remote:
            continue

        base_url, owner, repo = remote
        identity = (owner, repo)
        if identity in seen:
            continue
        seen.add(identity)
        repos.append((base_url, owner, repo))

    return repos


# --- Token resolution ---------------------------------------------------------


def _get_token_from_credentials(base_url: str) -> str | None:
    """Read the API token from ~/.git-credentials for the given base URL."""
    creds_file = Path.home() / '.git-credentials'
    if not creds_file.exists():
        return None

    parsed_base = urlparse(base_url)
    base_host = parsed_base.hostname
    base_port = parsed_base.port

    if base_port:
        match_targets = {
            f'{base_host}:{base_port}',
            quote(f'{base_host}:{base_port}', safe=''),
        }
    else:
        match_targets = {base_host, quote(base_host, safe='')}

    for line in creds_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = urlparse(line)
        except Exception:
            continue
        if not parsed.username or parsed.password is None:
            continue

        cred_host = parsed.hostname or ''
        try:
            parsed_port = parsed.port
        except ValueError:
            continue
        if parsed_port:
            cred_host_with_port = f'{cred_host}:{parsed_port}'
        else:
            cred_host_with_port = cred_host

        if (cred_host in match_targets
                or cred_host_with_port in match_targets
                or unquote(cred_host) in match_targets):
            return parsed.password

    return None


# --- API ----------------------------------------------------------------------


def _api_get(base_url: str, token: str, path: str) -> list | dict:
    url = f'{base_url}/api/v1{path}'
    req = Request(url, headers={
        'Authorization': f'token {token}',
        'Content-Type': 'application/json',
    }, method='GET')
    try:
        with urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        # 404 is expected when looking up a per-repo resource that doesn't
        # exist at the requested index — most commonly _get_pr_state querying
        # /pulls/{N} where N came from a run's `prettyref` but isn't a valid
        # PR index on this repo (cross-repo triggers, issue refs, or other
        # cases where Forgejo's prettyref doesn't map to a real PR here).
        # Treat as no-data rather than printing popup-noise error lines.
        if e.code != 404:
            print(f'\033[31mAPI error: {e}\033[0m')
        return []
    except Exception as e:
        print(f'\033[31mAPI error: {e}\033[0m')
        return []


def _get_runs(base_url: str, owner: str, repo: str, token: str, limit: int = 10) -> list:
    data = _api_get(base_url, token, f'/repos/{owner}/{repo}/actions/runs?limit={limit}')
    if isinstance(data, dict):
        return data.get('workflow_runs', [])
    return data if isinstance(data, list) else []


def _get_pr_state(base_url: str, owner: str, repo: str, token: str, pr_num: int) -> str:
    """Get PR state: 'open', 'merged', or 'closed'."""
    data = _api_get(base_url, token, f'/repos/{owner}/{repo}/pulls/{pr_num}')
    if isinstance(data, dict):
        if data.get('merged_at') or data.get('merged'):
            return 'merged'
        return data.get('state', 'unknown')
    return 'unknown'


# --- Helpers ------------------------------------------------------------------


def _vis_len(s: str) -> int:
    """Visual length of a string, excluding ANSI escape codes."""
    return len(_ANSI_RE.sub('', s))


def _parse_time(ts: str) -> str:
    if not ts:
        return ''
    try:
        if ts.endswith('Z'):
            ts = ts[:-1] + '+00:00'
        dt = datetime.fromisoformat(ts).astimezone(PT)
        return dt.strftime('%H:%M')
    except Exception:
        return ''


def _wf_short(name: str) -> str:
    return WF_ABBREV.get(name, name.replace('.yml', '').replace('.yaml', ''))


def _normalize_branch(prettyref: str, workflow: str) -> str:
    """Normalize branch/ref names for display."""
    if not prettyref:
        return 'unknown'
    if prettyref.startswith('#'):
        return f'PR {prettyref}'
    if len(prettyref) == 40 and all(c in '0123456789abcdef' for c in prettyref.lower()):
        return prettyref[:7]
    return prettyref


# --- Rendering ----------------------------------------------------------------


def _extract_pr_number(branch: str) -> int | None:
    """Extract PR number from 'PR #N' format."""
    if branch.startswith('PR #'):
        try:
            return int(branch[4:])
        except ValueError:
            pass
    return None


def _build_branch_groups(runs: list) -> list[dict]:
    """Group runs by branch, keeping only the latest status per workflow per branch."""
    branch_data: dict[str, dict] = {}

    for run in runs:
        raw_ref = run.get('prettyref', '') or run.get('head_branch', '') or 'unknown'
        wf_id = run.get('workflow_id', '') or '?'
        branch = _normalize_branch(raw_ref, wf_id)
        title = run.get('title', '') or run.get('display_title', '') or ''
        wf = _wf_short(wf_id)
        status = run.get('status', 'unknown')
        icon = STATUS_ICON.get(status, '?')
        time_str = _parse_time(run.get('started', '') or run.get('created_at', ''))
        run_id = run.get('id', 0)

        if branch not in branch_data:
            branch_data[branch] = {
                'branch': branch,
                'title': title,
                'time': time_str,
                'workflows': {},
                '_latest_run_id': run_id,
            }

        if wf not in branch_data[branch]['workflows']:
            branch_data[branch]['workflows'][wf] = {'icon': icon, 'wf': wf, 'status': status}

        if run_id > branch_data[branch]['_latest_run_id']:
            branch_data[branch]['title'] = title
            branch_data[branch]['time'] = time_str
            branch_data[branch]['_latest_run_id'] = run_id

    seen_order = []
    for run in runs:
        raw_ref = run.get('prettyref', '') or run.get('head_branch', '') or 'unknown'
        wf_id = run.get('workflow_id', '') or '?'
        branch = _normalize_branch(raw_ref, wf_id)
        if branch not in seen_order:
            seen_order.append(branch)

    PREVIEW_WFS = {'clean', 'dprev'}
    result = []
    for b in seen_order:
        if b not in branch_data:
            continue
        wfs = branch_data[b]['workflows']
        dominated_by_preview_noop = all(
            wf in PREVIEW_WFS and wfs[wf]['status'] in ('skipped', 'cancelled')
            for wf in wfs
        )
        if not dominated_by_preview_noop:
            result.append(branch_data[b])
    return result


def _format_branch(branch: str, pr_states: dict[int, str], branch_col_w: int) -> str:
    """Format branch name with optional strikethrough for merged/closed PRs."""
    pr_num = _extract_pr_number(branch)
    is_closed = pr_num and pr_states.get(pr_num) in ('merged', 'closed')

    display = branch
    if len(display) > branch_col_w:
        display = display[:branch_col_w - 2] + '..'

    if is_closed:
        return f'\033[2;9m{display:<{branch_col_w}}\033[0m'
    return f'\033[1m{display:<{branch_col_w}}\033[0m'


def _render_section(groups: list[dict], pr_states: dict[int, str], width: int,
                    header: str | None = None) -> int:
    """Render a section of CI output. Returns line count."""
    if not groups:
        return 0

    narrow = width < 70
    lines = 0

    if header:
        print(f'\033[1;97m{header}\033[0m')
        print()
        lines += 2

    if narrow:
        for g in groups:
            wfs = g['workflows']
            icons = ' '.join(f'{wfs[w]["icon"]}{w[:2]}' for w in sorted(wfs))
            branch_str = _format_branch(g['branch'], pr_states, 14)
            print(f'  {branch_str}  {icons}')
            max_t = width - 4
            t = g['title']
            t = t[:max_t - 2] + '..' if len(t) > max_t else t
            print(f'  \033[2m{t}\033[0m')
            lines += 2
    else:
        branch_col_w = max(len(g['branch']) for g in groups) if groups else 6
        branch_col_w = min(branch_col_w, 16)

        for g in groups:
            wfs = g['workflows']
            branch_str = _format_branch(g['branch'], pr_states, branch_col_w)
            col_parts = []
            for wf in sorted(wfs.keys()):
                wf_short = wf[:7] if len(wf) > 7 else wf
                col_parts.append(f'{wfs[wf]["icon"]} {wf_short}')
            status_str = '  '.join(col_parts)
            status_vis_len = _vis_len(status_str) if status_str else 0
            title_start = 2 + branch_col_w + 2 + status_vis_len + 2
            avail = max(width - title_start, 10)
            t = g['title']
            t = t[:avail - 2] + '..' if len(t) > avail else t
            print(f'  {branch_str}  {status_str}  \033[2m{t}\033[0m')
            lines += 1

    return lines


def _render(repos_data: list[tuple[str, list]], width: int,
            all_pr_states: dict[str, dict[int, str]]) -> int:
    """Render CI output for all repos. Returns total line count."""
    lines = 0

    for repo_name, runs in repos_data:
        if not runs:
            continue
        groups = _build_branch_groups(runs)
        pr_states = all_pr_states.get(repo_name, {})
        lines += _render_section(groups, pr_states, width, repo_name)
        print()
        lines += 1

    if lines == 0:
        print('No recent runs.')
        lines += 1

    # Footer
    total_repos = len([r for r, runs in repos_data if runs])
    total_runs = sum(len(runs) for _, runs in repos_data)
    print(f'\033[2m{total_repos} repo(s), {total_runs} runs  |  any key to close\033[0m')
    lines += 1

    return lines


# --- Main ---------------------------------------------------------------------


def _collect_pr_numbers(runs: list) -> set[int]:
    """Extract all PR numbers from runs."""
    pr_nums = set()
    for run in runs:
        raw_ref = run.get('prettyref', '') or run.get('head_branch', '') or ''
        wf_id = run.get('workflow_id', '') or ''
        branch = _normalize_branch(raw_ref, wf_id)
        pr_num = _extract_pr_number(branch)
        if pr_num:
            pr_nums.add(pr_num)
    return pr_nums


def _fetch_pr_states(base_url: str, owner: str, repo: str, token: str,
                     pr_nums: set[int]) -> dict[int, str]:
    """Fetch state for multiple PRs."""
    states = {}
    for pr_num in pr_nums:
        states[pr_num] = _get_pr_state(base_url, owner, repo, token, pr_num)
    return states


def main():
    parser = argparse.ArgumentParser(
        description=(
            'Compact CI status across all Forgejo repos in a hive. '
            'Discovers repos via git remotes in each workspace, queries '
            'recent workflow runs, and prints a per-repo summary.'
        ),
    )
    parser.add_argument(
        '--hive-root',
        type=lambda s: Path(s).expanduser(),
        help='Hive root directory (auto-detected from cwd if not provided)',
    )
    parser.add_argument(
        '--width',
        type=int,
        default=int(os.environ.get('COLUMNS', '80')),
        help='Terminal width (defaults to $COLUMNS or 80)',
    )
    parser.add_argument(
        '--height-file',
        help='Write output line count to this file (rendering size hint)',
    )
    ns = parser.parse_args()
    width = ns.width
    height_file = ns.height_file
    hive_root = ns.hive_root

    # Auto-detect hive root if not provided
    if hive_root is None:
        hive_root = _find_hive_root()
        if hive_root is None:
            print('\033[31mNot in a hive - use --hive-root\033[0m')
            sys.exit(1)

    # Discover repos in the hive
    repos = _discover_repos(hive_root)
    if not repos:
        print('\033[31mNo Forgejo repos found in hive\033[0m')
        sys.exit(1)

    spinner = _Spinner()
    spinner.start('Fetching CI runs...')

    repos_data: list[tuple[str, list]] = []
    all_pr_states: dict[str, dict[int, str]] = {}

    for base_url, owner, repo in repos:
        token = _get_token_from_credentials(base_url)
        if not token:
            continue

        repo_name = f'{owner}/{repo}'
        runs = _get_runs(base_url, owner, repo, token, limit=12)
        repos_data.append((repo_name, runs))

        # Fetch PR states
        pr_nums = _collect_pr_numbers(runs)
        if pr_nums:
            pr_states = _fetch_pr_states(base_url, owner, repo, token, pr_nums)
            all_pr_states[repo_name] = pr_states

    spinner.stop()

    line_count = _render(repos_data, width, all_pr_states)

    if height_file:
        Path(height_file).write_text(str(line_count))


if __name__ == '__main__':
    main()
