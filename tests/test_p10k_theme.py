#!/usr/bin/env python3
"""Tests for the day/night palette selection in p10k.zsh.

The config must be sourceable outside a p10k session (zsh -f), where it
only defines functions and variables. Mode detection is driven by a
stubbed `defaults` on a minimal PATH; when no stub is given, PATH contains
only the (empty) stub dir so the host's real `defaults` can never leak in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

P10K = Path(__file__).resolve().parents[1] / 'p10k.zsh'

pytestmark = pytest.mark.skipif(
    shutil.which('zsh') is None, reason='zsh not installed')

DEFAULTS_DARK = """#!/bin/bash
echo Dark
"""

DEFAULTS_LIGHT = """#!/bin/bash
exit 1
"""

SNIPPET = (
    f'source {P10K} && '
    'print -r -- "$_term_public_p10k_mode $POWERLEVEL9K_BACKGROUND"'
)


def _env(tmp_path, defaults=None):
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    env = {
        'HOME': str(tmp_path / 'home'),
        'XDG_CACHE_HOME': str(tmp_path / 'cache'),
        'PATH': str(bindir),
        # The config contains $'\uNNNN' escapes, which zsh rejects outside
        # a UTF-8 locale ("character not in range").
        'LC_ALL': (os.environ.get('LC_ALL') or os.environ.get('LANG')
                   or 'C.UTF-8'),
    }
    (tmp_path / 'home').mkdir(exist_ok=True)
    if defaults is not None:
        stub = bindir / 'defaults'
        stub.write_text(defaults)
        stub.chmod(0o755)
        # touch(1) is needed by the watch-mode tests.
        env['PATH'] += ':/usr/bin:/bin'
    return env


def _run(env, snippet):
    return subprocess.run(
        [shutil.which('zsh'), '-f', '-c', snippet],
        capture_output=True, text=True, env=env)


def _mode_file(env) -> Path:
    mode_dir = Path(env['XDG_CACHE_HOME']) / 'term-theme'
    mode_dir.mkdir(parents=True, exist_ok=True)
    return mode_dir / 'mode'


def test_dark_appearance_selects_night(tmp_path):
    r = _run(_env(tmp_path, DEFAULTS_DARK), SNIPPET)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'night 236'


def test_light_appearance_selects_day(tmp_path):
    r = _run(_env(tmp_path, DEFAULTS_LIGHT), SNIPPET)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'day 254'


def test_state_file_fallback_without_defaults(tmp_path):
    env = _env(tmp_path)
    _mode_file(env).write_text('day\n')
    r = _run(env, SNIPPET)
    assert r.stdout.strip() == 'day 254'


def test_defaults_to_night_without_any_source(tmp_path):
    r = _run(_env(tmp_path), SNIPPET)
    assert r.stdout.strip() == 'night 236'


def test_invalid_state_file_is_night(tmp_path):
    env = _env(tmp_path)
    _mode_file(env).write_text('dusk\n')
    r = _run(env, SNIPPET)
    assert r.stdout.strip() == 'night 236'


def test_watch_mode_applies_newer_state_file(tmp_path):
    # No mtime nudging: a write landing in the same second the shell
    # started must be enough (regression for the clock-watermark bug).
    env = _env(tmp_path, DEFAULTS_DARK)
    mode = _mode_file(env)
    snippet = (
        f'source {P10K} && '
        'print -r -- "before:$_term_public_p10k_mode" && '
        f'print day > {mode} && '
        '_term_public_p10k_watch_mode && '
        'print -r -- "after:$_term_public_p10k_mode $POWERLEVEL9K_BACKGROUND"'
    )
    r = _run(env, snippet)
    assert 'before:night' in r.stdout, r.stderr
    assert 'after:day 254' in r.stdout


def test_watch_mode_equal_mtime_flip_applies(tmp_path):
    # Regression: the state file already exists and is rewritten with an
    # mtime identical to the startup snapshot's (a flip landing in the
    # same second, pinned here with touch -t for determinism). The mtime
    # comparison alone cannot see this write — the content check must.
    env = _env(tmp_path, DEFAULTS_DARK)
    mode = _mode_file(env)
    stamp = '202601010000'
    snippet = (
        f'print night > {mode} && touch -t {stamp} {mode} && '
        f'source {P10K} && '
        'print -r -- "before:$_term_public_p10k_mode" && '
        f'print day > {mode} && touch -t {stamp} {mode} && '
        '_term_public_p10k_watch_mode && '
        'print -r -- "after:$_term_public_p10k_mode $POWERLEVEL9K_BACKGROUND"'
    )
    r = _run(env, snippet)
    assert 'before:night' in r.stdout, r.stderr
    assert 'after:day 254' in r.stdout


def test_watch_mode_same_content_rewrite_applies(tmp_path):
    # A stale 'day' file is rightly ignored at startup (the dark
    # appearance wins) — but a later term-theme rewrite of that same
    # content must still land, even though only the mtime changes.
    env = _env(tmp_path, DEFAULTS_DARK)
    mode = _mode_file(env)
    mode.write_text('day\n')
    os.utime(mode, (0, 0))
    snippet = (
        f'source {P10K} && '
        'print -r -- "before:$_term_public_p10k_mode" && '
        f'touch {mode} && '
        '_term_public_p10k_watch_mode && '
        'print -r -- "after:$_term_public_p10k_mode"'
    )
    r = _run(env, snippet)
    assert 'before:night' in r.stdout, r.stderr
    assert 'after:day' in r.stdout


def test_watch_mode_ignores_stale_state_file(tmp_path):
    env = _env(tmp_path, DEFAULTS_DARK)
    mode = _mode_file(env)
    mode.write_text('day\n')
    os.utime(mode, (0, 0))  # written long before this shell started
    snippet = (
        f'source {P10K} && _term_public_p10k_watch_mode; '
        'print -r -- "$_term_public_p10k_mode"')
    r = _run(env, snippet)
    assert r.stdout.strip() == 'night'


def test_git_formatter_colors_follow_mode(tmp_path):
    snippet = (
        f'source {P10K} && '
        'print -r -- "$_term_public_git_clean" && '
        '_term_public_p10k_apply_palette day && '
        'print -r -- "$_term_public_git_clean"')
    r = _run(_env(tmp_path, DEFAULTS_DARK), snippet)
    assert r.stdout.split() == ['%76F', '%28F']
