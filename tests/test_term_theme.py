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

    def test_unparseable_config_left_untouched_but_warned(self, fake_mac):
        """Never touch what we cannot parse — but never claim success."""
        env, _ = fake_mac
        content = 'not [ valid toml\n'
        cfg_path = self._config(env, content)
        result = run(env, 'day')
        assert result.returncode == 0
        assert cfg_path.read_text() == content
        assert 'not synced' in result.stderr

    def test_status_does_not_touch_theme(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'status')
        assert cfg_path.read_text() == self.REALISTIC

    # TOML allows several spellings of the same [tui].theme setting; the
    # semantic reader accepts them all, so the editor must too (#20
    # review). Each case parses to tui.theme = catppuccin-mocha.
    ALT_SPELLINGS = [
        '[ tui ]\ntheme = "catppuccin-mocha"\n',
        '["tui"]\ntheme = "catppuccin-mocha"\n',
        '[tui]\n"theme" = "catppuccin-mocha"\n',
        'tui.theme = "catppuccin-mocha"\n',
        'tui = { theme = "catppuccin-mocha", animations = true }\n',
    ]

    @pytest.mark.parametrize('content', ALT_SPELLINGS)
    def test_day_syncs_alternate_toml_spellings(self, fake_mac, content):
        env, _ = fake_mac
        cfg_path = self._config(env, content)
        result = run(env, 'day')
        assert result.returncode == 0
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'
        assert 'not synced' not in result.stderr

    def test_inline_table_preserves_siblings(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(
            env, 'tui = { theme = "catppuccin-mocha", animations = true }\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'
        assert cfg['tui']['animations'] is True

    def test_inline_table_without_theme_gains_it(self, fake_mac):
        """No theme in the inline table means the dark default applies —
        day must add the key without disturbing siblings."""
        env, _ = fake_mac
        cfg_path = self._config(env, 'tui = { animations = true }\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'
        assert cfg['tui']['animations'] is True

    def test_dotted_siblings_without_theme(self, fake_mac):
        """Dotted tui.* keys already define the table — a new [tui]
        header would be illegal, so the dotted form must be extended."""
        env, _ = fake_mac
        cfg_path = self._config(env, 'tui.animations = true\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'
        assert cfg['tui']['animations'] is True

    def test_failed_commit_preserves_original_and_warns(self, fake_mac):
        """The staged atomic write must never truncate the real config:
        when the commit cannot happen, the original bytes survive and
        the failure is surfaced."""
        env, _ = fake_mac
        content = '[tui]\ntheme = "catppuccin-mocha"\n'
        cfg_path = self._config(env, content)
        cfg_path.parent.chmod(0o500)  # staging in the config dir fails
        try:
            result = run(env, 'day')
        finally:
            cfg_path.parent.chmod(0o755)
        assert result.returncode == 0
        assert cfg_path.read_text() == content
        assert 'not synced' in result.stderr

    def test_symlinked_config_edited_through_not_replaced(self, fake_mac):
        """A dotfiles-managed symlink stays a symlink; the edit lands in
        its target."""
        env, _ = fake_mac
        target_dir = Path(env['HOME']) / 'dotfiles'
        target_dir.mkdir()
        target = target_dir / 'codex-config.toml'
        target.write_text('[tui]\ntheme = "catppuccin-mocha"\n')
        cfg_path = Path(env['HOME']) / '.codex' / 'config.toml'
        cfg_path.parent.mkdir()
        cfg_path.symlink_to(target)
        run(env, 'day')
        assert cfg_path.is_symlink()
        cfg = tomllib.loads(target.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'

    def test_file_mode_preserved(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '[tui]\ntheme = "catppuccin-mocha"\n')
        cfg_path.chmod(0o600)
        run(env, 'day')
        assert (cfg_path.stat().st_mode & 0o777) == 0o600
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'


class TestGrokThemeSync:
    """Grok Build draws its whole TUI palette from `ui.theme`
    (~/.grok/config.toml), so a config pinned to GrokDay keeps its
    dark-on-white text all night.  term-theme flips the grokday/groknight
    pair per mode.  Both names, their aliases, and the self-adapting
    `auto`/`terminal` values were read off the theme table in the grok
    binary's own bundled docs (06-theming.md, grok 1.2.x).
    """

    # The shape of the user's real config: the theme sits in a [ui] table
    # with unrelated keys, after an array-of-tables and other sections,
    # and with a [privacy] section following it.
    REALISTIC = (
        '[cli]\n'
        'installer = "internal"\n'
        '\n'
        '[[marketplace.sources]]\n'
        'name = "xAI Official"\n'
        '\n'
        '[models]\n'
        'default = "grok-4.6"\n'
        '\n'
        '[ui]\n'
        'max_thoughts_width = 120\n'
        'permission_mode = "always-approve"\n'
        '\n'
        '[privacy]\n'
        'privacy_banner_acked = "2026-09-19T06:12:15Z"\n'
    )

    def _config(self, env, content=None) -> Path:
        cfg = Path(env['HOME']) / '.grok' / 'config.toml'
        cfg.parent.mkdir(exist_ok=True)
        if content is not None:
            cfg.write_text(content)
        return cfg

    def test_day_flips_unset_theme_to_grokday(self, fake_mac):
        """No ui.theme means grok's GrokNight default — day must override
        it, landing in [ui] itself and disturbing no other section."""
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert cfg['ui']['max_thoughts_width'] == 120  # unrelated keys survive
        assert cfg['ui']['permission_mode'] == 'always-approve'
        assert 'theme' not in cfg['privacy']
        assert cfg['marketplace']['sources'][0]['name'] == 'xAI Official'

    def test_night_flips_grokday_back_to_groknight(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '[ui]\ntheme = "grokday"\n')
        run(env, 'night')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'groknight'

    def test_night_with_unset_theme_writes_nothing(self, fake_mac):
        """Unset already means the GrokNight default — no gratuitous edit."""
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'night')
        assert cfg_path.read_text() == self.REALISTIC

    # Grok accepts several names for each half of the pair, case-insensitively.
    # A config already naming this mode in any of them needs no rewrite...
    @pytest.mark.parametrize('mode,name', [
        ('day', 'grokday'), ('day', 'grok-day'), ('day', 'light'),
        ('day', 'day'), ('day', 'GrokDay'),
        ('night', 'groknight'), ('night', 'grok-night'), ('night', 'dark'),
        ('night', 'GROKNIGHT'),
    ])
    def test_alias_for_this_mode_is_left_as_written(self, fake_mac, mode, name):
        env, _ = fake_mac
        content = f'[ui]\ntheme = "{name}"\n'
        cfg_path = self._config(env, content)
        result = run(env, mode)
        assert cfg_path.read_text() == content
        assert 'not synced' not in result.stderr

    # ...but an alias for the OTHER mode is exactly what must flip.
    @pytest.mark.parametrize('mode,name,want', [
        ('day', 'grok-night', 'grokday'),
        ('day', 'dark', 'grokday'),
        ('day', 'GrokNight', 'grokday'),
        ('night', 'grok-day', 'groknight'),
        ('night', 'light', 'groknight'),
        ('night', 'Day', 'groknight'),
    ])
    def test_alias_for_other_mode_flips(self, fake_mac, mode, name, want):
        env, _ = fake_mac
        cfg_path = self._config(env, f'[ui]\ntheme = "{name}"\n')
        result = run(env, mode)
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == want
        assert 'not synced' not in result.stderr

    # `auto`/`system` follow the macOS appearance this script just flipped
    # (grok re-polls within seconds), and `terminal`/`transparent`/`native`
    # draw from the terminal's own ANSI palette that Ghostty already remaps.
    # Both are self-adapting: there is nothing to write, and writing would
    # take away the better behaviour.
    @pytest.mark.parametrize('name', [
        'auto', 'system', 'terminal', 'terminal-default', 'transparent',
        'native',
    ])
    def test_self_adapting_theme_is_left_alone(self, fake_mac, name):
        env, _ = fake_mac
        content = f'[ui]\ntheme = "{name}"\n'
        cfg_path = self._config(env, content)
        for mode in ('day', 'night'):
            result = run(env, mode)
            assert cfg_path.read_text() == content
            assert 'not synced' not in result.stderr

    @pytest.mark.parametrize('name', [
        'tokyonight', 'rosepine-moon', 'oscura-midnight',
    ])
    def test_pinned_theme_is_respected(self, fake_mac, name):
        """A /theme pick outside the pair is deliberate — leave it."""
        env, _ = fake_mac
        content = f'[ui]\ntheme = "{name}"\n'
        cfg_path = self._config(env, content)
        for mode in ('day', 'night'):
            run(env, mode)
            assert cfg_path.read_text() == content

    def test_missing_grok_dir_is_fine(self, fake_mac):
        env, _ = fake_mac
        result = run(env, 'day')
        assert result.returncode == 0
        assert not (Path(env['HOME']) / '.grok').exists()

    def test_creates_config_when_dir_exists(self, fake_mac):
        """An installed grok without a config.toml still gets the binding."""
        env, _ = fake_mac
        cfg_path = self._config(env)
        assert not cfg_path.exists()
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'

    def test_unparseable_config_left_untouched_but_warned(self, fake_mac):
        """Never touch what we cannot parse — but never claim success."""
        env, _ = fake_mac
        content = 'not [ valid toml\n'
        cfg_path = self._config(env, content)
        result = run(env, 'day')
        assert result.returncode == 0
        assert cfg_path.read_text() == content
        assert 'grok theme not synced' in result.stderr

    def test_status_does_not_touch_theme(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, self.REALISTIC)
        run(env, 'status')
        assert cfg_path.read_text() == self.REALISTIC

    # The same editor serves codex's [tui] and grok's [ui]; `ui` is a
    # suffix of `tui`, so a loose table pattern would have the grok hook
    # write codex's theme (and vice versa).  Neither may touch the other.
    def test_tui_table_is_not_mistaken_for_ui(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '[tui]\ntheme = "catppuccin-mocha"\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['tui']['theme'] == 'catppuccin-mocha'  # untouched
        assert cfg['ui']['theme'] == 'grokday'  # grok's own table, added

    def test_ui_sub_table_does_not_receive_the_theme(self, fake_mac):
        """[ui.contextual_hints] is not [ui] — the key must land in the
        parent table it is read from."""
        env, _ = fake_mac
        cfg_path = self._config(
            env, '[ui]\ncompact_mode = false\n\n'
                 '[ui.contextual_hints]\nundo = true\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert 'theme' not in cfg['ui']['contextual_hints']
        assert cfg['ui']['contextual_hints']['undo'] is True

    # TOML allows several spellings of the same [ui].theme setting; the
    # semantic reader accepts them all, so the editor must too.  Each case
    # parses to ui.theme = groknight.
    ALT_SPELLINGS = [
        '[ ui ]\ntheme = "groknight"\n',
        '["ui"]\ntheme = "groknight"\n',
        '[ui]\n"theme" = "groknight"\n',
        'ui.theme = "groknight"\n',
        'ui = { theme = "groknight", compact_mode = true }\n',
    ]

    @pytest.mark.parametrize('content', ALT_SPELLINGS)
    def test_day_syncs_alternate_toml_spellings(self, fake_mac, content):
        env, _ = fake_mac
        cfg_path = self._config(env, content)
        result = run(env, 'day')
        assert result.returncode == 0
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert 'not synced' not in result.stderr

    def test_dotted_siblings_without_theme(self, fake_mac):
        """Dotted ui.* keys already define the table — a new [ui] header
        would be illegal, so the dotted form must be extended."""
        env, _ = fake_mac
        cfg_path = self._config(env, 'ui.compact_mode = true\n')
        run(env, 'day')
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert cfg['ui']['compact_mode'] is True

    def test_symlinked_config_edited_through_not_replaced(self, fake_mac):
        """A dotfiles-managed symlink stays a symlink; the edit lands in
        its target."""
        env, _ = fake_mac
        target_dir = Path(env['HOME']) / 'dotfiles'
        target_dir.mkdir()
        target = target_dir / 'grok-config.toml'
        target.write_text('[ui]\ntheme = "groknight"\n')
        cfg_path = Path(env['HOME']) / '.grok' / 'config.toml'
        cfg_path.parent.mkdir()
        cfg_path.symlink_to(target)
        run(env, 'day')
        assert cfg_path.is_symlink()
        cfg = tomllib.loads(target.read_text())
        assert cfg['ui']['theme'] == 'grokday'

    def test_failed_commit_preserves_original_and_warns(self, fake_mac):
        """The staged atomic write must never truncate the real config."""
        env, _ = fake_mac
        content = '[ui]\ntheme = "groknight"\n'
        cfg_path = self._config(env, content)
        cfg_path.parent.chmod(0o500)  # staging in the config dir fails
        try:
            result = run(env, 'day')
        finally:
            cfg_path.parent.chmod(0o755)
        assert result.returncode == 0
        assert cfg_path.read_text() == content
        assert 'grok theme not synced' in result.stderr

    def test_file_mode_preserved(self, fake_mac):
        env, _ = fake_mac
        cfg_path = self._config(env, '[ui]\ntheme = "groknight"\n')
        cfg_path.chmod(0o600)
        run(env, 'day')
        assert (cfg_path.stat().st_mode & 0o777) == 0o600
        cfg = tomllib.loads(cfg_path.read_text())
        assert cfg['ui']['theme'] == 'grokday'


class TestGrokConfigResolution:
    """Deciding whether to flip grok's theme means resolving the theme grok
    will actually render, not reading one key in one file (PR #46 review).
    Three things make that non-trivial: the config root is `$GROK_HOME`
    when set; `ui.ui_theme` is a documented legacy alias read when
    `ui.theme` is absent; and `managed_config.toml` is an org-deployed
    layer merging *below* config.toml, so config.toml's silence is not the
    same as grok's built-in default.  Getting any of them wrong writes over
    a pinned or automatic theme this hook promises to preserve.
    """

    PAIR_NIGHT = '[ui]\ntheme = "groknight"\n'

    def _env(self, env, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        return dict(env, GROK_HOME=str(root))

    def _default_grok(self, env, content=None) -> Path:
        cfg = Path(env['HOME']) / '.grok' / 'config.toml'
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            cfg.write_text(content)
        return cfg

    # --- $GROK_HOME ------------------------------------------------------

    def test_grok_home_selects_the_active_config(self, fake_mac, tmp_path):
        """The active install is the one that changes; the inactive default
        config must come back byte-identical."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'config.toml').write_text(self.PAIR_NIGHT)
        inactive = self._default_grok(env, self.PAIR_NIGHT)
        run(env2, 'day')
        assert tomllib.loads(
            (active / 'config.toml').read_text())['ui']['theme'] == 'grokday'
        assert inactive.read_text() == self.PAIR_NIGHT  # untouched

    def test_grok_home_without_a_default_grok_dir(self, fake_mac, tmp_path):
        """No ~/.grok at all must not mean the active install is skipped."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'config.toml').write_text(self.PAIR_NIGHT)
        assert not (Path(env['HOME']) / '.grok').exists()
        result = run(env2, 'day')
        assert result.returncode == 0
        assert tomllib.loads(
            (active / 'config.toml').read_text())['ui']['theme'] == 'grokday'

    def test_empty_grok_home_falls_back_to_the_default(self, fake_mac):
        """`GROK_HOME=` is unset, not a root named the empty string."""
        env, _ = fake_mac
        cfg = self._default_grok(env, self.PAIR_NIGHT)
        run(dict(env, GROK_HOME=''), 'day')
        assert tomllib.loads(cfg.read_text())['ui']['theme'] == 'grokday'

    # --- a theme key outside the documented schema -----------------------

    # `ui.theme` and its legacy alias `ui.ui_theme` are the documented
    # fields; a bare top-level `theme` is neither.  Rather than guess
    # whether grok honours it, stand down — and say so, because a sync the
    # user expected did not happen.
    @pytest.mark.parametrize('value', ['auto', 'tokyonight', 'groknight'])
    def test_top_level_theme_stands_down_and_warns(self, fake_mac, tmp_path,
                                                   value):
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        content = f'theme = "{value}"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert result.returncode == 0
        assert (active / 'config.toml').read_text() == content
        assert 'grok theme not synced' in result.stderr

    def test_top_level_theme_in_managed_layer_also_stands_down(
            self, fake_mac, tmp_path):
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'managed_config.toml').write_text('theme = "auto"\n')
        content = '[models]\ndefault = "grok-4.6"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert (active / 'config.toml').read_text() == content
        assert 'grok theme not synced' in result.stderr

    # --- the legacy `ui.ui_theme` alias ----------------------------------

    def test_legacy_alias_is_flipped_where_it_lives(self, fake_mac, tmp_path):
        """Writing the canonical key instead would leave a stale alias
        beside it saying the opposite."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'config.toml').write_text('[ui]\nui_theme = "groknight"\n')
        run(env2, 'day')
        cfg = tomllib.loads((active / 'config.toml').read_text())
        assert cfg['ui']['ui_theme'] == 'grokday'
        assert 'theme' not in cfg['ui']  # no competing canonical key added

    @pytest.mark.parametrize('value', ['auto', 'tokyonight'])
    def test_legacy_alias_self_adapting_or_pinned_is_preserved(
            self, fake_mac, tmp_path, value):
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        content = f'[ui]\nui_theme = "{value}"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert (active / 'config.toml').read_text() == content
        assert 'not synced' not in result.stderr

    def test_canonical_key_outranks_the_legacy_alias(self, fake_mac, tmp_path):
        """`ui.theme` present means the alias is not what grok renders."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'config.toml').write_text(
            '[ui]\ntheme = "auto"\nui_theme = "groknight"\n')
        result = run(env2, 'day')
        cfg = tomllib.loads((active / 'config.toml').read_text())
        assert cfg['ui']['theme'] == 'auto'  # the effective value, preserved
        assert cfg['ui']['ui_theme'] == 'groknight'  # and the stale one left
        assert 'not synced' not in result.stderr

    # --- the managed_config.toml layer -----------------------------------

    @pytest.mark.parametrize('value', ['auto', 'system', 'tokyonight'])
    def test_managed_self_adapting_or_pinned_is_not_overridden(
            self, fake_mac, tmp_path, value):
        """config.toml being silent is not grok's built-in default when a
        managed layer supplies the theme — writing the canonical key here
        would override an org's choice, since config.toml outranks it."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'managed_config.toml').write_text(f'[ui]\ntheme = "{value}"\n')
        content = '[models]\ndefault = "grok-4.6"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert (active / 'config.toml').read_text() == content
        assert 'not synced' not in result.stderr

    def test_managed_pair_theme_gets_a_canonical_override(self, fake_mac,
                                                          tmp_path):
        """A managed GrokNight is ours to flip, and config.toml is the
        documented place to outrank it — the managed file stays untouched."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        managed = '[ui]\ntheme = "groknight"\n'
        (active / 'managed_config.toml').write_text(managed)
        (active / 'config.toml').write_text('[models]\ndefault = "grok-4.6"\n')
        run(env2, 'day')
        cfg = tomllib.loads((active / 'config.toml').read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert cfg['models']['default'] == 'grok-4.6'
        assert (active / 'managed_config.toml').read_text() == managed

    def test_config_toml_outranks_the_managed_layer(self, fake_mac, tmp_path):
        """The user's own pin wins over a managed pair theme, so there is
        nothing to flip."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'managed_config.toml').write_text('[ui]\ntheme = "groknight"\n')
        content = '[ui]\ntheme = "auto"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert (active / 'config.toml').read_text() == content
        assert 'not synced' not in result.stderr

    def test_managed_legacy_alias_is_resolved_too(self, fake_mac, tmp_path):
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'managed_config.toml').write_text('[ui]\nui_theme = "auto"\n')
        content = '[models]\ndefault = "grok-4.6"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert (active / 'config.toml').read_text() == content
        assert 'not synced' not in result.stderr

    def test_unparseable_managed_layer_warns_rather_than_guessing(
            self, fake_mac, tmp_path):
        """A layer we cannot read is a theme we cannot resolve."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'managed_config.toml').write_text('not [ valid toml\n')
        content = '[models]\ndefault = "grok-4.6"\n'
        (active / 'config.toml').write_text(content)
        result = run(env2, 'day')
        assert result.returncode == 0
        assert (active / 'config.toml').read_text() == content
        assert 'grok theme not synced' in result.stderr

    def test_absent_managed_layer_is_the_ordinary_case(self, fake_mac,
                                                       tmp_path):
        """No managed file means the built-in default really does apply."""
        env, _ = fake_mac
        active = tmp_path / 'active'
        env2 = self._env(env, active)
        (active / 'config.toml').write_text('[models]\ndefault = "grok-4.6"\n')
        assert not (active / 'managed_config.toml').exists()
        result = run(env2, 'day')
        cfg = tomllib.loads((active / 'config.toml').read_text())
        assert cfg['ui']['theme'] == 'grokday'
        assert 'not synced' not in result.stderr

    def test_codex_is_unaffected_by_grok_layering(self, fake_mac, tmp_path):
        """codex passes no layering extras, so a managed_config.toml or a
        top-level theme beside its config must change nothing for it."""
        env, _ = fake_mac
        codex = Path(env['HOME']) / '.codex'
        codex.mkdir()
        (codex / 'managed_config.toml').write_text('[tui]\ntheme = "auto"\n')
        (codex / 'config.toml').write_text(
            'theme = "auto"\n[tui]\ntheme = "catppuccin-mocha"\n')
        result = run(env, 'day')
        cfg = tomllib.loads((codex / 'config.toml').read_text())
        assert cfg['tui']['theme'] == 'catppuccin-latte'  # still flips
        assert cfg['theme'] == 'auto'  # the stray top-level key is left alone
        assert 'not synced' not in result.stderr


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
