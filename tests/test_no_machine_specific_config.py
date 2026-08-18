"""Guard: no machine-specific content in tracked files.

`setup.sh` links `~/.bash_profile` and `~/.bashrc` to `bash/bash_profile`
and `bash/bashrc`.  Package installers that "add themselves to your
PATH" follow those symlinks and append to the *tracked repo file*, not
to a private dotfile.  Docker Desktop did exactly that on 2026-08-18,
writing an `export PATH="$PATH:/Users/<user>/.docker/bin"` block to the
top of `bash/bash_profile`.

That is machine-specific and belongs in the untracked `local/` overlay;
this repo is public and explicitly excludes personal paths.  The window
between an installer running and someone noticing is exactly where such
a line gets committed, so the check runs in CI rather than relying on a
reader spotting it in a diff.

Common offenders besides Docker Desktop: conda/miniconda, nvm, rustup,
pyenv, the Google Cloud SDK, and JetBrains Toolbox.

The two sweeps are complementary and neither is sufficient alone: the
marker sweep catches a block whose paths happen to be parameterized,
and the home-directory sweep catches an installer whose comment wording
is not in the pattern list.
"""

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# A literal home directory: /Users/tfiala, but not the parameterized
# /Users/$USER or /Users/${USER} that macOS `dscl` lookups legitimately
# use (scripts/term-public-troubleshoot.sh, setup/bootstrap-macos.sh).
HARDCODED_HOME_RE = re.compile(r"/Users/(?!\$)[A-Za-z0-9._-]+")

# Marker comments installers leave behind when they edit a shell profile.
INSTALLER_MARKER_RE = re.compile(
    r"(added by .{0,40}(installer|desktop|toolbox|sdk)"
    r"|the following lines? (were|was) added"
    r"|the next lines? updates? path for"
    r"|>>> .{0,30} initiali[sz]e >>>"
    r"|!! contents within this block are managed by)",
    re.IGNORECASE,
)

# Config the installers actually target.  Docs and tests may legitimately
# discuss installer text (this file does), so the marker sweep stays on
# the files a stray append would land in.
CONFIG_PREFIXES = ("bash/", "starship/", "tmux/", "ghostty/", "setup/")
CONFIG_FILES = ("setup.sh",)


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def readable_text(path):
    """Text content, or None for binary/unreadable files."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def is_config(path):
    rel = path.relative_to(REPO_ROOT).as_posix()
    return rel.startswith(CONFIG_PREFIXES) or rel in CONFIG_FILES


class TestNoMachineSpecificContent:

    def test_repo_has_tracked_files(self):
        """Fail closed: an empty file list would make every other
        assertion here vacuous."""
        assert len(tracked_files()) > 10

    def test_config_files_are_covered(self):
        """The marker sweep is scoped by prefix; prove that scope still
        selects the files installers actually write to."""
        covered = {p.relative_to(REPO_ROOT).as_posix()
                   for p in tracked_files() if is_config(p)}
        for required in ("bash/bash_profile", "bash/bashrc", "setup.sh"):
            assert required in covered, f"{required} escaped the sweep"

    def test_no_hardcoded_home_directory(self):
        """No tracked file names a real user's home directory."""
        offenders = []
        for path in tracked_files():
            text = readable_text(path)
            if text is None:
                continue
            for num, line in enumerate(text.splitlines(), 1):
                m = HARDCODED_HOME_RE.search(line)
                if m:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{num}: {m.group(0)}")
        assert not offenders, (
            "hardcoded home directory in tracked files (move it to "
            "local/env.local and use $HOME):\n  " + "\n  ".join(offenders))

    def test_no_installer_appended_block(self):
        """No tracked shell config carries an installer's marker
        comment — the signature of a write that followed the symlink."""
        offenders = []
        for path in tracked_files():
            if not is_config(path):
                continue
            text = readable_text(path)
            if text is None:
                continue
            for num, line in enumerate(text.splitlines(), 1):
                if INSTALLER_MARKER_RE.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{num}: {line.strip()[:70]}")
        assert not offenders, (
            "installer-appended block in tracked config (relocate it to "
            "local/env.local):\n  " + "\n  ".join(offenders))

    def test_bash_profile_starts_with_repo_header(self):
        """Installers append to the top or bottom.  The tracked file's
        first line is a fixed landmark, so a prepended block moves it."""
        first = (REPO_ROOT / "bash" / "bash_profile").read_text().splitlines()[0]
        assert first == "# term-public bash_profile.", (
            f"bash/bash_profile no longer starts with its header: {first!r}")
