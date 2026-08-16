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
    def _config(self, env) -> Path:
        return Path(env['HOME']) / '.claude.json'

    def test_flips_theme_for_future_sessions(self, fake_mac):
        env, _ = fake_mac
        self._config(env).write_text('{"theme": "dark", "other": 1}')
        run(env, 'day')
        cfg = json.loads(self._config(env).read_text())
        assert cfg['theme'] == 'light'
        assert cfg['other'] == 1  # unrelated keys survive
        run(env, 'night')
        assert json.loads(self._config(env).read_text())['theme'] == 'dark'

    def test_unset_theme_means_dark(self, fake_mac):
        env, _ = fake_mac
        self._config(env).write_text('{}')
        run(env, 'night')
        cfg = json.loads(self._config(env).read_text())
        assert cfg.get('theme', 'dark') == 'dark'
        run(env, 'day')
        assert json.loads(self._config(env).read_text())['theme'] == 'light'

    def test_preserves_variant_suffix(self, fake_mac):
        env, _ = fake_mac
        self._config(env).write_text('{"theme": "dark-daltonized"}')
        run(env, 'day')
        assert (json.loads(self._config(env).read_text())['theme']
                == 'light-daltonized')

    def test_leaves_auto_and_custom_alone(self, fake_mac):
        env, _ = fake_mac
        for theme in ('auto', 'custom:dracula'):
            self._config(env).write_text(json.dumps({'theme': theme}))
            run(env, 'day')
            assert json.loads(self._config(env).read_text())['theme'] == theme

    def test_missing_config_is_fine(self, fake_mac):
        env, _ = fake_mac
        result = run(env, 'day')
        assert result.returncode == 0
        assert not self._config(env).exists()

    def test_status_does_not_touch_theme(self, fake_mac):
        env, _ = fake_mac
        self._config(env).write_text('{"theme": "dark"}')
        run(env, 'status')
        assert json.loads(self._config(env).read_text())['theme'] == 'dark'


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
