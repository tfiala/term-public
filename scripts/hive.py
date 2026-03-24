#!/usr/bin/env python3
"""hive.py - hive/apiary helper with dtach-backed dev shells.

This is a trimmed public baseline extracted from a larger private workflow.
It keeps:

- hive discovery
- apiary config
- status / pull / create
- dtach-backed `hive shell`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


class _Colors:
    def __init__(self) -> None:
        self.enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def force_enable(self) -> None:
        self.enabled = True

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def red(self, text: str) -> str:
        return self._wrap("1;91", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)


C = _Colors()
_NAME_RE = re.compile(r"^(.+)-(\d+)$")
_APIARY_CONFIG = Path("~/.config/hive/apiary.json").expanduser()
_DTACH_DIR = Path("/tmp/hive-dtach")
_FETCH_TIMEOUT = 5

_SHELL_PALETTE = [
    {"name": "blue", "rgb": "97;150;255", "c256": "75"},
    {"name": "teal", "rgb": "45;212;168", "c256": "43"},
    {"name": "green", "rgb": "102;187;106", "c256": "114"},
    {"name": "purple", "rgb": "179;157;219", "c256": "141"},
    {"name": "amber", "rgb": "255;202;40", "c256": "220"},
    {"name": "rose", "rgb": "239;83;80", "c256": "203"},
    {"name": "cyan", "rgb": "38;198;218", "c256": "44"},
    {"name": "orange", "rgb": "255;167;38", "c256": "214"},
]


def CHECK() -> str:
    return C.green("✓")


def CROSS() -> str:
    return C.red("✗")


def _git(args: list[str], cwd: str | Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git"] + args, -1, "", "timeout")


def _git_out(args: list[str], cwd: str | Path | None = None) -> str | None:
    result = _git(args, cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _load_apiary() -> list[Path] | None:
    if not _APIARY_CONFIG.is_file():
        return None
    try:
        data = json.loads(_APIARY_CONFIG.read_text())
        return [Path(p).expanduser() for p in data.get("hives", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"{CROSS()} Invalid apiary config: {_APIARY_CONFIG}", file=sys.stderr)
        sys.exit(1)


def _display_path(path: Path) -> str:
    try:
        return f"~/{path.resolve().relative_to(Path.home())}"
    except ValueError:
        return str(path.resolve())


def _storable_path(path: Path) -> str:
    return _display_path(path)


def _save_apiary(hives: list[Path]) -> None:
    _APIARY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    _APIARY_CONFIG.write_text(
        json.dumps({"hives": [_storable_path(h) for h in hives]}, indent=2) + "\n"
    )


def _short_name(hive: Path) -> str:
    name = hive.name
    if name.endswith("-app"):
        return name
    return name


def _find_hive_root() -> Path | None:
    toplevel = _git_out(["rev-parse", "--show-toplevel"])
    if toplevel is not None:
        return Path(toplevel).parent

    cwd = Path.cwd().resolve()
    apiary_hives = _load_apiary()
    if apiary_hives:
        best: tuple[int, Path] | None = None
        for hive in apiary_hives:
            resolved = hive.resolve()
            if cwd == resolved or resolved in cwd.parents:
                depth = len(resolved.parts)
                if best is None or depth > best[0]:
                    best = (depth, hive)
        if best is not None:
            return best[1]

    return None


def _discover_repos(hive: Path) -> list[Path]:
    repos: list[Path] = []
    for entry in sorted(hive.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            repos.append(entry)
    return repos


def _default_branch(repo_path: Path) -> str:
    ref = _git_out(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path)
    if ref and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/") :]
    return "main"


def _workspace_number(name: str) -> str | None:
    match = _NAME_RE.match(name)
    return match.group(2) if match else None


def _infer_next_repo_dir(hive: Path, name_prefix: str | None) -> Path:
    groups: dict[str, list[int]] = {}
    for entry in _discover_repos(hive):
        match = _NAME_RE.match(entry.name)
        if match:
            prefix, number = match.group(1), int(match.group(2))
        else:
            prefix, number = entry.name, 1
        groups.setdefault(prefix, []).append(number)

    if name_prefix is not None:
        prefix = name_prefix
    elif len(groups) == 1:
        prefix = next(iter(groups))
    elif not groups:
        print(f"{CROSS()} No repos found to infer prefix from; use --name-prefix", file=sys.stderr)
        sys.exit(1)
    else:
        found = ", ".join(sorted(groups))
        print(f"{CROSS()} Ambiguous prefixes found: {found}; use --name-prefix", file=sys.stderr)
        sys.exit(1)

    next_num = max(groups.get(prefix, [0])) + 1
    target = hive / f"{prefix}-{next_num}"
    if target.exists():
        print(f"{CROSS()} Target directory already exists: {target}", file=sys.stderr)
        sys.exit(1)
    return target


def _hive_color(hive: Path) -> dict:
    apiary = _load_apiary()
    if apiary:
        resolved = hive.resolve()
        for i, entry in enumerate(apiary):
            if entry.resolve() == resolved:
                return _SHELL_PALETTE[i % len(_SHELL_PALETTE)]
    return _SHELL_PALETTE[0]


def _socket_path(hive_name: str, number: str) -> Path:
    return _DTACH_DIR / f"{hive_name}-{number}.sock"


def _sidecar_path(hive_name: str, number: str) -> Path:
    return _DTACH_DIR / f"{hive_name}-{number}.json"


def _socket_alive(sock: Path) -> bool:
    if not sock.exists():
        return False
    result = subprocess.run(
        ["dtach", "-p", str(sock)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=2,
    )
    return result.returncode == 0


def _find_workspace_for_reattach(hive: Path, hive_name: str) -> tuple[Path, str] | None:
    cwd = Path.cwd().resolve()
    for entry in _discover_repos(hive):
        resolved = entry.resolve()
        if cwd == resolved or resolved in cwd.parents:
            number = _workspace_number(entry.name)
            if number and _socket_alive(_socket_path(hive_name, number)):
                return entry, number
    return None


def _find_clean_workspace(hive: Path, hive_name: str) -> tuple[Path, str] | None:
    for entry in _discover_repos(hive):
        number = _workspace_number(entry.name)
        if not number:
            continue
        if _socket_alive(_socket_path(hive_name, number)):
            continue
        default = _default_branch(entry)
        current = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=entry)
        if current != default:
            continue
        porcelain = _git_out(["status", "--porcelain"], cwd=entry)
        if porcelain == "":
            return entry, number
    return None


def _create_new_workspace(hive: Path) -> tuple[Path, str]:
    existing = next(iter(_discover_repos(hive)), None)
    if existing is None:
        print(f"{CROSS()} No existing repos in hive to clone from", file=sys.stderr)
        sys.exit(1)

    origin_url = _git_out(["remote", "get-url", "origin"], cwd=existing)
    if not origin_url:
        print(f"{CROSS()} Cannot determine origin URL from {existing.name}", file=sys.stderr)
        sys.exit(1)

    target = _infer_next_repo_dir(hive, None)
    result = _git(["clone", origin_url, str(target)])
    if result.returncode != 0:
        print(f"{CROSS()} Clone failed: {target.name}", file=sys.stderr)
        stderr = result.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[:3]:
                print(f"  {C.dim(line)}", file=sys.stderr)
        sys.exit(1)

    number = _workspace_number(target.name) or "1"
    print(f"{CHECK()} Created {target.name}")
    return target, number


def _write_sidecar(hive_name: str, number: str, workspace: Path) -> None:
    _DTACH_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "hive": hive_name,
        "workspace": str(workspace),
        "number": int(number),
        "created": datetime.now(timezone.utc).isoformat(),
        "branch_at_checkout": _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace) or "unknown",
    }
    _sidecar_path(hive_name, number).write_text(json.dumps(data, indent=2) + "\n")


def _launch_dtach(hive: Path, hive_name: str, workspace: Path, number: str) -> None:
    _DTACH_DIR.mkdir(parents=True, exist_ok=True)
    sock = _socket_path(hive_name, number)
    if not _socket_alive(sock):
        _write_sidecar(hive_name, number, workspace)

    color = _hive_color(hive)
    prompt_zsh = Path.home() / "bin" / "hive-shell-prompt.zsh"
    if not prompt_zsh.is_file():
        prompt_zsh = Path(__file__).resolve().parents[1] / "zsh" / "hive-shell-prompt.zsh"

    env = os.environ.copy()
    env["HIVE_ROOT"] = str(hive)
    env["HIVE_NAME"] = hive_name
    env["HIVE_WORKSPACE"] = workspace.name
    env["HIVE_NUMBER"] = number
    env["HIVE_COLOR_RGB"] = color["rgb"]
    env["HIVE_COLOR_256"] = color["c256"]

    original_zdotdir = env.get("ZDOTDIR", str(Path.home()))
    if original_zdotdir.startswith(str(_DTACH_DIR / ".zdotdir-")):
        original_zdotdir = env.get("HIVE_REAL_ZDOTDIR", str(Path.home()))

    zdotdir = _DTACH_DIR / f".zdotdir-{hive_name}-{number}"
    zdotdir.mkdir(parents=True, exist_ok=True)

    (zdotdir / ".zshenv").write_text(
        "# Hive shell wrapper\n"
        f'HIVE_REAL_ZDOTDIR="{original_zdotdir}"\n'
        '[[ -f "$HIVE_REAL_ZDOTDIR/.zshenv" ]] && source "$HIVE_REAL_ZDOTDIR/.zshenv"\n'
        f'export ZDOTDIR="{zdotdir}"\n'
    )
    (zdotdir / ".zshrc").write_text(
        "# Hive shell wrapper\n"
        "typeset -g POWERLEVEL9K_INSTANT_PROMPT=quiet\n"
        '[[ -f "$HIVE_REAL_ZDOTDIR/.zshrc" ]] && source "$HIVE_REAL_ZDOTDIR/.zshrc"\n'
        'export HISTFILE="$HIVE_REAL_ZDOTDIR/.zsh_history"\n'
        f'source "{prompt_zsh}"\n'
    )
    env["ZDOTDIR"] = str(zdotdir)

    action = "Reattaching to" if _socket_alive(sock) else "Starting"
    print(f'{CHECK()} {action} {C.cyan(f"{hive_name}-{number}")} in {C.dim(str(workspace))}')

    os.chdir(workspace)
    os.execvpe(
        "dtach",
        ["dtach", "-A", str(sock), "-Ez", "-r", "winch", "zsh"],
        env,
    )


def _shell_list() -> None:
    if not _DTACH_DIR.is_dir():
        print(C.dim("No active sessions"))
        return

    sockets = sorted(_DTACH_DIR.glob("*.sock"))
    if not sockets:
        print(C.dim("No active sessions"))
        return

    found = False
    for sock in sockets:
        name = sock.stem
        alive = _socket_alive(sock)
        sidecar = _DTACH_DIR / f"{name}.json"
        workspace_display = ""
        branch_display = ""

        if sidecar.is_file():
            try:
                data = json.loads(sidecar.read_text())
                workspace = data.get("workspace", "")
                if workspace:
                    workspace_display = _display_path(Path(workspace))
                    workspace_path = Path(workspace)
                    if workspace_path.is_dir():
                        branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace_path)
                        if branch:
                            branch_display = branch
            except (json.JSONDecodeError, KeyError):
                pass

        found = True
        if alive:
            parts = [f"{CHECK()} {C.cyan(name)}"]
            if branch_display:
                parts.append(branch_display)
            if workspace_display:
                parts.append(C.dim(workspace_display))
            print("  ".join(parts))
        else:
            print(f"  {CROSS()} {name}  {C.dim('(dead)')}")

    if not found:
        print(C.dim("No active sessions"))


def _shell_cleanup() -> None:
    if not _DTACH_DIR.is_dir():
        print(C.dim("Nothing to clean up"))
        return

    cleaned = 0

    for sock in sorted(_DTACH_DIR.glob("*.sock")):
        if not _socket_alive(sock):
            sock.unlink()
            name = sock.stem
            for suffix in (".json", ".pr"):
                sidecar = _DTACH_DIR / f"{name}{suffix}"
                if sidecar.is_file():
                    sidecar.unlink()
            print(f"  {CHECK()} Removed dead session: {name}")
            cleaned += 1

    for sidecar in sorted(_DTACH_DIR.glob("*.json")):
        if not (_DTACH_DIR / f"{sidecar.stem}.sock").exists():
            sidecar.unlink()
            print(f"  {CHECK()} Removed orphaned sidecar: {sidecar.stem}")
            cleaned += 1

    for pr_cache in sorted(_DTACH_DIR.glob("*.pr")):
        if not (_DTACH_DIR / f"{pr_cache.stem}.sock").exists():
            pr_cache.unlink()
            cleaned += 1

    for zdotdir in sorted(_DTACH_DIR.glob(".zdotdir-*")):
        if not zdotdir.is_dir():
            continue
        suffix = zdotdir.name[len(".zdotdir-") :]
        sock = _DTACH_DIR / f"{suffix}.sock"
        if not sock.exists() or not _socket_alive(sock):
            shutil.rmtree(zdotdir)
            cleaned += 1

    if cleaned == 0:
        print(C.dim("Nothing to clean up"))
    else:
        print(f"\nCleaned {cleaned} item(s)")


def cmd_status(args: argparse.Namespace) -> None:
    hive = _find_hive_root()
    if hive is None:
        print(f"{CROSS()} Not inside a hive. Use --hive-aware cwd or apiary config.", file=sys.stderr)
        sys.exit(1)

    print(f"Hive: {C.dim(str(hive))}\n")
    repos = _discover_repos(hive)
    if not repos:
        print(f"  {CROSS()} No git repos found in hive")
        return

    for repo in repos:
        branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo) or "(unknown)"
        default = _default_branch(repo)
        porcelain = _git_out(["status", "--porcelain"], cwd=repo)
        dirty = 0 if porcelain in (None, "") else len(porcelain.splitlines())
        branch_mark = CHECK() if branch == default else CROSS()
        if args.compact:
            indicators = []
            if dirty:
                indicators.append(f"{dirty}!")
            extra = f"  {' '.join(indicators)}" if indicators else ""
            print(f"  {repo.name:<20} {branch_mark} {branch}{extra}")
        else:
            print(f"  {C.dim(repo.name)}")
            print(f"    {branch_mark} {branch}")
            print(f"    {CHECK() if dirty == 0 else CROSS()} {'clean' if dirty == 0 else f'{dirty} uncommitted file(s)'}")
            print()


def cmd_pull(args: argparse.Namespace) -> None:
    hive = _find_hive_root()
    if hive is None:
        print(f"{CROSS()} Not inside a hive. Use --hive-aware cwd or apiary config.", file=sys.stderr)
        sys.exit(1)

    print(f"Hive: {C.dim(str(hive))}\n")
    for repo in _discover_repos(hive):
        porcelain = _git_out(["status", "--porcelain"], cwd=repo)
        if porcelain not in (None, ""):
            print(f"  {repo.name:<20} {CROSS()} skipped dirty workspace")
            continue
        branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo) or "main"
        result = _git(["pull", "--rebase", "origin", branch], cwd=repo, timeout=30)
        if result.returncode == 0:
            print(f"  {repo.name:<20} {CHECK()} pulled")
        else:
            _git(["rebase", "--abort"], cwd=repo)
            print(f"  {repo.name:<20} {CROSS()} rebase failed")


def cmd_create(args: argparse.Namespace) -> None:
    hive = _find_hive_root()
    if hive is None:
        print(f"{CROSS()} Not inside a hive.", file=sys.stderr)
        sys.exit(1)

    target = _infer_next_repo_dir(hive, getattr(args, "name_prefix", None))
    source_repo = next(iter(_discover_repos(hive)), None)
    if source_repo is None:
        print(f"{CROSS()} No source repo found in hive", file=sys.stderr)
        sys.exit(1)
    origin_url = _git_out(["remote", "get-url", "origin"], cwd=source_repo)
    if not origin_url:
        print(f"{CROSS()} Cannot determine origin URL from {source_repo.name}", file=sys.stderr)
        sys.exit(1)

    result = _git(["clone", origin_url, str(target)])
    if result.returncode != 0:
        print(f"{CROSS()} Clone failed: {target.name}", file=sys.stderr)
        sys.exit(1)
    print(f"{CHECK()} {target.name}")


def cmd_apiary(args: argparse.Namespace) -> None:
    hives = _load_apiary() or []

    if args.apiary_action == "list":
        if not hives:
            print(C.dim("No configured hives"))
            return
        for hive in hives:
            print(_display_path(hive))
        return

    path_arg = getattr(args, "path", None)
    target = Path(path_arg).expanduser().resolve() if path_arg else Path.cwd().resolve()

    if args.apiary_action == "add":
        if target in [h.resolve() for h in hives]:
            print(C.dim(f"Already present: {_display_path(target)}"))
            return
        hives.append(target)
        _save_apiary(hives)
        print(f"{CHECK()} Added {_display_path(target)}")
    elif args.apiary_action == "remove":
        kept = [h for h in hives if h.resolve() != target]
        if len(kept) == len(hives):
            print(C.dim(f"Not present: {_display_path(target)}"))
            return
        _save_apiary(kept)
        print(f"{CHECK()} Removed {_display_path(target)}")


def cmd_shell(args: argparse.Namespace) -> None:
    if args.shell_action == "list":
        _shell_list()
        return
    if args.shell_action == "cleanup":
        _shell_cleanup()
        return

    hive_arg = getattr(args, "hive", None)
    if hive_arg:
        hive: Path | None = None
        apiary = _load_apiary()
        if apiary:
            for entry in apiary:
                if _short_name(entry) == hive_arg:
                    hive = entry
                    break
        if hive is None:
            candidate = Path(hive_arg).expanduser().resolve()
            if candidate.is_dir():
                hive = candidate
        if hive is None:
            print(f"{CROSS()} Hive not found: {hive_arg}", file=sys.stderr)
            sys.exit(1)
    else:
        hive = _find_hive_root()
        if hive is None:
            print(f"{CROSS()} Not inside a hive. Use --hive to specify one.", file=sys.stderr)
            sys.exit(1)

    hive_name = _short_name(hive)
    if args.number is not None:
        number = str(args.number)
        workspace = None
        for entry in _discover_repos(hive):
            if _workspace_number(entry.name) == number:
                workspace = entry
                break
        if workspace is None:
            print(f"{CROSS()} No workspace #{number} found in {hive_name}", file=sys.stderr)
            sys.exit(1)
        _launch_dtach(hive, hive_name, workspace, number)
        return

    result = _find_workspace_for_reattach(hive, hive_name)
    if result:
        workspace, number = result
        _launch_dtach(hive, hive_name, workspace, number)
        return

    result = _find_clean_workspace(hive, hive_name)
    if result:
        workspace, number = result
        _launch_dtach(hive, hive_name, workspace, number)
        return

    workspace, number = _create_new_workspace(hive)
    _launch_dtach(hive, hive_name, workspace, number)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hive helper with dtach-backed dev shells.")
    parser.add_argument("--color", action="store_true", help="Force colored output")
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status", help="Show git status for repos in the current hive")
    status_parser.add_argument("--compact", action="store_true", help="One line per repo")

    pull_parser = sub.add_parser("pull", help="Pull all clean repos in the current hive")
    pull_parser.add_argument("--compact", action="store_true", help="Reserved for CLI compatibility")

    create_parser = sub.add_parser("create", help="Clone a new numbered repo into the current hive")
    create_parser.add_argument("--name-prefix", help="Explicit prefix for the new repo")

    apiary_parser = sub.add_parser("apiary", help="Manage hive roots")
    apiary_sub = apiary_parser.add_subparsers(dest="apiary_action", required=True)
    apiary_sub.add_parser("list", help="List configured hives")
    apiary_add = apiary_sub.add_parser("add", help="Add a hive root")
    apiary_add.add_argument("path", nargs="?", help="Path to add (default: cwd)")
    apiary_remove = apiary_sub.add_parser("remove", help="Remove a hive root")
    apiary_remove.add_argument("path", nargs="?", help="Path to remove (default: cwd)")

    shell_parser = sub.add_parser("shell", help="Start or attach to a dtach shell session")
    shell_parser.add_argument("--hive", help="Hive short name or path")
    shell_parser.add_argument("--number", "-n", type=int, help="Explicit workspace number")
    shell_sub = shell_parser.add_subparsers(dest="shell_action")
    shell_sub.add_parser("list", help="List active sessions")
    shell_sub.add_parser("cleanup", help="Remove stale sessions")

    args = parser.parse_args()
    if args.color:
        C.force_enable()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "pull":
        cmd_pull(args)
    elif args.command == "create":
        cmd_create(args)
    elif args.command == "apiary":
        cmd_apiary(args)
    elif args.command == "shell":
        cmd_shell(args)


if __name__ == "__main__":
    main()
