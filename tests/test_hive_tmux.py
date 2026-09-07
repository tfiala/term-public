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
from unittest.mock import MagicMock, call, patch

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
        for key in ('bind 0', 'bind c', 'bind b', 'bind p', 'bind g', 'bind G',
                    'bind C-g', 'bind R', 'bind r'):
            assert key in conf
        assert '$HIVE_ROOT' in conf

    def test_zero_selects_window_ten_only_in_hive_sessions(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        bindings = [line for line in conf.splitlines()
                    if line.startswith('bind 0 ')]
        assert len(bindings) == 1
        binding = bindings[0]
        assert "'[ -n \"$HIVE_ROOT\" ]'" in binding
        assert "'select-window -t :=10'" in binding
        assert "'select-window -t :=0'" in binding

    def test_invokes_hive_tmux_subcommands(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert 'hive tmux label-window' in conf
        assert 'hive tmux refresh-labels' in conf
        assert 'hive tmux status-context' in conf
        assert 'hive tmux turn-refresh' in conf
        assert 'hive tmux pairs' in conf
        assert 'set-hook after-new-window' in conf
        assert 'hive tmux popup' in conf
        assert 'hive tmux runs' in conf
        assert 'hive tmux --hive' in conf
        assert 'hive-ci-popup' in conf

    def test_popup_bindings_target_originating_client(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        popup_lines = [line for line in conf.splitlines()
                       if 'hive tmux popup' in line]
        assert len(popup_lines) == 5
        assert all('--client "#{client_name}"' in line
                   and '--pane "#{pane_id}"' in line for line in popup_lines)

    def test_status_right_is_bounded(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert 'set status-right-length 60' in conf
        assert '@hive_turn_pair_line' in conf
        assert '#(hive tmux turn-refresh' not in conf
        assert 'rev-parse --abbrev-ref HEAD' not in conf

    def test_refresh_bindings_use_shell_safe_session_name(self, fake_hive):
        conf = hive._generate_tmux_config(fake_hive, hive._SHELL_PALETTE[0])
        assert '#{session_id}' not in conf
        assert conf.count('refresh-labels "#{session_name}"') == 2
        assert 'bind r run-shell -b' in conf
        assert 'turn-refresh "#{session_name}" >/dev/null' in conf
        assert 'turn-refresh "#{session_name}" --bust-cache' in conf

    def test_window_formats_compose_run_and_turn_suffixes(self):
        assert '@hive_run_suffix' in hive._WINDOW_STATUS_FORMAT
        assert '@hive_turn_suffix' in hive._WINDOW_STATUS_FORMAT
        assert '@hive_run_suffix' in hive._WINDOW_STATUS_CURRENT_FORMAT
        assert '@hive_turn_suffix' in hive._WINDOW_STATUS_CURRENT_FORMAT


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

    def test_discover_workspaces_sorts_numeric_suffixes_naturally(self, fake_hive):
        for name in ('widget-9', 'widget-10', 'docs', 'alpha-2'):
            workspace = fake_hive / name
            workspace.mkdir()
            (workspace / '.git').mkdir()
        names = [w.name for w in hive._discover_workspaces(fake_hive)]
        assert names == [
            'alpha-2', 'docs', 'widget-1', 'widget-2', 'widget-3',
            'widget-9', 'widget-10',
        ]


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


class TestCompactBranch:
    branch = 'fix/automatic-publication-receipt-ledger'

    def test_preserves_kind_with_task_initials(self):
        assert hive._compact_branch(self.branch, 10) == 'fix/aprl'

    def test_initializes_kind_at_six_columns(self):
        assert hive._compact_branch(self.branch, 6) == 'f/aprl'

    def test_drops_kind_at_four_columns(self):
        assert hive._compact_branch(self.branch, 4) == 'aprl'

    def test_short_branch_passes_through(self):
        assert hive._compact_branch('main', 10) == 'main'

    def test_single_word_branch_is_bounded(self):
        assert hive._compact_branch('authorization', 4) == 'auth'


class TestBranchFieldWidth:
    def test_wide_client_uses_ten_columns(self):
        assert hive._branch_field_width('120', 'workingal-1', '9') == 10

    def test_narrow_client_steps_down(self):
        assert hive._branch_field_width('116', 'workingal-1', '9') == 6
        assert hive._branch_field_width('114', 'workingal-1', '9') == 4
        assert hive._branch_field_width('113', 'workingal-1', '9') == 0

    def test_reserves_turn_pair_run_glyphs_two_digit_tabs_and_sync(self):
        assert hive._branch_field_width('121', 'workingal-1', '10') == 4
        assert hive._branch_field_width('120', 'workingal-1', '10') == 0

    def test_invalid_dimensions_fail_to_small_field(self):
        assert hive._branch_field_width('unknown', 'workingal-1', '9') == 4


class TestTmuxStatusContext:
    def test_prints_fixed_width_compact_branch(self, capsys):
        def fake_git_out(args, cwd=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return '/repo'
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'fix/automatic-publication-receipt-ledger'
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git_out), \
             patch.object(hive, '_tmux_git_sync') as git_sync:
            hive._tmux_status_context('/repo', '120', 'workingal-1', '9')
        assert capsys.readouterr().out == 'fix/aprl   | '
        git_sync.assert_called_once_with('/repo')

    def test_prints_nothing_when_tabs_consume_width(self, capsys):
        with patch.object(hive, '_git_out') as git_out:
            hive._tmux_status_context('/repo', '77', 'workingal-1', '9')
        assert capsys.readouterr().out == ''
        git_out.assert_not_called()


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
        assert ['tmux', 'set-window-option', '-t', '@1',
                'window-status-format', hive._WINDOW_STATUS_FORMAT] in calls
        assert ['tmux', 'set-window-option', '-t', '@1',
                'window-status-current-format',
                hive._WINDOW_STATUS_CURRENT_FORMAT] in calls

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


class TestRefreshLabels:
    def test_refreshes_every_window_without_parsing_paths_on_spaces(self):
        listed = MagicMock(
            returncode=0,
            stdout='@1\t/tmp/hive/widget-1\n'
                   '@2\t/tmp/hive with spaces/widget-2\n')
        with patch.object(hive.subprocess, 'run', return_value=listed), \
             patch.object(hive, '_tmux_label_window') as label:
            refreshed = hive._tmux_refresh_labels('$1')

        assert refreshed is True
        label.assert_has_calls([
            call('/tmp/hive/widget-1', '@1'),
            call('/tmp/hive with spaces/widget-2', '@2'),
        ])

    def test_failed_list_is_reported(self):
        listed = MagicMock(returncode=1, stdout='', stderr='no session')
        with patch.object(hive.subprocess, 'run', return_value=listed), \
             patch.object(hive, '_tmux_label_window') as label:
            refreshed = hive._tmux_refresh_labels('missing')
        assert refreshed is False
        label.assert_not_called()


# --- turn indicator (ADR-0003) ----------------------------------------------


class TestTurnCommentClassifier:
    @pytest.mark.parametrize(('line', 'expected'), [
        ('Review disposition: LGTM at exact head `86e43ea0` against base '
         '`d2f42c47`.', 'approve'),
        ('Approved / LGTM on exact rebased head `83ebe092`.', 'approve'),
        ('Approved at `ca464e32`.', 'approve'),
        ('## Re-review: `174c671` — approved', 'approve'),
        ('Independent full-PR review at `c265a4f8` — **approved**, '
         'confirming the standing approval at this head.', 'approve'),
        ('Reviewed locally. No blocking findings.', 'approve'),
        ('Reviewed the PR diff in `tmux/tmux.conf`. No blocking findings.',
         'approve'),
        ('No blocking issues.', 'approve'),
        ('## Review — approve, one non-blocking nit', 'approve'),
        ('Approved. One non-blocking nit below.', 'approve'),
        ('LGTM with one nit', 'approve'),
        ('Approved on exact head `abcdef1`, one non-blocking nit.', 'approve'),
        ('Changes still requested on exact head `abcdef1`',
         'changes requested'),
        ('## Re-review — no new delta', 'no new delta'),
        ('Both blockers addressed in `ca464e3`; CI in flight.', 'completion'),
        ('Addressed the review notes in `6bafaf6`.', 'completion'),
        ('Fixed in dbc4f93.', 'completion'),
        ('Implemented the convergence fix and pushed `31e8a2b`.',
         'completion'),
        ('Rebased onto current main; new head `83ebe09`, CI in flight.',
         'completion'),
        ('Addressed all notes in the latest push:', 'completion'),
    ])
    def test_recognized_corpus_forms(self, line, expected):
        assert hive._classify_turn_comment(line) == expected

    @pytest.mark.parametrize('line', [
        'Cannot approve this yet',
        'Approval withheld pending the failing test',
        'Not ready for approval',
        'Approved with changes requested on abcdef1',
        'Pushed back on abcdef1',
        'LGTM-adjacent but blocked',
        'Rebased; the new head abcdef1 is not ready',
        'No blocking findings? Several, actually.',
        'Approved if CI passes',
        'LGTM except for the failing migration',
        'Approve after addressing the blocker',
        'Review: approved once the required test lands',
        'Approved provided that the migration is fixed',
        'LGTM assuming CI turns green',
        'Reviewed conditionally; no blocking findings.',
        'Approved pending CI',
        'Review at abcdef1 — approved, but changes requested on docs',
        'Approved, but do not merge',
        'LGTM — hold the merge',
        'approved (sarcasm)',
        'Approved / LGTM on exact rebased head abcdef1, but see the nit below',
        'Approved, one blocking nit',
        'Approved, one nit but do not merge',
    ])
    def test_mutation_negatives_never_direct_a_turn(self, line):
        assert hive._classify_turn_comment(line) == 'unrecognized'

    def test_one_exact_marker_overrides_first_line(self):
        body = 'Changes requested on exact head abcdef1\n\nHandoff: approve\n'
        assert hive._classify_turn_comment(body) == 'approve'

    @pytest.mark.parametrize('body', [
        'Progress note\n\n> Handoff: approve',
        'Progress note\n\n- Handoff: approve',
        'Progress note\n```\nHandoff: approve\n```',
        'Progress note\nHandoff: approve\nHandoff: addressed',
    ])
    def test_quoted_fenced_or_conflicting_markers_do_not_override(self, body):
        assert hive._classify_turn_comment(body) == 'unrecognized'

    def test_remote_summary_text_cannot_emit_terminal_controls(self):
        assert hive._turn_bounded_text(
            'Title\x1b]52;c;payload\x07\nnext') == 'Title ]52;c;payload next'


class TestTurnProcessIdentity:
    def _record(self, pid, ppid, comm, args):
        return {'pid': pid, 'ppid': ppid, 'tty': 'ttys001',
                'comm': comm, 'args': args}

    def test_symlinked_codex_entry_resolves_to_package(self, tmp_path):
        package = tmp_path / 'lib/node_modules/@openai/codex/bin'
        package.mkdir(parents=True)
        entry = package / 'codex.js'
        entry.write_text('')
        bindir = tmp_path / 'bin'
        bindir.mkdir()
        link = bindir / 'codex'
        link.symlink_to(entry)
        records = {
            10: self._record(10, 1, 'bash', 'bash'),
            11: self._record(11, 10, 'node', f'node {link}'),
        }
        assert hive._turn_process_agent(10, records) == 'codex'

    def test_ordinary_node_is_not_codex(self, tmp_path):
        script = tmp_path / 'server.js'
        script.write_text('')
        records = {
            10: self._record(10, 1, 'bash', 'bash'),
            11: self._record(11, 10, 'node', f'node {script}'),
        }
        assert hive._turn_process_agent(10, records) is None

    def test_native_codex_descendant_under_package_qualifies(self, tmp_path):
        binary = (tmp_path / 'node_modules/@openai/codex/vendor/bin/codex')
        binary.parent.mkdir(parents=True)
        binary.write_text('')
        records = {
            10: self._record(10, 1, 'bash', 'bash'),
            11: self._record(11, 10, str(binary), str(binary)),
        }
        assert hive._turn_process_agent(10, records) == 'codex'

    def test_child_tool_does_not_hide_claude_ancestor(self):
        records = {
            10: self._record(10, 1, 'bash', 'bash'),
            11: self._record(11, 10, 'claude', 'claude'),
            12: self._record(12, 11, 'python', 'python worker.py'),
        }
        assert hive._turn_process_agent(10, records) == 'claude'

    def test_two_agent_signatures_fail_closed(self, tmp_path):
        package = tmp_path / 'node_modules/@openai/codex/bin'
        package.mkdir(parents=True)
        entry = package / 'codex.js'
        entry.write_text('')
        records = {
            10: self._record(10, 1, 'bash', 'bash'),
            11: self._record(11, 10, 'claude', 'claude'),
            12: self._record(12, 10, 'node', f'node {entry}'),
        }
        assert hive._turn_process_agent(10, records) is None

    def test_malformed_snapshot_fails_closed(self):
        result = MagicMock(returncode=0, stdout='not-a-process-row\n')
        with patch.object(hive.subprocess, 'run', return_value=result):
            assert hive._turn_process_snapshot({'/dev/ttys001'}) is None


class TestTurnRoles:
    def test_role_command_writes_role_and_exact_pair_binding(self, tmp_path,
                                                               capsys):
        displayed = subprocess.CompletedProcess(
            [], 0, stdout=f'hive-0\t@1\t{tmp_path}\n', stderr='')
        written = subprocess.CompletedProcess([], 0, stdout='', stderr='')

        def fake_git(args, cwd=None, timeout=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(tmp_path)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/thing'
            return None

        with patch.object(hive.subprocess, 'run',
                          side_effect=[displayed, written]) as run, \
             patch.object(hive, '_git_out', side_effect=fake_git), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_tmux_turn_refresh', return_value=True):
            assert hive._tmux_role('reviewer')
        binding = hive._turn_pair_binding(
            ('git@github.com:acme/widget', 'feat/thing'))
        assert run.call_args_list[1].args[0][-1] == f'reviewer {binding}'
        assert 'Turn role declared: reviewer' in capsys.readouterr().out

    def test_role_clear_does_not_require_git_checkout(self, capsys):
        displayed = subprocess.CompletedProcess(
            [], 0, stdout='hive-0\t@1\t/not-a-repo\n', stderr='')
        written = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(hive.subprocess, 'run',
                          side_effect=[displayed, written]) as run, \
             patch.object(hive, '_git_out') as git_out, \
             patch.object(hive, '_tmux_turn_refresh', return_value=True):
            assert hive._tmux_role('clear')
        git_out.assert_not_called()
        assert run.call_args_list[1].args[0][-1] == ''
        assert 'declaration cleared' in capsys.readouterr().out

    def test_declaration_is_bound_to_exact_pair_key(self):
        key = ('git@github.com:acme/widget', 'feat/thing')
        value = f'reviewer {hive._turn_pair_binding(key)}'
        assert hive._turn_declared_role(value, key) == 'reviewer'
        assert hive._turn_declared_role(
            value, (key[0], 'feat/other')) is None

    @pytest.mark.parametrize(('oldest', 'expected'), [
        ('branch: Created from HEAD', 'implementer'),
        ('branch: Created from main', 'implementer'),
        ('branch: Created from origin/feat/thing', 'reviewer'),
    ])
    def test_derives_measured_reflog_shapes(self, tmp_path, oldest, expected):
        with patch.object(hive, '_git_out', return_value=f'newest\n{oldest}'):
            assert hive._derive_turn_role(
                tmp_path, 'git@github.com:acme/widget', 'feat/thing', 'main') \
                == expected

    def test_forgejo_fetched_shape_derives_reviewer(self, tmp_path):
        with patch.object(
                hive, '_git_out',
                return_value='branch: Created from origin/feat/thing'):
            assert hive._derive_turn_role(
                tmp_path, 'ssh://forgejo/acme/widget', 'feat/thing', 'main') \
                == 'reviewer'

    @pytest.mark.parametrize(('remote', 'cli'), [
        ('git@github.com:acme/widget', 'gh'),
        ('https://github.com/acme/widget', 'gh'),
        ('ssh://git@forgejo.home/acme/widget', 'fj'),
        ('git@notgithub.com:acme/widget', 'fj'),
    ])
    def test_pr_cli_selection_uses_the_exact_remote_host(self, remote, cli):
        assert hive._turn_cli_for_remote(remote) == cli

    def test_missing_reflog_does_not_latch(self, tmp_path):
        with patch.object(hive, '_git_out', return_value=None):
            assert hive._derive_turn_role(
                tmp_path, 'git@github.com:acme/widget', 'feat/thing', 'main') \
                is None

    def test_fresh_window_loses_derived_role_when_evidence_disappears(
            self, tmp_path):
        def window():
            return hive._TurnWindow(
                window_id='@1', index=1, pane_id='%1', pane_pid=10,
                pane_tty='ttys001', pane_path=str(tmp_path),
                pane_command='claude', activity=0)

        def git_out_with_reflog(args, cwd=None, timeout=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(tmp_path)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/thing'
            if args == ['rev-parse', 'HEAD']:
                return 'a' * 40
            if args[:2] == ['reflog', 'show']:
                return 'branch: Created from HEAD'
            return None

        records = {10: {'pid': 10, 'ppid': 1, 'tty': 'ttys001',
                        'comm': 'claude', 'args': 'claude'}}
        first = window()
        with patch.object(hive, '_git_out', side_effect=git_out_with_reflog), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'):
            hive._populate_turn_window(first, records)
        assert (first.role, first.role_source) == ('implementer', 'reflog')

        second = window()
        with patch.object(hive, '_git_out',
                          side_effect=lambda args, cwd=None, timeout=None:
                          None if args[:2] == ['reflog', 'show']
                          else git_out_with_reflog(args, cwd, timeout)), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'):
            hive._populate_turn_window(second, records)
        assert (second.role, second.role_source) == ('unknown', 'none')

    def test_branch_change_clears_stale_declaration(self, tmp_path):
        old_key = ('git@github.com:acme/widget', 'feat/old')
        window = hive._TurnWindow(
            window_id='@1', index=1, pane_id='%1', pane_pid=10,
            pane_tty='ttys001', pane_path=str(tmp_path),
            pane_command='claude', activity=0,
            declared=f'reviewer {hive._turn_pair_binding(old_key)}')
        records = {10: {'pid': 10, 'ppid': 1, 'tty': 'ttys001',
                        'comm': 'claude', 'args': 'claude'}}

        def fake_git(args, cwd=None, timeout=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(tmp_path)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/new'
            if args == ['rev-parse', 'HEAD']:
                return 'a' * 40
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_derive_turn_role', return_value=None):
            hive._populate_turn_window(window, records)
        assert window.clear_declaration is True
        assert window.role == 'unknown'

    @pytest.mark.parametrize(('branch', 'agent', 'expected_reason'), [
        ('HEAD', 'claude', 'incomplete git identity'),
        ('main', 'claude', 'default branch'),
        ('feat/thing', 'bash', 'no supported agent'),
    ])
    def test_detached_default_and_shell_windows_are_ineligible(
            self, tmp_path, branch, agent, expected_reason):
        window = hive._TurnWindow(
            window_id='@1', index=1, pane_id='%1', pane_pid=10,
            pane_tty='ttys001', pane_path=str(tmp_path),
            pane_command=agent, activity=0)
        records = {10: {'pid': 10, 'ppid': 1, 'tty': 'ttys001',
                        'comm': agent, 'args': agent}}

        def fake_git(args, cwd=None, timeout=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(tmp_path)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return branch
            if args == ['rev-parse', 'HEAD']:
                return 'a' * 40
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'):
            hive._populate_turn_window(window, records)
        assert window.eligible is False
        assert window.reason == expected_reason

    def test_agent_start_and_exit_change_eligibility_on_successive_ticks(
            self, tmp_path):
        def window():
            return hive._TurnWindow(
                window_id='@1', index=1, pane_id='%1', pane_pid=10,
                pane_tty='ttys001', pane_path=str(tmp_path),
                pane_command='bash', activity=0)

        def fake_git(args, cwd=None, timeout=None):
            if args[:2] == ['rev-parse', '--show-toplevel']:
                return str(tmp_path)
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'feat/thing'
            if args == ['rev-parse', 'HEAD']:
                return 'a' * 40
            return None

        agent_records = {
            10: {'pid': 10, 'ppid': 1, 'tty': 'ttys001',
                 'comm': 'bash', 'args': 'bash'},
            11: {'pid': 11, 'ppid': 10, 'tty': 'ttys001',
                 'comm': 'claude', 'args': 'claude'},
        }
        shell_records = {10: agent_records[10]}
        started, exited = window(), window()
        with patch.object(hive, '_git_out', side_effect=fake_git), \
             patch.object(hive, '_get_origin_url',
                          return_value='git@github.com:acme/widget'), \
             patch.object(hive, '_default_branch', return_value='main'), \
             patch.object(hive, '_derive_turn_role', return_value=None):
            hive._populate_turn_window(started, agent_records)
            hive._populate_turn_window(exited, shell_records)
        assert started.eligible is True
        assert exited.eligible is False
        assert exited.reason == 'no supported agent'


class TestTurnPrivateCache:
    @pytest.mark.parametrize('mask', [0o000, 0o022])
    def test_modes_are_private_under_any_umask(self, tmp_path, mask):
        state = tmp_path / 'state/hive/turn'
        old_umask = os.umask(mask)
        try:
            with patch.object(hive, '_TURN_STATE_DIR', state):
                path = state / 'entry.json'
                assert hive._write_private_json(path, {'ok': True})
        finally:
            os.umask(old_umask)
        assert state.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600

    def test_closed_observation_expires_but_merged_receipt_does_not(self,
                                                                    tmp_path):
        key = ('git@github.com:acme/widget', 'feat/thing')
        heads = ('a' * 40,)
        with patch.object(hive, '_TURN_STATE_DIR', tmp_path / 'turn'):
            hive._save_turn_pr(
                key, heads, {'state': 'closed', 'head_sha': heads[0]}, 10)
            assert hive._cached_turn_pr(key, heads, 69.999)['state'] == 'closed'
            assert hive._cached_turn_pr(key, heads, 70) is None
            hive._save_turn_pr(
                key, heads, {'state': 'merged', 'head_sha': heads[0]}, 80)
            assert hive._cached_turn_pr(key, heads, 100000)['state'] == 'merged'

    def test_version_one_receipt_is_invalidated_before_forgejo_recheck(
            self, tmp_path):
        key = ('ssh://git@forgejo.home/acme/widget', 'feat/thing')
        heads = ('a' * 40,)
        with patch.object(hive, '_TURN_STATE_DIR', tmp_path / 'turn'):
            path = hive._turn_cache_path(key)
            hive._write_private_json(path, {
                'version': 1, 'pair': list(key),
                'merged_receipt': {
                    'state': 'merged', 'head_sha': heads[0], 'number': 9}})
            assert hive._cached_turn_pr(key, heads, 20) is None
            hive._save_turn_pr(
                key, heads, {'state': 'closed', 'head_sha': heads[0]}, 20)
            assert hive._cached_turn_pr(key, heads, 21)['state'] == 'closed'
            refreshed = json.loads(path.read_text())
            assert refreshed['version'] == hive._TURN_CACHE_VERSION == 2
            assert 'merged_receipt' not in refreshed

    def test_reopened_pr_replaces_closed_observation_at_same_head(self,
                                                                  tmp_path):
        key = ('git@github.com:acme/widget', 'feat/thing')
        heads = ('a' * 40,)
        with patch.object(hive, '_TURN_STATE_DIR', tmp_path / 'turn'):
            hive._save_turn_pr(
                key, heads, {'state': 'closed', 'head_sha': heads[0]}, 10)
            hive._save_turn_pr(
                key, heads, {'state': 'open', 'head_sha': heads[0]}, 71)
            assert hive._cached_turn_pr(key, heads, 72)['state'] == 'open'

    def test_head_advance_invalidates_receipt(self, tmp_path):
        key = ('git@github.com:acme/widget', 'feat/thing')
        with patch.object(hive, '_TURN_STATE_DIR', tmp_path / 'turn'):
            hive._save_turn_pr(
                key, ('a' * 40,),
                {'state': 'merged', 'head_sha': 'a' * 40}, 10)
            assert hive._cached_turn_pr(key, ('b' * 40,), 20) is None

    def test_manual_bust_preserves_only_merged_receipt(self, tmp_path):
        key = ('git@github.com:acme/widget', 'feat/thing')
        heads = ('a' * 40,)
        state = tmp_path / 'turn'
        with patch.object(hive, '_TURN_STATE_DIR', state):
            hive._save_turn_pr(
                key, heads, {'state': 'merged', 'head_sha': heads[0]}, 10)
            path = hive._turn_cache_path(key)
            data = json.loads(path.read_text())
            data['mutable'] = {'observed_at': 10, 'heads': list(heads),
                               'result': {'state': 'closed'}}
            hive._write_private_json(path, data)
            hive._bust_turn_mutable_cache()
            after = json.loads(path.read_text())
        assert 'mutable' not in after
        assert after['merged_receipt']['state'] == 'merged'

    def test_cache_never_persists_body_or_process_arguments(self, tmp_path):
        key = ('git@github.com:acme/widget', 'feat/thing')
        result = {
            'state': 'open', 'number': 7, 'title': 'A' * 100,
            'body': 'secret body', 'args': '--secret token',
            'comment': {'id': '1', 'classification': 'completion',
                        'first_line': 'B' * 100, 'body': 'raw body'},
        }
        with patch.object(hive, '_TURN_STATE_DIR', tmp_path / 'turn'):
            hive._save_turn_pr(key, ('a',), result, 10)
            text = hive._turn_cache_path(key).read_text()
        assert 'secret body' not in text
        assert '--secret' not in text
        assert 'raw body' not in text
        assert 'A' * 81 not in text
        assert 'B' * 81 not in text


class TestTurnPrLookup:
    @pytest.mark.parametrize(('pr', 'expected'), [
        ({'state': 'OPEN', 'merged': False,
          'mergedAt': '0001-01-01T00:00:00Z'}, 'open'),
        ({'state': 'CLOSED', 'merged': False,
          'mergedAt': '0001-01-01T00:00:00Z'}, 'closed'),
        ({'state': 'OPEN',
          'mergedAt': '0001-01-01T00:00:00+00:00'}, 'open'),
        ({'state': 'MERGED', 'merged': True,
          'mergedAt': '0001-01-01T00:00:00Z'}, 'merged'),
        ({'state': 'OPEN', 'mergedAt': None}, 'open'),
        ({'state': 'MERGED',
          'mergedAt': '2026-09-07T00:00:00Z'}, 'merged'),
        ({'state': 'OPEN', 'mergedAt': 'not-a-time'}, 'open'),
    ])
    def test_pr_state_normalizes_real_github_and_forgejo_shapes(
            self, pr, expected):
        assert hive._turn_pr_state(pr) == expected

    def test_cli_timeout_and_oversized_json_fail_closed(self, tmp_path):
        with patch.object(
                hive.subprocess, 'run',
                side_effect=subprocess.TimeoutExpired('gh', 1)):
            assert hive._run_turn_pr_cli(
                'gh', tmp_path, ['list'], time.monotonic() + 5) is None
        oversized = MagicMock(
            returncode=0, stdout=' ' * (hive._TURN_PR_JSON_MAX + 1))
        with patch.object(hive.subprocess, 'run', return_value=oversized):
            assert hive._run_turn_pr_cli(
                'gh', tmp_path, ['list'], time.monotonic() + 5) is None

    def test_open_pr_is_group_wide_across_mixed_heads(self, tmp_path):
        responses = [
            [{'number': 7, 'state': 'OPEN'}],
            {'number': 7, 'state': 'OPEN', 'title': 'Turn',
             'headRefOid': 'b' * 40,
             'comments': [{'id': 'c', 'createdAt': '2026-09-06T00:00:00Z',
                           'body': 'Addressed in `abcdef1`.'}]},
        ]
        with patch.object(hive, '_run_turn_pr_cli', side_effect=responses):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('a' * 40, 'b' * 40), time.monotonic() + 5)
        assert result['state'] == 'open'
        assert result['comment']['classification'] == 'completion'

    def test_forgejo_zero_merge_time_open_pr_remains_open(self, tmp_path):
        zero = '0001-01-01T00:00:00Z'
        responses = [
            [{'number': 1088, 'state': 'OPEN', 'merged': False,
              'mergedAt': zero}],
            {'number': 1088, 'state': 'OPEN', 'merged': False,
             'mergedAt': zero, 'headRefOid': 'b' * 40, 'comments': []},
        ]
        with patch.object(
                hive, '_run_turn_pr_cli', side_effect=responses) as run:
            result = hive._lookup_turn_pr(
                tmp_path, ('ssh://git@forgejo.home/infra/home-dc',
                           'adr-0111-digitalstorm-node'),
                ('a' * 40, 'b' * 40), time.monotonic() + 5)
        assert result['state'] == 'open'
        assert all(any('merged' in arg for arg in call.args[2])
                   for call in run.call_args_list)

    @pytest.mark.parametrize(('terminal', 'expected'), [
        ({'number': 7, 'state': 'MERGED', 'merged': True,
          'mergedAt': '2026-09-07T00:00:00Z',
          'headRefName': 'refs/pull/7/head', 'headRefOid': 'wanted'},
         'merged'),
        ({'number': 9, 'state': 'CLOSED', 'merged': False,
          'mergedAt': '0001-01-01T00:00:00Z',
          'headRefName': 'refs/pull/9/head', 'headRefOid': 'wanted'},
         'closed'),
    ])
    def test_forgejo_terminal_lookup_matches_sha_after_branch_deletion(
            self, tmp_path, terminal, expected):
        unrelated_open = {
            'number': 1088, 'state': 'OPEN', 'merged': False,
            'mergedAt': '0001-01-01T00:00:00Z', 'headRefOid': 'elsewhere'}
        with patch.object(
                hive, '_run_turn_pr_cli',
                side_effect=[[], [unrelated_open, terminal]]) as run:
            result = hive._lookup_turn_pr(
                tmp_path, ('ssh://git@forgejo.home/acme/widget',
                           'feat/deleted'),
                ('wanted',), time.monotonic() + 5)
        assert result['state'] == expected
        terminal_args = run.call_args_list[1].args[2]
        assert '--head' not in terminal_args
        assert any('merged' in arg for arg in terminal_args)

    def test_forgejo_multiple_prs_at_same_sha_is_unknown(self, tmp_path):
        matches = [
            {'number': number, 'state': 'MERGED', 'merged': True,
             'headRefOid': 'wanted'}
            for number in (7, 8)]
        with patch.object(
                hive, '_run_turn_pr_cli', side_effect=[[], matches]):
            result = hive._lookup_turn_pr(
                tmp_path, ('ssh://git@forgejo.home/acme/widget',
                           'feat/deleted'),
                ('wanted',), time.monotonic() + 5)
        assert result == {
            'state': 'unknown', 'reason': 'multiple PRs at local HEAD'}

    def test_multiple_open_prs_are_unknown(self, tmp_path):
        with patch.object(hive, '_run_turn_pr_cli', return_value=[{}, {}]):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('a',), time.monotonic() + 5)
        assert result == {'state': 'unknown', 'reason': 'multiple open PRs'}

    def test_no_open_pr_with_mixed_heads_is_unknown(self, tmp_path):
        run = MagicMock(return_value=[])
        with patch.object(hive, '_run_turn_pr_cli', run):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('a', 'b'), time.monotonic() + 5)
        assert result['reason'] == 'mixed local HEADs'
        assert run.call_count == 1

    def test_full_nonmatching_terminal_page_is_unknown(self, tmp_path):
        responses = [[], [
            {'number': i, 'state': 'CLOSED', 'headRefOid': str(i)}
            for i in range(hive._TURN_TERMINAL_LIMIT)]]
        with patch.object(hive, '_run_turn_pr_cli', side_effect=responses):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('wanted',), time.monotonic() + 5)
        assert result['reason'] == 'terminal history truncated'

    def test_short_nonmatching_terminal_page_proves_none(self, tmp_path):
        with patch.object(
                hive, '_run_turn_pr_cli', side_effect=[[], []]) as run:
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('wanted',), time.monotonic() + 5)
        assert result == {'state': 'none'}
        assert run.call_args_list[1].args[2][-2:] == [
            '--search', 'sort:updated-desc']

    @pytest.mark.parametrize(('state', 'merged_at', 'expected'), [
        ('CLOSED', None, 'closed'),
        ('MERGED', '2026-09-06T00:00:00Z', 'merged'),
    ])
    def test_terminal_state_must_match_shared_head(self, tmp_path, state,
                                                   merged_at, expected):
        terminal = {'number': 7, 'state': state, 'headRefOid': 'wanted',
                    'mergedAt': merged_at}
        with patch.object(hive, '_run_turn_pr_cli',
                          side_effect=[[], [terminal]]):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('wanted',), time.monotonic() + 5)
        assert result['state'] == expected
        assert result['head_sha'] == 'wanted'

    def test_lookup_failure_is_unknown_and_not_cacheable(self, tmp_path):
        with patch.object(hive, '_run_turn_pr_cli', return_value=None):
            result = hive._lookup_turn_pr(
                tmp_path, ('git@github.com:acme/widget', 'feat/thing'),
                ('a',), time.monotonic() + 5)
        assert result['state'] == 'unknown'
        assert result['_cacheable'] is False


def _turn_window(index, role='unknown', activity=0):
    return hive._TurnWindow(
        window_id=f'@{index}', index=index, pane_id=f'%{index}', pane_pid=index,
        pane_tty=f'ttys{index:03}', pane_path=f'/repo-{index}',
        pane_command='agent', activity=activity, role=role,
        role_source='reflog', eligible=True)


class TestTurnBatonRule:
    def _pair(self, now=100):
        return [_turn_window(1, 'implementer', now - 60),
                _turn_window(2, 'reviewer', now - 50)]

    @pytest.mark.parametrize(('classification', 'target', 'glyph', 'verb'), [
        ('approve', '@1', '✓', 'merge pr'),
        ('changes requested', '@1', '▶', 'address review feedback'),
        ('no new delta', '@1', '▶', 'address review feedback'),
        ('completion', '@2', '▶', 're-review'),
    ])
    def test_comment_handoffs_target_the_role(self, classification, target,
                                              glyph, verb):
        windows = self._pair()
        decision = hive._decide_turn(
            windows, {'state': 'open',
                      'comment': {'classification': classification}}, 100)
        assert decision['target'] == target
        assert decision['suffixes'][target] == glyph
        assert decision['verb'] == verb

    def test_named_question_flag_defaults_off_and_outranks_comments(self):
        windows = self._pair()
        assert all(window.question is False for window in windows)
        windows[0].question = True
        decision = hive._decide_turn(
            windows, {'state': 'open',
                      'comment': {'classification': 'completion'}}, 100)
        assert decision['target'] == '@1'
        assert decision['suffixes'] == {'@1': '?', '@2': ''}

    @pytest.mark.parametrize('state', ['closed', 'merged'])
    def test_terminal_state_outranks_shape_and_comments(self, state):
        windows = [_turn_window(1), _turn_window(2), _turn_window(3)]
        decision = hive._decide_turn(
            windows, {'state': state,
                      'comment': {'classification': 'completion'}}, 100)
        assert set(decision['suffixes'].values()) == {'↩'}
        assert decision['verb'] == 'put back'

    def test_unrecognized_latest_comment_is_unknown(self):
        decision = hive._decide_turn(
            self._pair(), {'state': 'open',
                           'comment': {'classification': 'unrecognized'}}, 100)
        assert set(decision['suffixes'].values()) == {''}
        assert decision['reason'] == 'unrecognized handoff'

    def test_active_pair_without_comments_has_no_glyph(self):
        windows = self._pair()
        windows[0].activity = 95
        decision = hive._decide_turn(windows, {'state': 'none'}, 100)
        assert set(decision['suffixes'].values()) == {''}
        assert decision['reason'] == 'active'

    def test_quiet_pair_targets_the_earlier_quiet_since(self):
        decision = hive._decide_turn(self._pair(), {'state': 'none'}, 100)
        assert decision['target'] == '@1'
        assert decision['suffixes']['@1'] == '▶'

    def test_equal_quiet_since_is_unknown(self):
        windows = self._pair()
        windows[1].activity = windows[0].activity
        decision = hive._decide_turn(windows, {'state': 'none'}, 100)
        assert decision['reason'] == 'equal quiet-since'

    def test_singleton_only_targets_its_own_role(self):
        reviewer = [_turn_window(2, 'reviewer')]
        completion = hive._decide_turn(
            reviewer, {'state': 'open',
                       'comment': {'classification': 'completion'}}, 100)
        approval = hive._decide_turn(
            reviewer, {'state': 'open',
                       'comment': {'classification': 'approve'}}, 100)
        assert completion['suffixes']['@2'] == '▶'
        assert approval['reason'] == 'next: implementer — no window'

    def test_duplicate_roles_and_three_windows_are_malformed(self):
        duplicate = [_turn_window(1, 'reviewer'), _turn_window(2, 'reviewer')]
        three = self._pair() + [_turn_window(3, 'implementer')]
        assert hive._turn_group_shape(duplicate) == 'malformed'
        assert hive._turn_group_shape(three) == 'malformed'
        for windows in (duplicate, three):
            decision = hive._decide_turn(
                windows, {'state': 'open',
                          'comment': {'classification': 'completion'}}, 100)
            assert decision['reason'] == 'malformed group'

    def test_unknown_pr_state_clears_all_glyphs(self):
        decision = hive._decide_turn(
            self._pair(), {'state': 'unknown', 'reason': 'lookup failed'}, 100)
        assert set(decision['suffixes'].values()) == {''}
        assert decision['reason'] == 'lookup failed'


class TestTurnProducerBoundary:
    def test_collects_existing_turn_outputs_for_delta_writes(self):
        row = '\t'.join((
            '@1', '1', '%1', '10', 'ttys001', '/workspace', 'node', '100',
            'reviewer binding', 'Codex', '▶', 'reviewer', 'declared',
            '2:re-review'))
        listed = subprocess.CompletedProcess(
            [], 0, stdout=f'{row}\n', stderr='')
        with patch.object(hive.subprocess, 'run', return_value=listed):
            windows = hive._collect_turn_windows('hive-0')
        assert windows is not None
        assert len(windows) == 1
        window = windows[0]
        assert (window.current_suffix, window.current_role,
                window.current_role_source, window.current_target) == (
                    '▶', 'reviewer', 'declared', '2:re-review')

    def test_unchanged_turn_outputs_do_not_mutate_tmux(self):
        window = _turn_window(1, 'reviewer')
        window.current_suffix = '▶'
        window.current_role = 'reviewer'
        window.current_role_source = window.role_source
        window.current_target = '1:re-review'
        with patch.object(hive.subprocess, 'run') as run:
            hive._set_turn_window_options(window, '▶', '1:re-review')
        run.assert_not_called()

    def test_writes_only_changed_outputs_and_stale_declaration(self):
        window = _turn_window(1, 'reviewer')
        window.current_suffix = '▶'
        window.current_role = 'reviewer'
        window.current_role_source = window.role_source
        window.current_target = '1:re-review'
        window.declared = 'reviewer stale-binding'
        window.clear_declaration = True
        written = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(
                hive.subprocess, 'run', return_value=written) as run:
            hive._set_turn_window_options(window, '', '2:put back')
        assert [entry.args[0][-2:] for entry in run.call_args_list] == [
            ['@hive_turn_suffix', ''],
            ['@hive_turn_target', '2:put back'],
            ['@hive_turn_role_declared', ''],
        ]
        assert window.declared == ''

    def test_groups_same_branch_by_normalized_remote_and_handles_no_pr_pair(
            self, tmp_path):
        first = _turn_window(1, 'implementer', activity=10)
        second = _turn_window(2, 'reviewer', activity=20)
        other = _turn_window(3, 'implementer', activity=30)
        for window in (first, second, other):
            window.branch = 'feat/thing'
            window.head = 'a' * 40
            window.workspace = tmp_path
        first.remote = second.remote = 'git@github.com:acme/widget'
        other.remote = 'git@github.com:elsewhere/widget'

        def no_prs(groups, started, now):
            return {key: {'state': 'none'} for key in groups}

        with patch.object(hive, '_collect_turn_windows',
                          return_value=[first, second, other]), \
             patch.object(hive, '_turn_process_snapshot', return_value={}), \
             patch.object(hive, '_populate_turn_window'), \
             patch.object(hive, '_resolve_turn_prs', side_effect=no_prs):
            snapshot = hive._build_turn_session('hive-0', now=100)
        assert snapshot is not None
        assert len(snapshot['groups']) == 2
        pair_key = (first.remote, first.branch)
        assert snapshot['groups'][pair_key] == [first, second]
        assert snapshot['decisions'][pair_key]['target'] == '@1'

    def test_applies_outputs_to_every_window_and_clears_ineligible(self):
        first = _turn_window(1, 'implementer')
        second = _turn_window(2, 'reviewer')
        inactive = _turn_window(3)
        inactive.eligible = False
        inactive.reason = 'default branch'
        key = ('remote', 'branch')
        first.remote = second.remote = key[0]
        first.branch = second.branch = key[1]
        snapshot = {
            'windows': [first, second, inactive],
            'groups': {key: [first, second]},
            'decisions': {key: {
                'suffixes': {'@1': '▶', '@2': ''}, 'target': '@1',
                'verb': 'address review feedback', 'kind': 'changes requested',
                'reason': ''}},
        }
        with patch.object(hive, '_set_turn_window_options') as write:
            hive._apply_turn_session(snapshot)
        assert write.call_count == 3
        write.assert_any_call(first, '▶', '1:address review feedback')
        write.assert_any_call(second, '', '1:address review feedback')
        write.assert_any_call(inactive, '', '')

    def test_deadline_marks_every_unfinished_key_unknown(self, tmp_path):
        key1 = ('one', 'branch')
        key2 = ('two', 'branch')
        groups = {}
        for key, index in ((key1, 1), (key2, 2)):
            window = _turn_window(index, 'implementer')
            window.remote, window.branch, window.head = key[0], key[1], 'a'
            window.workspace = tmp_path
            groups[key] = [window]

        gate = __import__('threading').Event()

        def blocked(*args):
            gate.wait(0.2)
            return {'state': 'none'}

        with patch.object(hive, '_cached_turn_pr', return_value=None), \
             patch.object(hive, '_lookup_turn_pr', side_effect=blocked), \
             patch.object(hive, '_TURN_PRODUCER_DEADLINE', 0):
            resolved = hive._resolve_turn_prs(
                groups, time.monotonic(), time.time())
        gate.set()
        assert {result['reason'] for result in resolved.values()} \
            == {'producer deadline'}

    def test_turn_refresh_rewrites_both_windows_without_selection_event(self):
        first = _turn_window(1, 'implementer')
        second = _turn_window(2, 'reviewer')
        key = ('remote', 'branch')
        first.remote = second.remote = key[0]
        first.branch = second.branch = key[1]
        snapshot = {
            'session': 'hive-0', 'windows': [first, second],
            'groups': {key: [first, second]},
            'prs': {key: {'state': 'open', 'number': 7}},
            'decisions': {key: {
                'suffixes': {'@1': '', '@2': '▶'}, 'target': '@2',
                'verb': 're-review', 'kind': 'completion', 'reason': ''}},
        }
        with patch.object(hive, '_build_turn_session', return_value=snapshot), \
             patch.object(hive, '_set_turn_window_options') as write, \
             patch.object(hive, '_set_turn_session_line') as set_line:
            assert hive._tmux_turn_refresh('hive-0')
        assert write.call_count == 2
        set_line.assert_called_once_with('hive-0', '1⇄2 #7 ▶2')

    def test_unchanged_pair_line_does_not_mutate_tmux(self):
        shown = subprocess.CompletedProcess(
            [], 0, stdout='1⇄2 #7 ▶2\n', stderr='')
        with patch.object(
                hive.subprocess, 'run', return_value=shown) as run:
            hive._set_turn_session_line('hive-0', '1⇄2 #7 ▶2')
        run.assert_called_once_with(
            ['tmux', 'show-option', '-t', 'hive-0', '-v',
             '@hive_turn_pair_line'], capture_output=True, text=True)

    def test_changed_pair_line_updates_session_option(self):
        shown = subprocess.CompletedProcess(
            [], 0, stdout='old\n', stderr='')
        written = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        with patch.object(
                hive.subprocess, 'run', side_effect=(shown, written)) as run:
            hive._set_turn_session_line('hive-0', '1⇄2 #7 ▶2')
        assert run.call_args_list[-1].args[0] == [
            'tmux', 'set-option', '-t', 'hive-0',
            '@hive_turn_pair_line', '1⇄2 #7 ▶2']

    def test_terminal_tick_clears_bound_declarations(self):
        first = _turn_window(1, 'implementer')
        second = _turn_window(2, 'reviewer')
        key = ('remote', 'branch')
        for window in (first, second):
            window.remote, window.branch, window.head = key[0], key[1], 'a'
            window.declared = f'{window.role} {hive._turn_pair_binding(key)}'
        with patch.object(hive, '_collect_turn_windows',
                          return_value=[first, second]), \
             patch.object(hive, '_turn_process_snapshot', return_value={}), \
             patch.object(hive, '_populate_turn_window'), \
             patch.object(hive, '_resolve_turn_prs',
                          return_value={key: {'state': 'merged'}}):
            snapshot = hive._build_turn_session('hive-0', now=100)
        assert snapshot is not None
        assert all(window.clear_declaration for window in snapshot['windows'])

    def test_pairs_popup_names_handoff_liveness_and_sanitizes_remote_text(
            self, capsys):
        first = _turn_window(1, 'implementer', activity=95)
        second = _turn_window(2, 'reviewer', activity=40)
        key = ('remote', 'branch')
        first.remote = second.remote = key[0]
        first.branch = second.branch = key[1]
        snapshot = {
            'session': 'hive-0', 'observed_at': 100,
            'windows': [first, second], 'groups': {key: [first, second]},
            'prs': {key: {
                'state': 'open', 'number': 7, 'title': 'Safe\x1b[31m title',
                'comment': {
                    'classification': 'completion',
                    'first_line': 'Pushed\x07 abcdef1',
                    'created_at': '1970-01-01T00:01:30Z'}}},
            'decisions': {key: {
                'suffixes': {'@1': '', '@2': '▶'}, 'target': '@2',
                'verb': 're-review', 'kind': 'completion', 'reason': ''}},
        }
        with patch.object(hive, '_build_turn_session', return_value=snapshot), \
             patch.object(hive, '_apply_turn_session'), \
             patch.object(hive, '_set_turn_session_line') as set_line:
            assert hive._tmux_pairs('hive-0')
        set_line.assert_called_once_with('hive-0', '1⇄2 #7 ▶2')
        output = capsys.readouterr().out
        assert '\x1b' not in output and '\x07' not in output
        assert 'handoff 1:implementer completion (10s ago)' in output
        assert '1:active (output 5s ago)' in output
        assert '2:quiet since 1m ago' in output


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

    def test_refresh_labels_action_routes(self):
        args = self._args(tmux_action='refresh-labels', session='$1')
        with patch.object(hive, '_tmux_refresh_labels', return_value=True) as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('$1')

    def test_refresh_labels_action_propagates_failure(self):
        args = self._args(tmux_action='refresh-labels', session='missing')
        with patch.object(
                hive, '_tmux_refresh_labels', return_value=False), \
             pytest.raises(SystemExit) as exc:
            hive.cmd_tmux(args)
        assert exc.value.code == 1

    def test_turn_refresh_action_routes_with_cache_bust(self):
        args = self._args(tmux_action='turn-refresh', session='infra-0',
                          bust_cache=True)
        with patch.object(hive, '_tmux_turn_refresh', return_value=True) as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('infra-0', bust_cache=True)

    def test_pairs_action_routes(self):
        args = self._args(tmux_action='pairs', session='infra-0')
        with patch.object(hive, '_tmux_pairs', return_value=True) as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('infra-0')

    def test_pairs_without_session_uses_current_tmux_session(self):
        with patch.object(hive, '_current_session', return_value='infra-0'), \
             patch.object(hive, '_build_turn_session', return_value=None) as fn:
            assert hive._tmux_pairs() is False
        fn.assert_called_once_with('infra-0')

    def test_role_action_routes(self):
        args = self._args(tmux_action='role', role='reviewer')
        with patch.object(hive, '_tmux_role', return_value=True) as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('reviewer')

    def test_status_context_action_routes(self):
        args = self._args(tmux_action='status-context', pane_path='/x',
                          client_width='80', session_name='infra-0',
                          window_count='8')
        with patch.object(hive, '_tmux_status_context') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('/x', '80', 'infra-0', '8')

    def test_git_sync_action_routes(self):
        args = self._args(tmux_action='git-sync', pane_path='/x')
        with patch.object(hive, '_tmux_git_sync') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with('/x')

    def test_popup_action_routes(self):
        args = self._args(tmux_action='popup', cwd='/x',
                          client='/dev/ttys000', pane='%7',
                          command=['hive', 'status'])
        with patch.object(hive, '_tmux_popup') as fn:
            hive.cmd_tmux(args)
        fn.assert_called_once_with(
            '/x', ['hive', 'status'], client='/dev/ttys000', pane='%7')

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


class TestTmuxPopup:
    @pytest.mark.parametrize(('line_count', 'paged'), ((1, False), (100, True)))
    def test_targets_originating_client_for_dimensions_and_popup(
            self, tmp_path, line_count, paged):
        tmpfile = tmp_path / 'popup-output'
        fd = os.open(tmpfile, os.O_CREAT | os.O_RDWR)

        def fake_run(args, **kwargs):
            if args == ['hive', 'status']:
                kwargs['stdout'].write('status output\n' * line_count)
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ['tmux', 'display-message', '-p']:
                value = '120\n' if args[-1] == '#{window_width}' else '40\n'
                return subprocess.CompletedProcess(
                    args, 0, stdout=value, stderr='')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        with patch('tempfile.mkstemp', return_value=(fd, str(tmpfile))), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run) as run:
            hive._tmux_popup(
                '/workspace', ['hive', 'status'],
                client='/dev/ttys000', pane='%7')

        calls = [entry.args[0] for entry in run.call_args_list]
        dimension_calls = [args for args in calls
                           if args[:3] == ['tmux', 'display-message', '-p']]
        assert len(dimension_calls) == 2
        assert all(args[3:7] == ['-c', '/dev/ttys000', '-t', '%7']
                   for args in dimension_calls)
        popup_calls = [args for args in calls
                       if args[:2] == ['tmux', 'display-popup']]
        assert len(popup_calls) == 1
        assert popup_calls[0][2:6] == [
            '-c', '/dev/ttys000', '-t', '%7']
        assert ('-E' in popup_calls[0]) is paged

    def test_no_command_message_targets_originating_client(self):
        with patch.object(hive.subprocess, 'run') as run:
            hive._tmux_popup(
                None, [], client='/dev/ttys000', pane='%7')
        run.assert_called_once_with(
            ['tmux', 'display-message', '-c', '/dev/ttys000', '-t', '%7',
             'hive tmux popup: no command'], capture_output=True)


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
class TestStatusBarTmuxProbe:
    def test_generated_config_parses(self, fake_hive, tmp_path):
        socket = f'hive-config-{os.getpid()}-{time.time_ns()}'
        probe_home = tmp_path / 'home'
        probe_tmux_dir = probe_home / '.tmux'
        probe_tmux_dir.mkdir(parents=True)
        # The base config has its own version-aware tests. Keep this probe
        # focused on parsing the generated layer even when CI's tmux is older
        # than the machine targeted by tmux/tmux.conf.
        (probe_tmux_dir / 'tmux.conf').touch()
        probe_env = {**os.environ, 'HOME': str(probe_home)}
        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'):
            config = hive._write_tmux_config(
                fake_hive, hive._SHELL_PALETTE[0])
        try:
            subprocess.run(
                ['tmux', '-L', socket, 'new-session', '-d', '-s', 'probe'],
                check=True, capture_output=True, text=True, env=probe_env)
            sourced = subprocess.run(
                ['tmux', '-L', socket, 'source-file', '-t', 'probe',
                 str(config)],
                capture_output=True, text=True, env=probe_env)
            assert sourced.returncode == 0, sourced.stderr
        finally:
            subprocess.run(['tmux', '-L', socket, 'kill-server'],
                           capture_output=True)

    def test_new_window_hook_applies_compact_format(
            self, fake_hive, tmp_path):
        socket = f'hive-new-window-{os.getpid()}-{time.time_ns()}'
        probe_home = tmp_path / 'home'
        probe_tmux_dir = probe_home / '.tmux'
        probe_tmux_dir.mkdir(parents=True)
        (probe_tmux_dir / 'tmux.conf').touch()
        probe_bin = tmp_path / 'bin'
        probe_bin.mkdir()
        (probe_bin / 'hive').symlink_to(_SCRIPTS_DIR / 'hive.py')
        probe_env = {
            **os.environ,
            'HOME': str(probe_home),
            'PATH': f'{probe_bin}{os.pathsep}{os.environ["PATH"]}',
        }
        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'):
            config = hive._write_tmux_config(
                fake_hive, hive._SHELL_PALETTE[0])
        try:
            subprocess.run(
                ['tmux', '-L', socket, 'new-session', '-d', '-s', 'probe'],
                check=True, capture_output=True, text=True, env=probe_env)
            subprocess.run(
                ['tmux', '-L', socket, 'source-file', '-t', 'probe',
                 str(config)],
                check=True, capture_output=True, text=True, env=probe_env)
            window_id = subprocess.run(
                ['tmux', '-L', socket, 'new-window', '-P', '-F',
                 '#{window_id}', '-t', 'probe', '-c',
                 str(fake_hive / 'widget-1')],
                check=True, capture_output=True, text=True,
                env=probe_env).stdout.strip()

            deadline = time.time() + 2
            observed = None
            while time.time() < deadline:
                observed = subprocess.run(
                    ['tmux', '-L', socket, 'show-window-options',
                     '-t', window_id, '-v', 'window-status-format'],
                    capture_output=True, text=True, env=probe_env)
                if (observed.returncode == 0 and
                        observed.stdout.rstrip('\n') ==
                        hive._WINDOW_STATUS_FORMAT):
                    break
                time.sleep(0.02)
            assert observed is not None
            assert observed.returncode == 0, observed.stderr
            assert observed.stdout.rstrip('\n') == hive._WINDOW_STATUS_FORMAT
        finally:
            subprocess.run(['tmux', '-L', socket, 'kill-server'],
                           capture_output=True, env=probe_env)

    def test_compact_formats_are_set_on_every_window(self):
        socket = f'hive-status-{os.getpid()}-{time.time_ns()}'
        try:
            subprocess.run(
                ['tmux', '-L', socket, 'new-session', '-d', '-s', 'probe',
                 '-n', 'one'],
                check=True, capture_output=True, text=True)
            subprocess.run(
                ['tmux', '-L', socket, 'new-window', '-d', '-t', 'probe',
                 '-n', 'two'],
                check=True, capture_output=True, text=True)
            windows = subprocess.run(
                ['tmux', '-L', socket, 'list-windows', '-t', 'probe',
                 '-F', '#{window_id}'],
                check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            socket_path = subprocess.run(
                ['tmux', '-L', socket, 'display-message', '-p', '-t', 'probe',
                 '#{socket_path}'],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

            with patch.dict(os.environ, {'TMUX': f'{socket_path},0,0'}):
                hive._set_tmux_window_status(windows[0], '')
                hive._set_tmux_window_status(windows[1], '●')

            for window_id, suffix in zip(windows, ('', '●')):
                inactive = subprocess.run(
                    ['tmux', '-L', socket, 'show-window-options',
                     '-t', window_id, '-v', 'window-status-format'],
                    check=True, capture_output=True, text=True,
                ).stdout.rstrip('\n')
                current = subprocess.run(
                    ['tmux', '-L', socket, 'show-window-options',
                     '-t', window_id, '-v', 'window-status-current-format'],
                    check=True, capture_output=True, text=True,
                ).stdout.rstrip('\n')
                stored_suffix = subprocess.run(
                    ['tmux', '-L', socket, 'show-window-options',
                     '-t', window_id, '-v', '@hive_run_suffix'],
                    check=True, capture_output=True, text=True,
                ).stdout.rstrip('\n')
                assert inactive == hive._WINDOW_STATUS_FORMAT
                assert current == hive._WINDOW_STATUS_CURRENT_FORMAT
                assert stored_suffix == suffix
        finally:
            subprocess.run(['tmux', '-L', socket, 'kill-server'],
                           capture_output=True)


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


class TestSubtreeRunStates:
    def test_no_acc_runs_dir_returns_empty(self, tmp_path):
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        with patch.object(hive, '_ACC_RUNS_DIR', tmp_path / 'nonexistent'):
            assert hive._subtree_run_states(ws) == []

    def test_workspace_with_no_runs_returns_empty(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=tmp_path / 'other', status=True)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._subtree_run_states(ws) == []

    def test_picks_most_recent_run_per_work_dir(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        old = _write_sidecar(acc, 'old', work_dir=ws, status=False,
                             objective='Old run')
        os.utime(old / 'manifest.json', (1000, 1000))
        _write_sidecar(acc, 'new', work_dir=ws, status=True,
                       objective='New run')
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            runs = hive._subtree_run_states(ws)
        assert len(runs) == 1
        assert runs[0]['state'] == 'succeeded'
        assert runs[0]['objective'] == 'New run'

    def test_local_clone_gets_its_own_row(self, tmp_path):
        # The whole point of the subtree walk: work in .local/<repo> is the
        # workspace's work, but it is a different work_dir and must not be
        # collapsed into the root's row.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        (ws / '.local' / 'corpus').mkdir(parents=True)
        _write_sidecar(acc, 'root', work_dir=ws, status=True)
        _write_sidecar(acc, 'clone', work_dir=ws / '.local' / 'corpus',
                       status=False)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            runs = hive._subtree_run_states(ws)
        by_subpath = {r['subpath']: r['state'] for r in runs}
        assert by_subpath == {'': 'succeeded', '.local/corpus': 'failed'}

    def test_sibling_workspace_is_not_a_subtree_match(self, tmp_path):
        # 'widget-10' starts with 'widget-1' as a string but is not under it.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        (tmp_path / 'widget-10').mkdir()
        _write_sidecar(acc, 's', work_dir=tmp_path / 'widget-10', status=False)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._subtree_run_states(ws) == []


class TestWorkspaceLiveRuns:
    def test_counts_live_run_at_workspace_root(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert len(hive._workspace_live_runs(ws)) == 1

    def test_counts_live_run_inside_local_clone(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        (ws / '.local' / 'corpus').mkdir(parents=True)
        _write_sidecar(acc, 's', work_dir=ws / '.local' / 'corpus',
                       heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            runs = hive._workspace_live_runs(ws)
        assert [r['subpath'] for r in runs] == ['.local/corpus']

    def test_terminal_states_are_not_live(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 'ok', work_dir=ws, status=True)
        _write_sidecar(acc, 'bad', work_dir=ws, status=False)
        _write_sidecar(acc, 'stale', work_dir=ws,
                       heartbeat_age=hive._RUN_HEARTBEAT_TTL + 100)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._workspace_live_runs(ws) == []

    def test_stale_runtime_mtime_is_pruned_without_reading(self, tmp_path):
        # The prefilter is the reason this is cheap enough for every relabel.
        # A live run rewrites runtime.json each heartbeat, so an old mtime
        # means not-live even if the JSON inside claims a fresh heartbeat.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        sc = _write_sidecar(acc, 's', work_dir=ws, heartbeat_age=1)
        os.utime(sc / 'runtime.json', (1000, 1000))
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._workspace_live_runs(ws) == []

    def test_fresh_mtime_with_stale_heartbeat_still_rejected(self, tmp_path):
        # The prefilter only widens the candidate set; last_heartbeat_at
        # remains the authority, so an over-included sidecar is still dropped.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws,
                       heartbeat_age=hive._RUN_HEARTBEAT_TTL + 1)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._workspace_live_runs(ws) == []


class TestRunStateLabelSuffix:
    def test_single_live_run_returns_bare_dot(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == '●'

    def test_concurrent_live_runs_are_counted(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        (ws / '.local' / 'corpus').mkdir(parents=True)
        (ws / '.local' / 'homepage').mkdir(parents=True)
        _write_sidecar(acc, 'a', work_dir=ws, heartbeat_age=10)
        _write_sidecar(acc, 'b', work_dir=ws / '.local' / 'corpus',
                       heartbeat_age=10)
        _write_sidecar(acc, 'c', work_dir=ws / '.local' / 'homepage',
                       heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == '●3'

    def test_failed_no_longer_surfaces(self, tmp_path):
        # Terminal states are history — the popup names the clone and error.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws, status=False)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == ''

    def test_interrupted_no_longer_surfaces(self, tmp_path):
        # An interrupted run means "we stopped watching", not "act on this",
        # and it never expired — the old indicator became permanent decor.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        _write_sidecar(acc, 's', work_dir=ws,
                       heartbeat_age=hive._RUN_HEARTBEAT_TTL + 100)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            assert hive._run_state_label_suffix(ws) == ''

    def test_succeeded_returns_empty(self, tmp_path):
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

    def test_sibling_workspace_run_does_not_leak(self, tmp_path):
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        ws = tmp_path / 'widget-1'
        ws.mkdir()
        (tmp_path / 'widget-10').mkdir()
        _write_sidecar(acc, 's', work_dir=tmp_path / 'widget-10',
                       heartbeat_age=10)
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
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

    def test_local_clone_row_is_named_by_subpath(self, fake_hive, tmp_path,
                                                 capsys):
        # Terminal states moved here from the window label, so the row has to
        # identify which clone failed — 'widget-1' alone would not.
        acc = tmp_path / 'acc-runs'
        acc.mkdir()
        clone = fake_hive / 'widget-1' / '.local' / 'corpus'
        clone.mkdir(parents=True)
        _write_sidecar(acc, 'a', work_dir=fake_hive / 'widget-1', status=True)
        _write_sidecar(acc, 'b', work_dir=clone, status=False,
                       objective='Corpus run blew up')
        with patch.object(hive, '_ACC_RUNS_DIR', acc):
            hive._tmux_runs(fake_hive)
        out = capsys.readouterr().out
        assert 'widget-1/.local/corpus' in out
        assert 'failed' in out and 'Corpus run blew up' in out
        assert 'succeeded' in out


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


# --- Day / night mode ---------------------------------------------------------


class TestDayPalette:
    def test_every_entry_has_day_variant(self):
        required = {'rgb', 'c256',
                    'primary', 'background', 'foreground', 'inactive_bg'}
        for color in hive._SHELL_PALETTE:
            assert required <= set(color['day']), \
                f'missing day fields in {color["name"]}'

    def test_day_hex_fields_are_hex(self):
        for color in hive._SHELL_PALETTE:
            for key in ('primary', 'background', 'foreground', 'inactive_bg'):
                val = color['day'][key]
                assert val.startswith('#') and len(val) == 7

    def test_palette_for_mode_night_is_identity(self):
        color = hive._SHELL_PALETTE[0]
        assert hive._palette_for_mode(color, 'night') is color

    def test_palette_for_mode_day_overrides_style_fields(self):
        color = hive._SHELL_PALETTE[0]
        day = hive._palette_for_mode(color, 'day')
        assert day['name'] == color['name']
        assert day['background'] == color['day']['background']
        assert day['c256'] == color['day']['c256']
        assert day['rgb'] == color['day']['rgb']


class TestAppearanceMode:
    def _stub_defaults(self, tmp_path, monkeypatch, script):
        bindir = tmp_path / 'bin'
        bindir.mkdir(exist_ok=True)
        stub = bindir / 'defaults'
        stub.write_text(script)
        stub.chmod(0o755)
        monkeypatch.setenv('PATH', str(bindir))

    def _no_sources(self, tmp_path, monkeypatch):
        monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
        monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
        return tmp_path / 'cache' / 'term-theme'

    def test_dark_appearance_is_night(self, tmp_path, monkeypatch):
        self._stub_defaults(tmp_path, monkeypatch, '#!/bin/bash\necho Dark\n')
        assert hive._appearance_mode() == 'night'

    def test_light_appearance_is_day(self, tmp_path, monkeypatch):
        self._stub_defaults(tmp_path, monkeypatch, '#!/bin/bash\nexit 1\n')
        assert hive._appearance_mode() == 'day'

    def test_state_file_fallback(self, tmp_path, monkeypatch):
        state = self._no_sources(tmp_path, monkeypatch)
        state.mkdir(parents=True)
        (state / 'mode').write_text('day\n')
        assert hive._appearance_mode() == 'day'

    def test_defaults_to_night_without_sources(self, tmp_path, monkeypatch):
        self._no_sources(tmp_path, monkeypatch)
        assert hive._appearance_mode() == 'night'

    def test_invalid_state_file_is_night(self, tmp_path, monkeypatch):
        state = self._no_sources(tmp_path, monkeypatch)
        state.mkdir(parents=True)
        (state / 'mode').write_text('dusk\n')
        assert hive._appearance_mode() == 'night'


class TestDayStyling:
    def test_style_pairs_match_generated_config(self, fake_hive):
        color = hive._SHELL_PALETTE[1]
        conf = hive._generate_tmux_config(fake_hive, color)
        for _, opt, val in hive._style_option_pairs(color):
            assert f'set {opt} "{val}"' in conf

    def test_day_palette_drives_status_bar(self, fake_hive):
        night = hive._SHELL_PALETTE[0]
        day = hive._palette_for_mode(night, 'day')
        conf = hive._generate_tmux_config(fake_hive, day)
        assert day['background'] in conf
        assert day['c256'] in conf
        # The night hexes must be fully replaced.
        for key in ('primary', 'background', 'foreground', 'inactive_bg'):
            assert night[key] not in conf


class TestTmuxRestyle:
    def test_restyles_hive_sessions_for_day(self, tmp_path, monkeypatch):
        hive_root = tmp_path / 'infra'
        hive_root.mkdir()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            m = MagicMock()
            if cmd[:2] == ['tmux', 'list-sessions']:
                m.stdout, m.returncode = 'infra-0\nplain\n', 0
            elif cmd[:2] == ['tmux', 'show-environment']:
                session = cmd[cmd.index('-t') + 1]
                if session == 'infra-0':
                    m.stdout, m.returncode = f'HIVE_ROOT={hive_root}\n', 0
                else:
                    m.stdout, m.returncode = (
                        'unknown variable: HIVE_ROOT\n', 1)
            else:
                m.stdout, m.returncode = '', 0
            return m

        monkeypatch.setattr(hive, '_appearance_mode', lambda: 'day')
        with patch.object(hive, '_TMUX_DIR', tmp_path / 'hive-tmux'), \
             patch.object(hive, '_load_apiary', return_value=[hive_root]), \
             patch.object(hive.subprocess, 'run', side_effect=fake_run):
            hive._tmux_restyle()

        day = hive._palette_for_mode(hive._SHELL_PALETTE[0], 'day')
        style_sets = [c for c in calls if c[:2] == ['tmux', 'set']]
        assert ['tmux', 'set', '-t', 'infra-0', 'status-style',
                f'bg={day["background"]},fg={day["foreground"]}'] in style_sets
        # The non-hive session is never styled.
        assert all('plain' not in c for c in style_sets)
        env_sets = [c for c in calls if c[:2] == ['tmux', 'set-environment']]
        assert ['tmux', 'set-environment', '-t', 'infra-0',
                'HIVE_COLOR_256', day['c256']] in env_sets
        # Only the hive session's config is (re)generated.
        assert [p.name for p in (tmp_path / 'hive-tmux').iterdir()] == [
            'infra.conf']

    def test_restyle_rewrites_config_for_reload_binding(
            self, tmp_path, monkeypatch):
        # backtick+r sources /tmp/hive-tmux/<name>.conf. After a restyle
        # that file must carry the new mode's palette — otherwise the
        # documented reload reverts the status bar to the palette from
        # session creation.
        hive_root = tmp_path / 'infra'
        hive_root.mkdir()
        tmux_dir = tmp_path / 'hive-tmux'
        night = hive._SHELL_PALETTE[0]

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            if cmd[:2] == ['tmux', 'list-sessions']:
                # Two sessions of the same hive share one config file.
                m.stdout, m.returncode = 'infra-0\ninfra-1\n', 0
            elif cmd[:2] == ['tmux', 'show-environment']:
                m.stdout, m.returncode = f'HIVE_ROOT={hive_root}\n', 0
            else:
                m.stdout, m.returncode = '', 0
            return m

        monkeypatch.setattr(hive, '_appearance_mode', lambda: 'day')
        write = MagicMock(side_effect=hive._write_tmux_config)
        with patch.object(hive, '_TMUX_DIR', tmux_dir), \
             patch.object(hive, '_load_apiary', return_value=[hive_root]):
            # The config a night-mode _tmux_start would have left behind.
            hive._write_tmux_config(hive_root, night)
            with patch.object(hive, '_write_tmux_config', write), \
                 patch.object(hive.subprocess, 'run', side_effect=fake_run):
                hive._tmux_restyle()

        conf = (tmux_dir / 'infra.conf').read_text()
        day = hive._palette_for_mode(night, 'day')
        assert day['background'] in conf
        # The night palette is fully replaced in the reload source.
        for key in ('primary', 'background', 'foreground', 'inactive_bg'):
            assert night[key] not in conf
        assert write.call_count == 1  # deduped across the session group

    def test_no_tmux_server_is_a_noop(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.stdout, m.returncode = '', 1
            return m

        monkeypatch.setattr(hive, '_appearance_mode', lambda: 'day')
        with patch.object(hive.subprocess, 'run', side_effect=fake_run) as p:
            hive._tmux_restyle()
        assert p.call_count == 1  # only the list-sessions probe
