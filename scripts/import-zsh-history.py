#!/usr/bin/env python3
"""import-zsh-history.py - One-shot ~/.zsh_history -> bash history import.

The bash cutover (#15/#19) kept the zsh era's history *behavior* (see the
History section of bash/bashrc) but not its accumulated *data*.  This
script converts zsh extended-history entries

    : <start>:<elapsed>;<command>

into the timestamped format bash reads when HISTTIMEFORMAT is set

    #<start>
    <command>

and appends them to the bash history file, by default the one bash/bashrc
sets as HISTFILE (~/.local/state/bash/history).  Multi-line zsh entries
(backslash-continued lines) are unfolded back into real newlines, which
bash re-joins into one entry via the timestamp markers.  Bytes zsh
"metafied" (0x83 marker + byte^0x20, used for non-ASCII) are restored.

Safety / idempotency: the destination is backed up to
<bash_history>.zsh-import.bak first, and the script refuses to run again
while that backup exists — delete it to force a re-import.  With the
default destination it also refuses while bash/bashrc has not yet migrated
~/.bash_history (importing first would make the migration skip it), and when
the legacy ~/.bash_history.zsh-import.bak shows the import already ran into
the old file.
"""

import argparse
import os
import sys
from pathlib import Path

ZSH_META = 0x83  # zsh Meta marker byte: next byte is stored XOR 0x20
BACKUP_SUFFIX = ".zsh-import.bak"
# The HISTFILE bash/bashrc sets, relative to HOME, and the pre-2026-11
# default it migrates from.  setup.sh names both too.
HISTFILE_REL = Path(".local/state/bash/history")
LEGACY_HISTFILE_REL = Path(".bash_history")


def default_guard(home: Path) -> str | None:
    """Why an import into the default destination must not run yet, or None.

    bash/bashrc migrates ~/.bash_history only while its own history file
    does not exist, so an import that creates that file first would leave
    the legacy history behind for good.  A legacy done-marker means the
    import already ran into ~/.bash_history, which the migration carries.
    """
    dest = home / HISTFILE_REL
    legacy = home / LEGACY_HISTFILE_REL
    legacy_marker = legacy.with_name(legacy.name + BACKUP_SUFFIX)
    if legacy_marker.exists():
        return (
            f"{legacy_marker} exists — zsh history was already imported into\n"
            f"       {legacy}, which bash/bashrc migrates to {dest}."
        )
    if legacy.is_file() and not dest.exists():
        return (
            f"{legacy} has not been migrated to {dest} yet.\n"
            f"       Start one interactive bash (bash/bashrc migrates it, or "
            f"says why it cannot),\n"
            f"       then rerun."
        )
    return None


def unmetafy(data: bytes) -> bytes:
    """Undo zsh's metafication of bytes that collide with its internals."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == ZSH_META and i + 1 < len(data):
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def parse_zsh_history(text: str) -> list[tuple[int | None, str]]:
    """Parse zsh history text into (timestamp, command) entries.

    Extended-history entries carry a timestamp; plain lines (pre-extended
    history, or a foreign file) come back with timestamp None.  A line
    ending in a backslash continues the entry on the next line; the
    backslash stands for the newline it escaped.
    """
    # Unfold continuations first: trailing backslash means the entry
    # continues, and the escaped newline is part of the command.
    physical = text.split("\n")
    logical: list[str] = []
    for line in physical:
        if logical and logical[-1].endswith("\\"):
            logical[-1] = logical[-1][:-1] + "\n" + line
        else:
            logical.append(line)

    entries: list[tuple[int | None, str]] = []
    for entry in logical:
        if not entry.strip():
            continue
        timestamp: int | None = None
        command = entry
        if entry.startswith(": "):
            head, sep, rest = entry.partition(";")
            if sep:
                fields = head[2:].split(":")
                if len(fields) == 2 and fields[0].strip().isdigit():
                    timestamp = int(fields[0].strip())
                    command = rest
        if command.strip():
            entries.append((timestamp, command))
    return entries


def render_bash_history(entries: list[tuple[int | None, str]]) -> str:
    """Render entries in the timestamped bash history-file format."""
    lines: list[str] = []
    for timestamp, command in entries:
        if timestamp is not None:
            lines.append(f"#{timestamp}")
        lines.append(command)
    return "".join(line + "\n" for line in lines)


def import_history(zsh_path: Path, bash_path: Path) -> int:
    backup = bash_path.with_name(bash_path.name + BACKUP_SUFFIX)
    if backup.exists():
        print(
            f"error: {backup} exists — zsh history was already imported.\n"
            f"       Delete it to force a re-import.",
            file=sys.stderr,
        )
        return 1
    if not zsh_path.is_file():
        print(f"error: no zsh history at {zsh_path}", file=sys.stderr)
        return 1

    text = unmetafy(zsh_path.read_bytes()).decode("utf-8", errors="replace")
    entries = parse_zsh_history(text)
    if not entries:
        print(f"error: no entries parsed from {zsh_path}", file=sys.stderr)
        return 1

    try:
        # The default destination's directory may not exist yet on a machine
        # where no shell has run bash/bashrc.  0700: it holds only history.
        bash_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing = bash_path.read_bytes() if bash_path.is_file() else b""
        # History can contain private commands, so the backup is created
        # 0600 regardless of umask; O_EXCL also makes the done-marker check
        # above race-safe.
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as err:
        print(f"error: cannot write {bash_path}: {err}", file=sys.stderr)
        return 1
    with os.fdopen(fd, "wb") as f:
        f.write(existing)

    converted = render_bash_history(entries).encode("utf-8")
    if existing and not existing.endswith(b"\n"):
        converted = b"\n" + converted
    # Append via O_APPEND, never read-modify-replace: a `history -a` from
    # a live shell between the snapshot above and this write must survive.
    # The 0600 mode applies only if the file is being created; an existing
    # destination keeps its current mode.
    fd = os.open(bash_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(converted)

    print(f"Imported {len(entries)} entries from {zsh_path} into {bash_path}")
    print(f"Backup of the previous bash history: {backup}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import zsh history into bash history (one-shot)."
    )
    parser.add_argument(
        "--zsh-history",
        type=Path,
        default=Path.home() / ".zsh_history",
        help="source zsh history file (default: ~/.zsh_history)",
    )
    parser.add_argument(
        "--bash-history",
        type=Path,
        default=None,
        help=f"destination bash history file (default: ~/{HISTFILE_REL})",
    )
    args = parser.parse_args()
    if args.bash_history is None:
        home = Path.home()
        reason = default_guard(home)
        if reason:
            print(f"error: {reason}", file=sys.stderr)
            return 1
        args.bash_history = home / HISTFILE_REL
    return import_history(args.zsh_history, args.bash_history)


if __name__ == "__main__":
    sys.exit(main())
