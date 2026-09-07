"""Tests for the vendored xterm-ghostty terminfo source.

setup.sh compiles ghostty/xterm-ghostty.terminfo into ~/.terminfo on hosts
without the Ghostty app bundle (a Linux box reached over SSH from a Ghostty
window), so SSH sessions keep TERM=xterm-ghostty instead of falling back to
xterm-256color in bash/bashrc.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINFO_SRC = REPO_ROOT / "ghostty" / "xterm-ghostty.terminfo"
SETUP_SH = REPO_ROOT / "setup.sh"
GHOSTTY_BUNDLE_TI = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")


def _caps(text: str) -> set[str]:
    """The capability set of a terminfo source, independent of infocmp's
    line wrapping (which differs between ncurses builds)."""
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    body = body.strip().rstrip(",")
    # Split on the separating commas only: a comma inside a capability is
    # written `\,` and is never followed by whitespace in infocmp output.
    return {c.strip() for c in re.split(r",(?:\s*\n\s*|\s+)", body) if c.strip()}


class TestVendoredSource:
    def test_names_xterm_ghostty(self):
        lines = [l for l in TERMINFO_SRC.read_text().splitlines() if not l.startswith("#")]
        assert lines[0].startswith("xterm-ghostty|"), lines[0]

    def test_header_explains_regeneration(self):
        text = TERMINFO_SRC.read_text()
        assert "infocmp -x xterm-ghostty" in text
        assert "tic -x" in text

    def test_no_machine_paths(self):
        assert "/Users/" not in TERMINFO_SRC.read_text()

    @pytest.mark.skipif(shutil.which("tic") is None, reason="tic not installed")
    def test_compiles_and_resolves(self, tmp_path):
        r = subprocess.run(
            ["tic", "-x", "-o", str(tmp_path), str(TERMINFO_SRC)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        r = subprocess.run(
            ["infocmp", "xterm-ghostty"],
            capture_output=True, text=True, env={"TERMINFO": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        assert r.returncode == 0, r.stderr
        assert "xterm-ghostty|" in r.stdout

    @pytest.mark.skipif(not GHOSTTY_BUNDLE_TI.is_dir(), reason="Ghostty app bundle not installed")
    def test_matches_installed_ghostty(self):
        """Drift check on a macOS host with Ghostty: the vendored file must
        carry the same capabilities as the installed bundle.  Regenerate
        with the command in the file header after a Ghostty upgrade."""
        r = subprocess.run(
            ["infocmp", "-x", "xterm-ghostty"],
            capture_output=True, text=True,
            env={"TERMINFO": str(GHOSTTY_BUNDLE_TI), "PATH": "/usr/bin:/bin"},
        )
        assert r.returncode == 0, r.stderr
        installed = _caps(r.stdout)
        vendored = _caps(TERMINFO_SRC.read_text())
        assert vendored == installed, (
            "ghostty/xterm-ghostty.terminfo drifted from the installed Ghostty; "
            f"missing={sorted(installed - vendored)[:5]} extra={sorted(vendored - installed)[:5]} "
            "(regenerate with the command in the file header)"
        )


class TestSetupInstallsIt:
    def test_setup_sh_falls_back_to_the_vendored_source(self):
        text = SETUP_SH.read_text()
        vendored = 'tic -x "$ROOT_DIR/ghostty/xterm-ghostty.terminfo"'
        assert vendored in text
        assert text.index('-d "$_ghostty_ti"') < text.index(vendored)  # bundle first
