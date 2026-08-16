"""Tests for the TERM/TERMINFO fixup in bash/bashrc."""

import os
import subprocess
from pathlib import Path

import pytest


BASHRC = str(Path(__file__).resolve().parents[1] / "bash" / "bashrc")

# The terminfo guard block, extracted so tests can run it in isolation
# without sourcing the rest of bashrc (which needs starship, etc.).
_TERMINFO_GUARD = """\
if command -v infocmp >/dev/null 2>&1 && ! infocmp "$TERM" >/dev/null 2>&1; then
  if [[ "$TERM" == *ghostty* \\
      && -d /Applications/Ghostty.app/Contents/Resources/terminfo ]]; then
    export TERMINFO=/Applications/Ghostty.app/Contents/Resources/terminfo
  else
    export TERM=xterm-256color
  fi
fi
"""


def _run_bash_snippet(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a bash snippet with the given environment, return the result."""
    return subprocess.run(
        ["bash", "--norc", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
    )


def _base_env(**overrides: str) -> dict[str, str]:
    """Minimal env for bash with optional overrides."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    env.update(overrides)
    return env


class TestTerminfoGuardPresent:
    """The guard block in bashrc must match the one under test."""

    def test_guard_block_in_bashrc(self):
        text = Path(BASHRC).read_text()
        assert _TERMINFO_GUARD in text, (
            "bash/bashrc terminfo guard drifted from the block these tests run"
        )

    def test_guard_precedes_prompt_setup(self):
        """The guard must run before anything talks to the terminal."""
        text = Path(BASHRC).read_text()
        assert text.index("infocmp") < text.index("starship")


class TestTerminfoResolvable:
    """When the current TERM has a valid terminfo entry, the guard is a no-op."""

    def test_known_term_unchanged(self):
        """xterm-256color is universally available — should not be altered."""
        env = _base_env(TERM="xterm-256color")
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-256color" in r.stdout
        assert "TERMINFO=unset" in r.stdout

    def test_terminfo_env_not_overwritten_when_already_set(self):
        """If TERMINFO is already set and infocmp succeeds, leave it alone."""
        env = _base_env(TERM="xterm-256color", TERMINFO="/custom/path")
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERMINFO=$TERMINFO"',
            env,
        )
        assert r.returncode == 0
        assert "TERMINFO=/custom/path" in r.stdout


class TestTerminfoUnresolvable:
    """When TERM has no terminfo entry and no Ghostty bundle exists.

    HOME points at an empty tmp dir so a real ~/.terminfo (installed by
    setup.sh) can't make infocmp succeed and short-circuit the guard.
    """

    def test_falls_back_to_xterm_256color(self, tmp_path):
        """Simulates SSH to a Linux host: unknown TERM, no Ghostty app."""
        env = _base_env(TERM="xterm-ghostty", HOME=str(tmp_path))
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM"',
            env,
        )
        assert r.returncode == 0
        # On systems without the Ghostty app bundle, TERM should fall back
        ghostty_ti = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")
        if ghostty_ti.is_dir():
            # We're on a macOS host with Ghostty — it sets TERMINFO instead
            assert "TERM=xterm-ghostty" in r.stdout
        else:
            assert "TERM=xterm-256color" in r.stdout

    def test_bogus_term_falls_back(self):
        """A non-ghostty unknown TERM always falls back to xterm-256color."""
        env = _base_env(TERM="xterm-totally-bogus-12345")
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-256color" in r.stdout
        assert "TERMINFO=unset" in r.stdout


class TestTerminfoGhosttyBundle:
    """When the Ghostty app bundle terminfo exists on the host."""

    ghostty_ti = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_sets_terminfo_to_ghostty_bundle(self, tmp_path):
        """On a macOS host with Ghostty, TERMINFO should point to the bundle."""
        env = _base_env(TERM="xterm-ghostty", HOME=str(tmp_path))
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert f"TERMINFO={self.ghostty_ti}" in r.stdout

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_term_preserved_when_bundle_found(self, tmp_path):
        """TERM should remain xterm-ghostty when the bundle resolves it."""
        env = _base_env(TERM="xterm-ghostty", HOME=str(tmp_path))
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-ghostty" in r.stdout

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_terminfo_resolves_after_fixup(self, tmp_path):
        """After the guard, infocmp should succeed for xterm-ghostty."""
        env = _base_env(TERM="xterm-ghostty", HOME=str(tmp_path))
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'infocmp "$TERM" >/dev/null 2>&1 && echo OK || echo FAIL',
            env,
        )
        assert r.returncode == 0
        assert "OK" in r.stdout


class TestNoInfocmp:
    """When infocmp is not available, the guard should be a no-op."""

    def test_term_unchanged_without_infocmp(self, tmp_path):
        """If infocmp isn't in PATH, don't touch TERM or TERMINFO."""
        # Build a PATH that has bash but not infocmp.
        bash_path = subprocess.run(
            ["which", "bash"], capture_output=True, text=True
        ).stdout.strip()
        bash_dir = str(Path(bash_path).parent)
        infocmp_result = subprocess.run(
            ["which", "infocmp"], capture_output=True, text=True
        )
        infocmp_dir = (
            str(Path(infocmp_result.stdout.strip()).parent)
            if infocmp_result.returncode == 0
            else None
        )
        if infocmp_dir == bash_dir:
            pytest.skip("infocmp and bash share the same directory")
        env = _base_env(TERM="xterm-ghostty", PATH=bash_dir)
        r = _run_bash_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-ghostty" in r.stdout
        assert "TERMINFO=unset" in r.stdout
