"""Tests for the TERM/TERMINFO fixup at the top of zsh/zshrc."""

import os
import subprocess
from pathlib import Path

import pytest


ZSHRC = str(Path(__file__).resolve().parents[1] / "zsh" / "zshrc")

# The terminfo guard block, extracted so tests can run it in isolation
# without sourcing the rest of zshrc (which needs oh-my-zsh, etc.).
_TERMINFO_GUARD = """\
if (( $+commands[infocmp] )) && ! infocmp "$TERM" &>/dev/null; then
  if [[ -d /Applications/Ghostty.app/Contents/Resources/terminfo ]]; then
    export TERMINFO=/Applications/Ghostty.app/Contents/Resources/terminfo
  else
    export TERM=xterm-256color
  fi
fi
"""


def _run_zsh_snippet(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a zsh snippet with the given environment, return the result."""
    return subprocess.run(
        ["zsh", "-f", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
    )


def _base_env(**overrides: str) -> dict[str, str]:
    """Minimal env for zsh with optional overrides."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    env.update(overrides)
    return env


class TestTerminfoGuardInZshrc:
    """Verify the terminfo guard block exists in zshrc with correct structure."""

    def test_guard_block_present(self):
        text = Path(ZSHRC).read_text()
        assert "infocmp" in text
        assert "xterm-256color" in text

    def test_guard_before_p10k_instant_prompt(self):
        text = Path(ZSHRC).read_text()
        infocmp_pos = text.index("infocmp")
        p10k_pos = text.index("p10k-instant-prompt")
        assert infocmp_pos < p10k_pos, (
            "terminfo guard must run before p10k instant prompt"
        )


class TestTerminfoResolvable:
    """When the current TERM has a valid terminfo entry, the guard is a no-op."""

    def test_known_term_unchanged(self):
        """xterm-256color is universally available — should not be altered."""
        env = _base_env(TERM="xterm-256color")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-256color" in r.stdout
        assert "TERMINFO=unset" in r.stdout

    def test_terminfo_env_not_overwritten_when_already_set(self):
        """If TERMINFO is already set and infocmp succeeds, leave it alone."""
        env = _base_env(TERM="xterm-256color", TERMINFO="/custom/path")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERMINFO=$TERMINFO"',
            env,
        )
        assert r.returncode == 0
        assert "TERMINFO=/custom/path" in r.stdout


class TestTerminfoUnresolvable:
    """When TERM has no terminfo entry and no Ghostty bundle exists."""

    def test_falls_back_to_xterm_256color(self):
        """Simulates SSH to a Linux host: unknown TERM, no Ghostty app."""
        env = _base_env(TERM="xterm-ghostty")
        r = _run_zsh_snippet(
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
        """A completely unknown TERM value should trigger the fallback."""
        ghostty_ti = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")
        env = _base_env(TERM="xterm-totally-bogus-12345")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        if ghostty_ti.is_dir():
            # Ghostty bundle exists — guard sets TERMINFO rather than
            # changing TERM, even for non-ghostty TERM values.
            assert f"TERMINFO={ghostty_ti}" in r.stdout
        else:
            assert "TERM=xterm-256color" in r.stdout


class TestTerminfoGhosttyBundle:
    """When the Ghostty app bundle terminfo exists on the host."""

    ghostty_ti = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_sets_terminfo_to_ghostty_bundle(self):
        """On a macOS host with Ghostty, TERMINFO should point to the bundle."""
        env = _base_env(TERM="xterm-ghostty")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert f"TERMINFO={self.ghostty_ti}" in r.stdout

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_term_preserved_when_bundle_found(self):
        """TERM should remain xterm-ghostty when the bundle resolves it."""
        env = _base_env(TERM="xterm-ghostty")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-ghostty" in r.stdout

    @pytest.mark.skipif(
        not Path("/Applications/Ghostty.app/Contents/Resources/terminfo").is_dir(),
        reason="Ghostty app bundle not installed",
    )
    def test_terminfo_resolves_after_fixup(self):
        """After the guard, infocmp should succeed for xterm-ghostty."""
        env = _base_env(TERM="xterm-ghostty")
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'infocmp "$TERM" &>/dev/null && echo OK || echo FAIL',
            env,
        )
        assert r.returncode == 0
        assert "OK" in r.stdout


class TestNoInfocmp:
    """When infocmp is not available, the guard should be a no-op."""

    def test_term_unchanged_without_infocmp(self, tmp_path):
        """If infocmp isn't in PATH, don't touch TERM or TERMINFO."""
        # Create a shadow directory with a fake infocmp that doesn't exist,
        # effectively hiding the real one.  We prepend it so zsh's
        # $+commands[infocmp] check fails.
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        # Build a PATH that has zsh but not infocmp: put shadow first
        # (no infocmp there) then only the directory containing zsh.
        zsh_path = subprocess.run(
            ["which", "zsh"], capture_output=True, text=True
        ).stdout.strip()
        zsh_dir = str(Path(zsh_path).parent)
        # Exclude directories containing infocmp
        infocmp_result = subprocess.run(
            ["which", "infocmp"], capture_output=True, text=True
        )
        infocmp_dir = str(Path(infocmp_result.stdout.strip()).parent) if infocmp_result.returncode == 0 else None
        path_dirs = [zsh_dir]
        if infocmp_dir and infocmp_dir != zsh_dir:
            # infocmp is in a different dir than zsh, so just use zsh's dir
            pass
        else:
            # infocmp is in the same dir as zsh — can't easily separate;
            # skip this test on such systems
            pytest.skip("infocmp and zsh share the same directory")
        env = _base_env(TERM="xterm-ghostty", PATH=":".join(path_dirs))
        r = _run_zsh_snippet(
            _TERMINFO_GUARD + 'echo "TERM=$TERM TERMINFO=${TERMINFO:-unset}"',
            env,
        )
        assert r.returncode == 0
        assert "TERM=xterm-ghostty" in r.stdout
        assert "TERMINFO=unset" in r.stdout
