#!/usr/bin/env python3
"""Tests for hive.py tmux dev-session support (ADR-0063)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
import hive


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def fake_hive(tmp_path):
    """A hive root with three numbered workspaces plus a non-repo directory."""
    hive_root = tmp_path / 'infra'
    hive_root.mkdir()
    for name in ['widget-1', 'widget-2', 'widget-3']:
        ws = hive_root / name
        ws.mkdir()
        (ws / '.git').mkdir()
    (hive_root / 'notes').mkdir()  # must be ignored — no .git
    return hive_root


# --- Palette -----------------------------------------------------------------


class TestPalette:
    def test_palette_has_eight_entries(self):
        assert len(hive._SHELL_PALETTE) == 8

    def test_every_entry_has_all_fields(self):
        required = {'name', 'rgb', 'c256',
                    'primary', 'background', 'foreground', 'inactive_bg'}
        for color in hive._SHELL_PALETTE:
            assert required <= set(color), f'missing fields in {color}'

    def test_hex_fields_are_hex(self):
        for color in hive._SHELL_PALETTE:
            for key in ('primary', 'background', 'foreground', 'inactive_bg'):
                val = color[key]
                assert val.startswith('#') and len(val) == 7

    def test_names_unique(self):
        names = [c['name'] for c in hive._SHELL_PALETTE]
        assert len(names) == len(set(names))


class TestHiveColor:
    def test_positional_assignment(self, tmp_path):
        a = tmp_path / 'a'
        a.mkdir()
        b = tmp_path / 'b'
        b.mkdir()
        with patch.object(hive, '_load_apiary', return_value=[a, b]):
            assert hive._hive_color(a) == hive._SHELL_PALETTE[0]
            assert hive._hive_color(b) == hive._SHELL_PALETTE[1]

    def test_wraps_past_palette_size(self, tmp_path):
        hives = []
        for i in range(len(hive._SHELL_PALETTE) + 1):
            d = tmp_path / f'h{i}'
            d.mkdir()
            hives.append(d)
        with patch.object(hive, '_load_apiary', return_value=hives):
            # The 9th hive wraps back to palette[0].
            assert hive._hive_color(hives[-1]) == hive._SHELL_PALETTE[0]

    def test_unknown_hive_falls_back_to_first(self, tmp_path):
        known = tmp_path / 'known'
        known.mkdir()
        other = tmp_path / 'other'
        other.mkdir()
        with patch.object(hive, '_load_apiary', return_value=[known]):
            assert hive._hive_color(other) == hive._SHELL_PALETTE[0]


# --- Config generation -------------------------------------------------------


class TestGenerateTmuxConfig:
    def test_sources_base_config(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert 'source-file ~/.tmux/tmux.conf' in conf

    def test_exports_hive_env(self, fake_hive):
        color = hive._SHELL_PALETTE[2]
        conf = hive._generate_tmux_config(fake_hive, color)
        assert f'set-environment HIVE_NAME "{fake_hive.name}"' in conf
        assert f'set-environment HIVE_ROOT "{fake_hive.resolve()}"' in conf
        assert f'set-environment HIVE_COLOR "{color["name"]}"' in conf
        assert f'set-environment HIVE_COLOR_RGB "{color["rgb"]}"' in conf
        assert f'set-environment HIVE_COLOR_256 "{color["c256"]}"' in conf

    def test_status_bar_uses_palette_hex(self, fake_hive):
        color = hive._SHELL_PALETTE[3]
        conf = hive._generate_tmux_config(fake_hive, color)
        assert color['background'] in conf
        assert color['primary'] in conf
        assert color['inactive_bg'] in conf

    def test_session_scoped_no_global_flag(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        # Hive-specific settings must be session-scoped (never `set -g`).
        assert 'set -g ' not in conf
        assert 'set status-style' in conf

    def test_keybindings_guard_on_hive_root(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        for key in ('bind c', 'bind b', 'bind g', 'bind G', 'bind C-g',
                    'bind R', 'bind r'):
            assert key in conf
        assert '$HIVE_ROOT' in conf

    def test_invokes_hive_tmux_subcommands(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert 'hive tmux label-window' in conf
        assert 'hive tmux git-sync' in conf
        assert 'hive tmux popup' in conf
        assert 'hive tmux runs' in conf
        assert 'hive tmux --hive' in conf
        assert 'hive-ci-popup' in conf


class TestWriteTmuxConfig:
    def test_writes_to_tmux_dir(self, fake_hive, tmp_path):
        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'):
            path = hive._write_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert path.name == f'{fake_hive.name}.conf'
        assert path.is_file()
        assert 'source-file' in path.read_text()


# --- Session helpers ---------------------------------------------------------


class TestSessionHelpers:
    def test_next_session_num_empty(self):
        assert hive._next_session_num('infra', []) == 0

    def test_next_session_num_skips_used(self):
        sessions = ['infra-0', 'infra-2', 'flow-app-1']
        assert hive._next_session_num('infra', sessions) == 3

    def test_next_session_num_ignores_non_numeric(self):
        assert hive._next_session_num('infra', ['infra-foo']) == 0

    def test_group_exists(self):
        assert hive._group_exists('infra', ['infra-0', 'flow-1'])
        assert not hive._group_exists('infra', ['flow-1'])

    def test_workspace_number(self):
        assert hive._workspace_number('widget-3') == '3'
        assert hive._workspace_number('flow-app-12') == '12'
        assert hive._workspace_number('noname') is None

    def test_discover_workspaces(self, fake_hive):
        names = [w.name for w in hive._discover_workspaces(fake_hive)]
        assert names == ['widget-1', 'widget-2', 'widget-3']


class TestResolveTmuxHive:
    def test_resolves_by_short_name(self, fake_hive):
        with patch.object(hive, '_load_apiary', return_value=[fake_hive]):
            assert hive._resolve_tmux_hive(fake_hive.name) == fake_hive

    def test_resolves_by_path(self, fake_hive):
        with patch.object(hive, '_load_apiary', return_value=[fake_hive]):
            assert hive._resolve_tmux_hive(str(fake_hive)) == fake_hive

    def test_path_outside_apiary_allowed(self, fake_hive):
        with patch.object(hive, '_load_apiary', return_value=[]):
            assert hive._resolve_tmux_hive(str(fake_hive)) == fake_hive

    def test_unknown_name_returns_none(self):
        with patch.object(hive, '_load_apiary', return_value=[]):
            assert hive._resolve_tmux_hive('nonesuch') is None

    def test_no_arg_detects_from_cwd(self, fake_hive):
        with patch.object(hive, '_find_hive_root', return_value=fake_hive):
            assert hive._resolve_tmux_hive(None) == fake_hive


# --- Window labeling ---------------------------------------------------------


class TestShortenBranch:
    def test_strips_prefix(self):
        assert hive._shorten_branch('feat/audio-manager') == 'audio-manager'

    def test_truncates_long(self):
        assert hive._shorten_branch('feat/' + 'x' * 40) == 'x' * 14 + '..'

    def test_plain_branch_unchanged(self):
        assert hive._shorten_branch('hotfix') == 'hotfix'


def _git_out_for_branch(branch):
    """Build a fake _git_out that reports a given current branch."""
    def fake(args, cwd=None):
        if args[:2] == ['rev-parse', '--abbrev-ref']:
            return branch
        return None
    return fake


class TestComputeWindowLabel:
    def test_default_branch_is_bare_name(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_git_out', _git_out_for_branch('main')), \
             patch.object(hive, '_default_branch', return_value='main'):
            data = hive._compute_window_label(ws)
        assert data['label'] == 'widget-1'
        assert data['pr'] is None

    def test_feature_branch_without_pr(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_git_out', _git_out_for_branch('feat/thing')), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_get_pr_info', return_value=None):
            data = hive._compute_window_label(ws)
        assert data['label'] == 'widget-1/thing'

    def test_feature_branch_with_open_pr(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_git_out', _git_out_for_branch('feat/thing')), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_get_pr_info',
                          return_value={'number': 42, 'state': 'open'}):
            data = hive._compute_window_label(ws)
        assert data['label'] == 'widget-1#42'
        assert data['pr'] == 42

    def test_closed_pr_not_used(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_git_out', _git_out_for_branch('feat/thing')), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_get_pr_info',
                          return_value={'number': 7, 'state': 'merged'}):
            data = hive._compute_window_label(ws)
        assert data['label'] == 'widget-1/thing'
        assert data['pr'] is None


class TestLabelCacheKey:
    def test_includes_leaf_name(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        key = hive._label_cache_key(ws)
        assert key.startswith('label-widget-1-')

    def test_distinct_for_same_leaf_different_path(self, tmp_path):
        a = tmp_path / 'hive-a' / 'widget-1'
        b = tmp_path / 'hive-b' / 'widget-1'
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        assert hive._label_cache_key(a) != hive._label_cache_key(b)


class TestLabelWindow:
    def test_non_git_dir_uses_basename(self, tmp_path):
        pane = tmp_path / 'somedir'
        pane.mkdir()
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout='')

        with patch.object(hive, '_git_out', return_value=None), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run):
            hive._tmux_label_window(str(pane), '@1')
        assert ['tmux', 'rename-window', '-t', '@1', 'somedir'] in calls

    def test_missing_pane_path_is_noop(self):
        with patch.object(hive.subprocess, 'run') as run:
            hive._tmux_label_window('', '@1')
            hive._tmux_label_window('/does/not/exist/xyz', '@1')
        run.assert_not_called()

    def test_fresh_cache_skips_recompute(self, tmp_path):
        ws = tmp_path / 'widget-1'
        (ws / '.git').mkdir(parents=True)
        tmux_dir = tmp_path / 'hive-tmux'

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(ws)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/thing'
            return None

        compute = MagicMock(return_value={
            'branch': 'feat/thing', 'default': 'main',
            'pr': 5, 'label': 'widget-1#5',
        })
        with patch.object(hive, '_TMUX_DIR', tmux_dir), \
             patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_compute_window_label', compute), \
             patch.object(hive.subprocess, 'run',
                          return_value=MagicMock(returncode=0)):
            hive._tmux_label_window(str(ws), '@1')
            hive._tmux_label_window(str(ws), '@1')
        # Second call must hit the cache — compute runs exactly once.
        assert compute.call_count == 1

    def test_branch_change_invalidates_cache(self, tmp_path):
        ws = tmp_path / 'widget-1'
        (ws / '.git').mkdir(parents=True)
        tmux_dir = tmp_path / 'hive-tmux'
        branch = {'name': 'feat/one'}

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(ws)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return branch['name']
            return None

        compute = MagicMock(side_effect=lambda w: {
            'branch': branch['name'], 'default': 'main',
            'pr': None, 'label': f'widget-1/{branch["name"]}',
        })
        with patch.object(hive, '_TMUX_DIR', tmux_dir), \
             patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_compute_window_label', compute), \
             patch.object(hive.subprocess, 'run',
                          return_value=MagicMock(returncode=0)):
            hive._tmux_label_window(str(ws), '@1')
            branch['name'] = 'feat/two'  # branch changed under the cache
            hive._tmux_label_window(str(ws), '@1')
        assert compute.call_count == 2

    def test_writes_pr_cache_for_numbered_workspace(self, tmp_path):
        ws = tmp_path / 'infra' / 'widget-3'
        (ws / '.git').mkdir(parents=True)
        tmux_dir = tmp_path / 'hive-tmux'

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(ws)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/thing'
            return None

        compute = MagicMock(return_value={
            'branch': 'feat/thing', 'default': 'main',
            'pr': 99, 'label': 'widget-3#99',
        })
        with patch.object(hive, '_TMUX_DIR', tmux_dir), \
             patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_compute_window_label', compute), \
             patch.object(hive.subprocess, 'run',
                          return_value=MagicMock(returncode=0)):
            hive._tmux_label_window(str(ws), '@1')
        pr_cache = tmux_dir / 'infra-3.pr'
        assert pr_cache.is_file()
        assert pr_cache.read_text() == '99'


# --- git-sync indicator ------------------------------------------------------


class TestGitSync:
    def _run(self, tmp_path, behind, ahead, capsys):
        repo = tmp_path / 'repo'
        repo.mkdir()

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(repo)
            if args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
                return 'origin/main'
            if args[:2] == ['rev-list', '--count']:
                return f'{behind}\t{ahead}'
            return None

        # Marker is fresh so no background fetch is launched.
        marker = tmp_path / 'hive-tmux'
        marker.mkdir()
        (marker / f".fetch{str(repo.resolve()).replace('/', '_')}").write_text(
            str(time.time()))
        with patch.object(hive, '_TMUX_DIR', marker), \
             patch.object(hive, '_git_out', side_effect=fake_git_out):
            hive._tmux_git_sync(str(repo))
        return capsys.readouterr().out

    def test_in_sync_prints_nothing(self, tmp_path, capsys):
        assert self._run(tmp_path, 0, 0, capsys) == ''

    def test_behind_only(self, tmp_path, capsys):
        assert '↓3' in self._run(tmp_path, 3, 0, capsys)

    def test_ahead_only(self, tmp_path, capsys):
        assert '↑2' in self._run(tmp_path, 0, 2, capsys)

    def test_ahead_and_behind(self, tmp_path, capsys):
        out = self._run(tmp_path, 3, 2, capsys)
        assert '↑2' in out and '↓3' in out

    def test_no_upstream_prints_nothing(self, tmp_path, capsys):
        repo = tmp_path / 'repo'
        repo.mkdir()

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(repo)
            return None  # no upstream

        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'), \
             patch.object(hive, '_git_out', side_effect=fake_git_out):
            hive._tmux_git_sync(str(repo))
        assert capsys.readouterr().out == ''


# --- cmd_tmux dispatch -------------------------------------------------------


class TestCmdTmuxDispatch:
    def _args(self, **kw):
        ns = MagicMock()
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_label_window_action_routes(self):
        args = self._args(tmux_action='label-window',
                          pane_path='/x', window_id='@1')
        with patch.object(hive, '_tmux_label_window') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('/x', '@1')

    def test_git_sync_action_routes(self):
        args = self._args(tmux_action='git-sync', pane_path='/x')
        with patch.object(hive, '_tmux_git_sync') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('/x')

    def test_popup_action_routes(self):
        args = self._args(tmux_action='popup', cwd='/x',
                          command=['hive', 'status'])
        with patch.object(hive, '_tmux_popup') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('/x', ['hive', 'status'])

    def test_exits_when_tmux_missing(self):
        args = self._args(tmux_action=None, hive=None, list_hives=False)
        with patch.object(hive, '_tmux_available', return_value=False):
            with pytest.raises(SystemExit):
                hive.cmd_tmux(args)

    def test_list_action(self, capsys):
        args = self._args(tmux_action=None, list_hives=True)
        with patch.object(hive, '_tmux_available', return_value=True), \
             patch.object(hive, '_load_apiary', return_value=None):
            hive.cmd_tmux(args)
        assert 'No hives configured' in capsys.readouterr().out

    def test_unresolvable_hive_exits(self):
        args = self._args(tmux_action=None, list_hives=False, hive='nope')
        with patch.object(hive, '_tmux_available', return_value=True), \
             patch.object(hive, '_resolve_tmux_hive', return_value=None):
            with pytest.raises(SystemExit):
                hive.cmd_tmux(args)


# --- Pane environment seeding ------------------------------------------------


class TestTmuxEnvArgs:
    def test_seeds_all_hive_wide_vars(self, tmp_path):
        hive_root = tmp_path / 'infra'
        hive_root.mkdir()
        color = hive._SHELL_PALETTE[1]
        args = hive._tmux_env_args(hive_root, 'infra', color, '3')
        joined = ' '.join(args)
        assert f'HIVE_ROOT={hive_root.resolve()}' in joined
        assert 'HIVE_NAME=infra' in joined
        assert f'HIVE_COLOR={color["name"]}' in joined
        assert f'HIVE_COLOR_RGB={color["rgb"]}' in joined
        assert f'HIVE_COLOR_256={color["c256"]}' in joined
        # Every value is preceded by a -e flag.
        assert args.count('-e') == len([a for a in args if '=' in a])

    def test_includes_hive_number_when_present(self, tmp_path):
        hive_root = tmp_path / 'infra'
        hive_root.mkdir()
        args = hive._tmux_env_args(hive_root, 'infra', hive._SHELL_PALETTE[0], '7')
        assert 'HIVE_NUMBER=7' in args

    def test_omits_hive_number_when_none(self, tmp_path):
        hive_root = tmp_path / 'infra'
        hive_root.mkdir()
        args = hive._tmux_env_args(hive_root, 'infra', hive._SHELL_PALETTE[0], None)
        assert not any(a.startswith('HIVE_NUMBER=') for a in args)


class TestUsedWorkspaces:
    def test_exact_pane_path_counts_workspace(self, fake_hive):
        workspaces = hive._discover_workspaces(fake_hive)
        with patch.object(hive, '_windows_in_session',
                          return_value=[str(fake_hive / 'widget-1')]):
            used = hive._used_workspaces('infra-0', workspaces)
        assert fake_hive / 'widget-1' in used
        assert fake_hive / 'widget-2' not in used

    def test_pane_in_subdir_still_counts_workspace(self, fake_hive):
        # A pane that has cd'd below the workspace root must still mark the
        # workspace as used (ADR-0063 review finding P2).
        workspaces = hive._discover_workspaces(fake_hive)
        with patch.object(hive, '_windows_in_session',
                          return_value=[str(fake_hive / 'widget-1' / 'scripts')]):
            used = hive._used_workspaces('infra-0', workspaces)
        assert fake_hive / 'widget-1' in used

    def test_pane_outside_any_workspace_ignored(self, fake_hive, tmp_path):
        workspaces = hive._discover_workspaces(fake_hive)
        with patch.object(hive, '_windows_in_session',
                          return_value=[str(tmp_path)]):
            used = hive._used_workspaces('infra-0', workspaces)
        assert used == set()


# --- _tmux_start (non-exec paths) --------------------------------------------


class TestTmuxStartNewWindow:
    def _run_new_window(self, fake_hive, panes):
        runs = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return MagicMock(returncode=0, stdout='')

        with patch.object(hive, '_current_session', return_value='infra-0'), \
             patch.object(hive, '_tmux_sessions', return_value=['infra-0']), \
             patch.object(hive, '_windows_in_session', return_value=panes), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run):
            hive._tmux_start(fake_hive, hive._SHELL_PALETTE[0], new_window=True)
        return runs

    def test_new_window_picks_unused_workspace(self, fake_hive):
        runs = self._run_new_window(fake_hive, [str(fake_hive / 'widget-1')])
        assert len(runs) == 1
        cmd = runs[0]
        # Opens a window on the first unused workspace (widget-2)...
        assert cmd[:6] == ['tmux', 'new-window', '-t', 'infra-0',
                           '-c', str(fake_hive / 'widget-2')]
        # ...with the HIVE_* env seeded at creation time.
        assert '-e' in cmd
        assert 'HIVE_NAME=infra' in cmd
        assert 'HIVE_NUMBER=2' in cmd

    def test_new_window_skips_workspace_with_pane_in_subdir(self, fake_hive):
        # widget-1 has a pane sitting in a subdirectory — it must still be
        # treated as used, so the new window lands on widget-2, not a
        # duplicate widget-1 (ADR-0063 review finding P2).
        runs = self._run_new_window(
            fake_hive, [str(fake_hive / 'widget-1' / 'scripts')])
        assert len(runs) == 1
        assert runs[0][:6] == ['tmux', 'new-window', '-t', 'infra-0',
                               '-c', str(fake_hive / 'widget-2')]

    def test_new_window_falls_back_to_hive_root(self, fake_hive):
        # Every workspace already windowed — open in the hive root, no
        # HIVE_NUMBER, no error.
        panes = [str(fake_hive / f'widget-{n}') for n in (1, 2, 3)]
        runs = self._run_new_window(fake_hive, panes)
        assert len(runs) == 1
        assert runs[0][:6] == ['tmux', 'new-window', '-t', 'infra-0',
                               '-c', str(fake_hive)]
        assert not any(a.startswith('HIVE_NUMBER=') for a in runs[0])

    def test_already_in_session_without_new_window_is_noop(self, fake_hive, capsys):
        with patch.object(hive, '_current_session', return_value='infra-0'), \
             patch.object(hive, '_tmux_sessions', return_value=['infra-0']), \
             patch.object(hive.subprocess, 'run') as run:
            hive._tmux_start(fake_hive, hive._SHELL_PALETTE[0], new_window=False)
        run.assert_not_called()
        assert 'Already in infra session' in capsys.readouterr().out


# --- tmux-backed probe -------------------------------------------------------


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')
class TestPaneEnvironmentProbe:
    """End-to-end probe: the `-e` args we generate actually reach the pane's
    process environment (ADR-0063 review finding P1)."""

    def test_env_args_reach_pane_process_env(self, tmp_path):
        ws = tmp_path / 'infra' / 'widget-7'
        ws.mkdir(parents=True)
        env_args = hive._tmux_env_args(
            ws.parent, 'infra', hive._SHELL_PALETTE[0], '7')
        socket = f'hive-tmux-probe-{os.getpid()}'
        try:
            subprocess.run(
                ['tmux', '-L', socket, 'new-session', '-d', '-s', 'probe',
                 '-c', str(ws), *env_args],
                check=True, capture_output=True, text=True,
            )
            pid = subprocess.run(
                ['tmux', '-L', socket, 'display-message', '-p', '-t', 'probe',
                 '#{pane_pid}'],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            pane_env = subprocess.run(
                ['ps', 'eww', '-p', pid],
                capture_output=True, text=True,
            ).stdout
            assert 'HIVE_NUMBER=7' in pane_env
            assert 'HIVE_NAME=infra' in pane_env
            assert f'HIVE_ROOT={ws.parent.resolve()}' in pane_env
        finally:
            subprocess.run(['tmux', '-L', socket, 'kill-server'],
                           capture_output=True)


# --- run-dsl status integration ----------------------------------------------


def _write_sidecar(acc_runs_dir, name, *, work_dir, status=None,
                   heartbeat_age=None, program='channel-plan-implement-review',
                   objective='Fix thing', created_at='2026-05-15T00:00:00Z'):
    """Create a fake run-dsl sidecar dir under ``acc_runs_dir``.

    - ``status``: None (no status.json — running/interrupted), True (success),
      or False (failure).
    - ``heartbeat_age``: seconds since now for the heartbeat timestamp. None
      means no runtime.json.
    """
    sidecar = acc_runs_dir / name
    sidecar.mkdir(parents=True)
    (sidecar / 'manifest.json').write_text(json.dumps({
        'tool': 'run-dsl',
        'objective': objective,
        'work_dir': str(work_dir),
        'program': program,
        'created_at': created_at,
    }))
    if status is not None:
        (sidecar / 'status.json').write_text(json.dumps({
            'completed': True,
            'success': status,
            'timestamp': '2026-05-15T01:00:00Z',
        }))
    if heartbeat_age is not None:
        hb = datetime.fromtimestamp(time.time() - heartbeat_age).isoformat()
        (sidecar / 'runtime.json').write_text(json.dumps({
            'pid': 12345,
            'started_at': hb,
            'last_heartbeat_at': hb,
        }))
    return sidecar


class TestClassifyRunState:
    def test_status_success_true(self, tmp_path):
        sc = _write_sidecar(tmp_path, 's', work_dir=tmp_path, status=True)
        assert hive._classify_run_state(sc) == 'succeeded'

    def test_status_success_false(self, tmp_path):
        sc = _write_sidecar(tmp_path, 's', work_dir=tmp_path, status=False)
        assert hive._classify_run_state(sc) == 'failed'

    def test_no_status_fresh_heartbeat_running(self, tmp_path):
        sc = _write_sidecar(tmp_path, 's', work_dir=tmp_path,
                            status=None, heartbeat_age=10)
        assert hive._classify_run_state(sc) == 'running'

    def test_no_status_stale_heartbeat_interrupted(self, tmp_path):
        sc = _write_sidecar(tmp_path, 's', work_dir=tmp_path,
                            status=None, heartbeat_age=hive._RUN_HEARTBEAT_TTL + 10)
        assert hive._classify_run_state(sc) == 'interrupted'

    def test_no_status_no_runtime_interrupted(self, tmp_path):
        sc = _write_sidecar(tmp_path, 's', work_dir=tmp_path,
                            status=None, heartbeat_age=None)
        assert hive._classify_run_state(sc) == 'interrupted'


class TestWorkspaceRunState:
    def test_no_acc_runs_dir_returns_none(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_ACC_RUNS_DIR', tmp_path / 'nonexistent'):
            assert hive._workspace_run_state(ws) is None

    def test_workspace_with_no_runs_returns_none(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        # A sidecar for a *different* work_dir.
        _write_sidecar(acc, 'a', work_dir=tmp_path / 'other')
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._workspace_run_state(ws) is None

    def test_picks_most_recent_run(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        older = _write_sidecar(acc, 'old', work_dir=ws, status=False,
                               objective='Old run')
        newer = _write_sidecar(acc, 'new', work_dir=ws, status=True,
                               objective='New run')
        # Force the newer one's manifest mtime to be later.
        os.utime(newer / 'manifest.json', (time.time(), time.time()))
        os.utime(older / 'manifest.json', (time.time() - 100, time.time() - 100))
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            state = hive._workspace_run_state(ws)
        assert state is not None
        assert state['state'] == 'succeeded'
        assert state['objective'] == 'New run'


class TestRunStateLabelSuffix:
    def test_running_returns_dot(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == '●'

    def test_failed_returns_cross(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, status=False)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == '✗'

    def test_interrupted_returns_ellipsis(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws,
                       heartbeat_age=hive._RUN_HEARTBEAT_TTL + 100)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == '…'

    def test_succeeded_returns_empty(self, tmp_path):
        # Recent-success is informational only — keep the label clean.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, status=True)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == ''

    def test_no_runs_returns_empty(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_ACC_RUNS_DIR', tmp_path / 'nonexistent'):
            assert hive._run_state_label_suffix(ws) == ''


class TestTmuxRunsPopup:
    def test_empty_hive_shows_no_runs_message(self, fake_hive, tmp_path, capsys):
        with patch.object(hive, '_ACC_RUNS_DIR', tmp_path / 'nonexistent'):
            hive._tmux_runs(fake_hive)
        out = capsys.readouterr().out
        assert 'Runs in' in out
        assert 'no run-dsl runs' in out

    def test_lists_workspaces_with_runs(self, fake_hive, tmp_path, capsys):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        _write_sidecar(acc, 'a', work_dir=fake_hive / 'widget-1',
                       status=True, program='channel-review',
                       objective='Review the thing')
        _write_sidecar(acc, 'b', work_dir=fake_hive / 'widget-2',
                       heartbeat_age=5, program='channel-brainstorm',
                       objective='Brainstorm thing')
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            hive._tmux_runs(fake_hive)
        out = capsys.readouterr().out
        assert 'widget-1' in out and 'succeeded' in out
        assert 'widget-2' in out and 'running' in out
        assert 'channel-review' in out
        assert 'Brainstorm thing' in out

    def test_omits_workspaces_without_runs(self, fake_hive, tmp_path, capsys):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        _write_sidecar(acc, 'a', work_dir=fake_hive / 'widget-2', status=True)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            hive._tmux_runs(fake_hive)
        out = capsys.readouterr().out
        assert 'widget-2' in out
        # widget-1 and widget-3 have no runs — must not appear in the body.
        # (The "Runs in <hive>" header itself doesn't mention workspace names.)
        body = out.split('\n', 2)[-1] if '\n' in out else out
        assert 'widget-1' not in body
        assert 'widget-3' not in body


class TestLabelWindowRunSuffix:
    def test_appends_running_suffix(self, tmp_path):
        ws = tmp_path / 'widget-1'
        (ws / '.git').mkdir(parents=True)
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, heartbeat_age=10)

        renames = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ['tmux', 'rename-window']:
                renames.append(cmd[-1])
            return MagicMock(returncode=0, stdout='')

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(ws)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'main'
            return None

        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'), \
             patch.object(hive, '_ACC_RUNS_DIR', acc), \
             patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run):
            hive._tmux_label_window(str(ws), '@1')

        assert renames == ['widget-1 ●']

    def test_no_suffix_when_no_run(self, tmp_path):
        ws = tmp_path / 'widget-1'
        (ws / '.git').mkdir(parents=True)

        renames = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ['tmux', 'rename-window']:
                renames.append(cmd[-1])
            return MagicMock(returncode=0, stdout='')

        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(ws)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'main'
            return None

        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'), \
             patch.object(hive, '_ACC_RUNS_DIR', tmp_path / 'no-such-dir'), \
             patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run):
            hive._tmux_label_window(str(ws), '@1')

        assert renames == ['widget-1']
