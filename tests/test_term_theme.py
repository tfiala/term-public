#!/usr/bin/env python3
"""Tests for scripts/term-theme (day/night mode toggle, issue #14).

The script's only external effect is running `osascript` against System
Events' appearance preferences, so the tests stub `osascript` with a shim
that persists dark-mode state in a file and answers the same statements the
real one would. The shim only matches the exact AppleScript phrases the
script is expected to emit — if the script's AppleScript drifts, the shim
returns nothing and the tests fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

TERM_THEME = Path(__file__).resolve().parents[1] / 'scripts' / 'term-theme'

# Emulates `osascript -e <stmt> [-e <stmt> ...]`: applies each statement to
# the state file, prints the result of the last one (like real osascript).
FAKE_OSASCRIPT = """#!/bin/bash
state=$(cat "$FAKE_DARK_MODE_FILE")
result=
while [ $# -gt 0 ]; do
  if [ "$1" = -e ]; then shift; fi
  case $1 in
    *"tell application \\"System Events\\" to tell appearance preferences to "*)
      case $1 in
        *"set dark mode to not dark mode")
          if [ "$state" = true ]; then state=false; else state=true; fi
          result=$state ;;
        *"set dark mode to true") state=true; result=$state ;;
        *"set dark mode to false") state=false; result=$state ;;
        *"get dark mode") result=$state ;;
      esac ;;
  esac
  shift
