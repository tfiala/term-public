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


@pytest.fixture
def fake_mac(tmp_path):
    """A PATH with a stubbed osascript plus the dark-mode state file."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    stub = bindir / 'osascript'
    stub.write_text(FAKE_OSASCRIPT)
    stub.chmod(0o755)
    state = tmp_path / 'dark-mode'
    state.write_text('true\n')
    env = dict(os.environ)
    env['PATH'] = f'{bindir}:{env["PATH"]}'
    env['FAKE_DARK_MODE_FILE'] = str(state)
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
