"""Tests for the zsh -> bash cutover (#15).

Covers the literal contracts the cutover introduced: the tmux
default-shell binding, the Starship config (palette references, hive
segments, and the PR-cache path shared with hive.py), the bootstrap
script, and bash/bashrc's environment setup.
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BASHRC = REPO_ROOT / "bash" / "bashrc"
TMUX_CONF = REPO_ROOT / "tmux" / "tmux.conf"
STARSHIP_TOML = REPO_ROOT / "starship" / "starship.toml"
BOOTSTRAP = REPO_ROOT / "setup" / "bootstrap-macos.sh"
HIVE_PY = REPO_ROOT / "scripts" / "hive.py"

HOMEBREW_BASH_PATHS = {"/opt/homebrew/bin/bash", "/usr/local/bin/bash"}


class TestTmuxDefaultShell:
    """tmux panes must run Homebrew bash, never /bin/bash 3.2."""

    def _default_shell_lines(self):
        text = TMUX_CONF.read_text()
        return [l for l in text.splitlines() if "default-shell" in l]

    def test_guard_and_target_agree(self):
        """Each if-shell tests the exact path it then sets."""
        pattern = re.compile(
            r"^if-shell 'test -x (\S+)' 'set -g default-shell (\S+)'$"
        )
        lines = self._default_shell_lines()
        assert lines, "tmux.conf no longer sets default-shell"
        seen = set()
        for line in lines:
            m = pattern.match(line)
            assert m, f"default-shell must stay guarded by if-shell: {line!r}"
            assert m.group(1) == m.group(2), f"guard/target mismatch: {line!r}"
            seen.add(m.group(2))
        assert seen == HOMEBREW_BASH_PATHS

    def test_native_prefix_wins(self):
        """/opt/homebrew must come last so it wins when both exist."""
        lines = self._default_shell_lines()
        assert "/opt/homebrew/bin/bash" in lines[-1]

    def test_never_system_bash(self):
        text = TMUX_CONF.read_text()
        assert not re.search(r"default-shell +/bin/bash\b", text)


class TestStarshipConfig:
    """starship/starship.toml parses and its internal references resolve."""

    def setup_method(self):
        self.config = tomllib.loads(STARSHIP_TOML.read_text())

    def test_palette_reference_resolves(self):
        palette = self.config["palette"]
        assert palette in self.config["palettes"]

    def test_all_style_colors_defined_in_palette(self):
        """Every fg:<name> used by a module resolves to a palette entry."""
        palette = self.config["palettes"][self.config["palette"]]
        text = STARSHIP_TOML.read_text()
        used = set(re.findall(r"fg:([a-z_]+)", text))
        assert used, "expected palette-named colors in module styles"
        missing = used - set(palette)
        assert not missing, f"colors used but not in palette: {missing}"

    def test_hive_segments_present_and_referenced(self):
        custom = self.config["custom"]
        assert "hive_badge" in custom
        assert "hive_pr" in custom
        assert "${custom.hive_badge}" in self.config["format"]
        assert "${custom.hive_pr}" in self.config["right_format"]

    def test_hive_pr_cache_path_matches_hive_py(self):
        """Both ends of the PR-cache contract: hive.py writes it,
        the hive_pr segment reads it."""
        hive_text = HIVE_PY.read_text()
        assert "_TMUX_DIR = Path('/tmp/hive-tmux')" in hive_text
        assert "_TMUX_DIR / f'{hive_name}-{number}.pr'" in hive_text

        starship_path = "/tmp/hive-tmux/${HIVE_NAME}-${HIVE_NUMBER}.pr"
        hive_pr = self.config["custom"]["hive_pr"]
        assert starship_path in hive_pr["command"]
        assert starship_path in hive_pr["when"]

    def test_status_and_time_enabled(self):
        """These modules are disabled by default; the port needs them on."""
        assert self.config["status"]["disabled"] is False
        assert self.config["time"]["disabled"] is False

    def test_palette_is_theme_adaptive_ansi_only(self):
        """Day/night legibility contract: only named ANSI colors, which
        each Ghostty theme maps against its own background.  A numeric
        256-cube or hex value is fixed across themes and will wash out
        on one of the two backgrounds."""
        ansi = re.compile(r"(bright-)?(black|red|green|yellow|blue|purple|cyan|white)")
        palette = self.config["palettes"][self.config["palette"]]
        for name, value in palette.items():
            assert ansi.fullmatch(value), (
                f"palette color {name} = {value!r} is not a named ANSI color"
            )


class TestBootstrap:
    """bootstrap-macos.sh installs and registers the bash stack."""

    def setup_method(self):
        self.text = BOOTSTRAP.read_text()

    def test_installs_bash_stack(self):
        assert re.search(r"^brew install bash bash-completion@2 starship$",
                         self.text, re.MULTILINE)

    def test_no_zsh_era_installs(self):
        assert "oh-my-zsh" not in self.text.lower().replace("_", "-")
        assert "powerlevel10k" not in self.text

    def test_registers_and_switches_login_shell(self):
        assert "/etc/shells" in self.text
        assert re.search(r'chsh -s "\$BREW_BASH"', self.text)


class TestBashrcEnvironment:
    """bash/bashrc environment setup works when sourced for real."""

    def _run(self, snippet, home, extra_env=None):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "TERM": "xterm-256color",
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", "--norc", "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_noninteractive_source_sets_root_and_path(self, tmp_path):
        r = self._run(
            f'source "{BASHRC}"; echo "ROOT=$TERM_PUBLIC_ROOT"; echo "PATH=$PATH"',
            tmp_path,
        )
        assert r.returncode == 0
        assert f"ROOT={REPO_ROOT}" in r.stdout
        assert f"{tmp_path}/bin" in r.stdout

    def test_root_resolves_through_symlink(self, tmp_path):
        """~/.bashrc is a symlink into the repo; the root walk must follow it."""
        link = tmp_path / ".bashrc"
        link.symlink_to(BASHRC)
        r = self._run('source "$HOME/.bashrc"; echo "ROOT=$TERM_PUBLIC_ROOT"',
                      tmp_path)
        assert r.returncode == 0
        assert f"ROOT={REPO_ROOT}" in r.stdout

    def test_noninteractive_skips_interactive_chrome(self, tmp_path):
        """vi mode must not leak into non-interactive shells."""
        r = self._run(
            f'source "{BASHRC}"; set -o | grep "^vi"',
            tmp_path,
        )
        assert r.returncode == 0
        assert "off" in r.stdout

    def test_interactive_shell_starts_cleanly(self, tmp_path):
        """An interactive shell sourcing bashrc reaches the prompt."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "TERM": "xterm-256color",
        }
        r = subprocess.run(
            ["bash", "--rcfile", str(BASHRC), "-i", "-c", "echo READY"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 0
        assert "READY" in r.stdout

    def test_prior_bashrc_backup_sourced(self, tmp_path):
        """A .bashrc.bak stays effective, not just archived (#19 review)."""
        (tmp_path / ".bashrc.bak").write_text("export FROM_BAK=1\n")
        r = self._run(f'source "{BASHRC}"; echo "BAK=$FROM_BAK"', tmp_path)
        assert r.returncode == 0
        assert "BAK=1" in r.stdout

    def test_bak_sourcing_does_not_recurse(self, tmp_path):
        """A backup that re-sources ~/.bashrc must not loop forever."""
        link = tmp_path / ".bashrc"
        link.symlink_to(BASHRC)
        (tmp_path / ".bashrc.bak").write_text(
            'source "$HOME/.bashrc"\nexport FROM_BAK=1\n')
        r = subprocess.run(
            ["bash", "--norc", "-c",
             'source "$HOME/.bashrc"; echo "BAK=$FROM_BAK"'],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(tmp_path), "TERM": "xterm-256color"},
            timeout=15,
        )
        assert r.returncode == 0
        assert "BAK=1" in r.stdout

    def test_env_local_overlay_sourced(self, tmp_path):
        """local/env.local from the resolved repo root is applied."""
        # Build a fake repo so the test doesn't depend on the real local/.
        repo = tmp_path / "repo"
        (repo / "bash").mkdir(parents=True)
        (repo / "local").mkdir()
        (repo / "bash" / "bashrc").write_text(BASHRC.read_text())
        (repo / "local" / "env.local").write_text("export FROM_OVERLAY=yes\n")
        r = self._run(
            f'source "{repo / "bash" / "bashrc"}"; echo "OVERLAY=$FROM_OVERLAY"',
            tmp_path,
        )
        assert r.returncode == 0
        assert "OVERLAY=yes" in r.stdout