done
echo "$state" > "$FAKE_DARK_MODE_FILE"
echo "$result"
"""


# Stub tmux: by default there is no server (`tmux ls` fails), so the hive
# restyle hook must stay quiet. Tests that want a live server overwrite the
# stub with the WITH_SERVER variant.
FAKE_TMUX_NO_SERVER = """#!/bin/bash
exit 1
"""

FAKE_TMUX_WITH_SERVER = """#!/bin/bash
exit 0
"""

# Stub hive: records every invocation so tests can assert the restyle hook.
FAKE_HIVE = """#!/bin/bash
echo "$@" >> "$FAKE_HIVE_LOG"
"""


@pytest.fixture
def fake_mac(tmp_path):
    """A PATH with a stubbed osascript plus the dark-mode state file.

    HOME and XDG_CACHE_HOME point into tmp_path so the script's side
    effects (mode state file, claude theme edit) never touch the real
    environment, and tmux/hive are stubbed so the restyle hook never
    reaches a real tmux server.
    """
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    stub = bindir / 'osascript'
    stub.write_text(FAKE_OSASCRIPT)
    stub.chmod(0o755)
    tmux = bindir / 'tmux'
    tmux.write_text(FAKE_TMUX_NO_SERVER)
    tmux.chmod(0o755)
    hive_stub = bindir / 'hive'
    hive_stub.write_text(FAKE_HIVE)
    hive_stub.chmod(0o755)
    state = tmp_path / 'dark-mode'
    state.write_text('true\n')
    (tmp_path / 'home').mkdir()
    env = dict(os.environ)
    env['PATH'] = f'{bindir}:{env["PATH"]}'
    env['FAKE_DARK_MODE_FILE'] = str(state)
    env['FAKE_HIVE_LOG'] = str(tmp_path / 'hive-log')
    env['HOME'] = str(tmp_path / 'home')
    env['XDG_CACHE_HOME'] = str(tmp_path / 'cache')
    return env, state


def run(env, *args):
    return subprocess.run(
        [str(TERM_THEME), *args], env=env, capture_output=True, text=True)


def dark_mode(state: Path) -> str:
    return state.read_text().strip()


def test_day_switches_to_light(fake_mac):
    env, state = fake_mac
    result = run(env, 'day')
    assert result.returncode == 0
    assert result.stdout.strip() == 'day'
    assert dark_mode(state) == 'false'


def test_night_switches_to_dark(fake_mac):
    env, state = fake_mac
    state.write_text('false\n')
    result = run(env, 'night')
    assert result.returncode == 0
    assert result.stdout.strip() == 'night'
    assert dark_mode(state) == 'true'


def test_day_is_idempotent(fake_mac):
    env, state = fake_mac
    state.write_text('false\n')
    result = run(env, 'day')
    assert result.returncode == 0
    assert result.stdout.strip() == 'day'
    assert dark_mode(state) == 'false'


def test_toggle_flips_each_way(fake_mac):
    env, state = fake_mac
    result = run(env, 'toggle')
    assert result.returncode == 0
    assert result.stdout.strip() == 'day'
    assert dark_mode(state) == 'false'
    result = run(env, 'toggle')
    assert result.returncode == 0
    assert result.stdout.strip() == 'night'
    assert dark_mode(state) == 'true'


def test_no_argument_toggles(fake_mac):
    env, state = fake_mac
    result = run(env)
    assert result.returncode == 0
    assert result.stdout.strip() == 'day'
    assert dark_mode(state) == 'false'


def test_status_reports_without_changing(fake_mac):
    env, state = fake_mac
    result = run(env, 'status')
    assert result.returncode == 0
    assert result.stdout.strip() == 'night'
    assert dark_mode(state) == 'true'


def test_unknown_mode_fails_with_usage(fake_mac):
    env, state = fake_mac
    result = run(env, 'dusk')
    assert result.returncode == 2
    assert 'unknown mode' in result.stderr
    assert 'usage:' in result.stderr
    assert dark_mode(state) == 'true'  # untouched


def test_help_exits_zero(fake_mac):
    env, _ = fake_mac
    result = run(env, '--help')
    assert result.returncode == 0
    assert 'usage:' in result.stderr


def test_unexpected_state_is_an_error_not_a_mode(fake_mac):
    # A broken probe must never read as a valid observation.
    env, state = fake_mac
    state.write_text('maybe\n')
    result = run(env, 'status')
    assert result.returncode == 1
    assert 'unexpected appearance state' in result.stderr
    assert result.stdout.strip() == ''


def test_osascript_failure_surfaces_stderr(fake_mac, tmp_path):
    env, _ = fake_mac
    failing = tmp_path / 'bin' / 'osascript'
    failing.write_text(
        '#!/bin/bash\n'
        'echo "execution error: Not authorized to send Apple events'
        ' to System Events. (-1743)" >&2\n'
        'exit 1\n')
    result = run(env, 'status')
    assert result.returncode == 1
    assert 'osascript failed' in result.stderr
    assert 'Automation' in result.stderr  # the remediation hint


def mode_file(env) -> Path:
    return Path(env['XDG_CACHE_HOME']) / 'term-theme' / 'mode'


class TestModeStateFile:
    def test_written_on_switch(self, fake_mac):
        env, _ = fake_mac
        run(env, 'day')
        assert mode_file(env).read_text() == 'day\n'
        run(env, 'night')
        assert mode_file(env).read_text() == 'night\n'

    def test_written_on_status(self, fake_mac):
        env, _ = fake_mac
        run(env, 'status')
        assert mode_file(env).read_text() == 'night\n'


class TestClaudeThemeSync:
    """The theme must land in ~/.claude/settings.json — the authoritative
    store for /config preferences since Claude Code 2.1.119. The legacy
    ~/.claude.json is read-only input (variant seeding), never a target.
    """

    def _config(self, env, content=None) -> Path:
        settings = Path(env['HOME']) / '.claude' / 'settings.json'
        settings.parent.mkdir(exist_ok=True)
        if content is not None:
            settings.write_text(content)
        return settings

    def _legacy(self, env) -> Path:
        return Path(env['HOME']) / '.claude.json'

    def test_flips_theme_for_future_sessions(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{"theme": "dark", "model": "opus"}')
        run(env, 'day')
        cfg = json.loads(cfg_path.read_text())
        assert cfg['theme'] == 'light'
        assert cfg['model'] == 'opus'  # unrelated settings survive
        run(env, 'night')
        assert json.loads(cfg_path.read_text())['theme'] == 'dark'

    def test_unset_theme_means_dark(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{}')
        run(env, 'night')
        cfg = json.loads(cfg_path.read_text())
        assert cfg.get('theme', 'dark') == 'dark'
        run(env, 'day')
        assert json.loads(cfg_path.read_text())['theme'] == 'light'

    def test_preserves_variant_suffix(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{"theme": "dark-daltonized"}')
        run(env, 'day')
        assert json.loads(cfg_path.read_text())['theme'] == 'light-daltonized'

    def test_leaves_auto_and_custom_alone(self, fake_mac):
        env, _ = fake_mac
        for theme in ('auto', 'custom:dracula'):
            cfg_path = self._config(env, json.dumps({'theme': theme}))
            run(env, 'day')
            assert json.loads(cfg_path.read_text())['theme'] == theme

    def test_missing_claude_dir_is_fine(self, fake_mac):
        env, _ = fake_mac
        result = run(env, 'day')
        assert result.returncode == 0
        assert not (Path(env['HOME']) / '.claude').exists()

    def test_creates_settings_when_dir_exists(self, fake_mac):
        # An installed Claude Code without an explicit theme setting still
        # gets the binding — in the authoritative file.
        env, _ = fake_mac
        cfg_path = self._config(env)
        assert not cfg_path.exists()
        run(env, 'day')
        assert json.loads(cfg_path.read_text())['theme'] == 'light'

    def test_seeds_variant_from_legacy_config(self, fake_mac):
        # Pre-2.1.119 the theme lived in ~/.claude.json; a daltonized
        # variant chosen there must carry into the migrated setting.
        env, _ = fake_mac
        cfg_path = self._config(env, '{}')
        self._legacy(env).write_text('{"theme": "dark-daltonized"}')
        run(env, 'day')
        assert json.loads(cfg_path.read_text())['theme'] == 'light-daltonized'

    def test_legacy_auto_respected_when_settings_unset(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{}')
        self._legacy(env).write_text('{"theme": "auto"}')
        run(env, 'day')
        assert 'theme' not in json.loads(cfg_path.read_text())

    def test_legacy_file_is_never_written(self, fake_mac):
        env, _ = fake_mac
        self._config(env, '{"theme": "dark"}')
        legacy = self._legacy(env)
        legacy.write_text('{"theme": "dark", "sessions": {}}')
        run(env, 'day')
        assert json.loads(legacy.read_text()) == {
            'theme': 'dark', 'sessions': {}}

    def test_unparseable_settings_left_untouched(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{not json')
        result = run(env, 'day')
        assert result.returncode == 0
        assert cfg_path.read_text() == '{not json'

    def test_status_does_not_touch_theme(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '{"theme": "dark"}')
        run(env, 'status')
        assert json.loads(cfg_path.read_text())['theme'] == 'dark'


class TestCodexThemeSync:
    """Codex's accent palette comes from its syntax theme (tui.theme in
    ~/.codex/config.toml) — its light/dark background detection gets no
    answer inside tmux and defaults to the dark catppuccin-mocha — so
    term-theme flips the catppuccin pair per mode.  Both pair names were
    verified against the bundled list in codex's /theme picker (v0.147.0).
    """

    # The shape of the user's real config: a [tui] table with unrelated
    # keys plus a [tui.*] sub-table that must not receive the theme.
    REALISTIC = (
        'model_reasoning_effort = "xhigh"\n'
        '\n'
        '[tui]\n'
        'status_line = ["model-with-reasoning", "git-branch"]\n'
        '\n'
        '[tui.model_availability_nux]\n'
        '"gpt-5.5" = 4\n'
    )

    def _config(self, env, content=None) -> Path:
        cfg = Path(env['HOME']) / '.codex' / 'config.toml'
        cfg.parent.mkdir(exist_ok=True)
        if content is not None:
            cfg.write_text(content)
        return cfg

    def test_day_flips_unset_theme_to_latte(self, fake_mac):
        """No tui.theme means codex's dark default — day must override it,
        landing in [tui] itself, never a [tui.*] sub-table."""
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'
        assert cfg['tui']['status_line'] == [
            'model-with-reasoning', 'git-branch']  # unrelated keys survive
        assert 'theme' not in cfg['tui']['model_availability_nux']
        assert cfg['model_reasoning_effort'] == 'xhigh'

    def test_night_flips_latte_back_to_mocha(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '[tui]\ntheme = "catppuccin-latte"\n')
        run(env, 'night')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-mocha'

    def test_night_with_unset_theme_writes_nothing(self, fake_mac):
        """Unset already means the dark default — no gratuitous edit."""
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'night')
        assert cfg_path.read_text() == self.REALISTIC

    def test_pinned_theme_is_respected(self, fake_mac):
        """A /theme pick outside the pair is deliberate — leave it."""
        env, _ = fake_mac
        content = '[tui]\ntheme = "zenburn"\n'
        cfg_path = self._config(env, content)
        for mode in ('day', 'night'):
            run(env, mode)
            assert cfg_path.read_text() == content

    def test_missing_codex_dir_is_fine(self, fake_mac):
        env, _ = fake_mac
        result = run(env, 'day')
        assert result.returncode == 0
        assert not (Path(env['HOME']) / '.codex').exists()

    def test_creates_config_when_dir_exists(self, fake_mac):
        """An installed codex without a config.toml still gets the binding."""
        env, _ = fake_mac
        cfg_path = self._config(env)
        assert not cfg_path.exists()
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'

    def test_unparseable_config_left_untouched(self, fake_mac):
        env, _ = fake_mac
        content = 'not [ valid toml\n'
        cfg_path = self._config(env, content)
        result = run(env, 'day')
        assert result.returncode == 0
        assert cfg_path.read_text() == content

    def test_status_does_not_touch_theme(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'status')
        assert cfg_path.read_text() == self.REALISTIC


class TestHiveRestyle:
    def test_skipped_without_tmux_server(self, fake_mac):
        env, _ = fake_mac
        run(env, 'day')
        assert not Path(env['FAKE_HIVE_LOG']).exists()

    def test_invoked_with_tmux_server(self, fake_mac, tmp_path):
        env, _ = fake_mac
        (tmp_path / 'bin' / 'tmux').write_text(FAKE_TMUX_WITH_SERVER)
        run(env, 'day')
        assert Path(env['FAKE_HIVE_LOG']).read_text().strip() == 'tmux restyle'

    def test_skipped_on_status(self, fake_mac, tmp_path):
        env, _ = fake_mac
        (tmp_path / 'bin' / 'tmux').write_text(FAKE_TMUX_WITH_SERVER)
        run(env, 'status')
        assert not Path(env['FAKE_HIVE_LOG']).exists()
