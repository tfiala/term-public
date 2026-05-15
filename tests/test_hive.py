"""Tests for hive.py apiary support (ADR-0043), pr-check, and branch resolution."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, call, ANY

import pytest

# Import the module under test
sys_path_entry = str(Path(__file__).resolve().parents[1] / 'scripts')
import sys
sys.path.insert(0, sys_path_entry)

import hive


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def fake_hive(tmp_path):
    """Create a fake hive with two git repos."""
    hive_root = tmp_path / 'hive'
    hive_root.mkdir()
    for name in ['repo-1', 'repo-2']:
        repo = hive_root / name
        repo.mkdir()
        (repo / '.git').mkdir()
    return hive_root


@pytest.fixture
def fake_apiary(tmp_path, fake_hive):
    """Create a fake apiary config pointing to fake_hive."""
    hive2 = tmp_path / 'hive2'
    hive2.mkdir()
    (hive2 / 'proj-1').mkdir()
    (hive2 / 'proj-1' / '.git').mkdir()

    config_dir = tmp_path / 'config' / 'hive'
    config_dir.mkdir(parents=True)
    config_file = config_dir / 'apiary.json'
    config_file.write_text(json.dumps({
        'hives': [str(fake_hive), str(hive2)],
    }))
    return config_file, [fake_hive, hive2]


# --- _load_apiary tests ------------------------------------------------------


class TestLoadApiary:
    def test_missing_config_returns_none(self, tmp_path):
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nonexistent.json'):
            assert hive._load_apiary() is None

    def test_valid_config(self, fake_apiary):
        config_file, expected_hives = fake_apiary
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            result = hive._load_apiary()
            assert result is not None
            assert len(result) == 2
            assert [p.resolve() for p in result] == [h.resolve() for h in expected_hives]

    def test_invalid_json_exits(self, tmp_path):
        config_file = tmp_path / 'apiary.json'
        config_file.write_text('not json')
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive._load_apiary()

    def test_tilde_expansion(self, tmp_path):
        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        config_file = config_dir / 'apiary.json'
        config_file.write_text(json.dumps({'hives': ['~/src/flow']}))
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            result = hive._load_apiary()
            assert result is not None
            assert result[0] == Path.home() / 'src' / 'flow'


# --- _looks_like_hive tests --------------------------------------------------


class TestLooksLikeHive:
    def test_dir_with_git_subdir(self, fake_hive):
        """A directory that has a git repo subdirectory looks like a hive."""
        assert hive._looks_like_hive(fake_hive)

    def test_empty_dir(self, tmp_path):
        assert not hive._looks_like_hive(tmp_path)

    def test_only_non_git_subdirs(self, tmp_path):
        (tmp_path / 'plain').mkdir()
        (tmp_path / 'another').mkdir()
        assert not hive._looks_like_hive(tmp_path)

    def test_nonexistent_path(self, tmp_path):
        assert not hive._looks_like_hive(tmp_path / 'does-not-exist')

    def test_file_not_dir(self, tmp_path):
        f = tmp_path / 'a-file'
        f.write_text('hello')
        assert not hive._looks_like_hive(f)

    def test_worktree_dotgit_file(self, tmp_path):
        """A worktree has a .git file (not dir); it still counts as a git repo."""
        wt = tmp_path / 'worktree'
        wt.mkdir()
        (wt / '.git').write_text('gitdir: /somewhere/main/.git/worktrees/wt\n')
        assert hive._looks_like_hive(tmp_path)


# --- _find_hive_root tests ---------------------------------------------------


class TestFindHiveRoot:
    def test_tier1_inside_git_repo(self, fake_hive):
        """When inside a git repo, returns parent."""
        repo = fake_hive / 'repo-1'
        with patch.object(hive, '_git_out', return_value=str(repo)):
            result = hive._find_hive_root()
            assert result == fake_hive

    def test_tier2_cwd_is_hive_base_dir(self, fake_hive, tmp_path):
        """Cwd in a hive base dir (contains git subdirs) resolves without apiary."""
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=fake_hive):
                with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
                    result = hive._find_hive_root()
                    assert result == fake_hive

    def test_tier3_configured_hive_root(self, tmp_path, fake_apiary):
        """When at a configured hive root (even if empty), returns it."""
        config_file, hive_roots = fake_apiary
        with patch.object(hive, '_git_out', return_value=None):
            with patch.object(hive, '_APIARY_CONFIG', config_file):
                with patch('hive.Path.cwd', return_value=hive_roots[0]):
                    result = hive._find_hive_root()
                    assert result == hive_roots[0]

    def test_tier3_subdir_of_configured_hive(self, tmp_path, fake_apiary):
        """When in a non-repo subdir under a configured hive, returns the hive root."""
        config_file, hive_roots = fake_apiary
        subdir = hive_roots[0] / 'some-subdir'
        subdir.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch.object(hive, '_APIARY_CONFIG', config_file):
                with patch('hive.Path.cwd', return_value=subdir):
                    result = hive._find_hive_root()
                    assert result == hive_roots[0]

    def test_tier3_most_specific_match_wins(self, tmp_path):
        """When nested hives exist, the deepest (most specific) match wins."""
        parent_hive = tmp_path / 'src'
        child_hive = parent_hive / 'flow'
        deep_dir = child_hive / 'docs'
        for d in [parent_hive, child_hive, deep_dir]:
            d.mkdir(parents=True, exist_ok=True)
        # Config lists parent before child — child should still win
        config_file = tmp_path / 'apiary.json'
        config_file.write_text(json.dumps(
            {'hives': [str(parent_hive), str(child_hive)]}))
        with patch.object(hive, '_git_out', return_value=None):
            with patch.object(hive, '_APIARY_CONFIG', config_file):
                with patch('hive.Path.cwd', return_value=deep_dir):
                    result = hive._find_hive_root()
                    assert result.resolve() == child_hive.resolve()

    def test_tier4_outside_any_hive(self, tmp_path):
        """When not in a repo or hive root, returns None."""
        empty = tmp_path / 'empty'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
                    result = hive._find_hive_root()
                    assert result is None

    def test_deleted_cwd_exits_with_clear_message(self, tmp_path, capsys):
        """If Path.cwd() raises (cwd deleted/moved), exit with a readable message."""
        def _raise_fnf():
            raise FileNotFoundError(2, 'No such file or directory')
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', side_effect=_raise_fnf):
                with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
                    with pytest.raises(SystemExit) as excinfo:
                        hive._find_hive_root()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert 'Cannot determine current directory' in err
        assert 'cd' in err


# --- _run_apiary tests --------------------------------------------------------


class TestRunApiary:
    def test_runs_fn_for_each_hive(self, fake_apiary):
        config_file, hive_roots = fake_apiary
        called = []
        hive._run_apiary(hive_roots, lambda h: called.append(h))
        assert called == hive_roots

    def test_skips_nonexistent_hives(self, tmp_path):
        real = tmp_path / 'real'
        real.mkdir()
        fake = tmp_path / 'nonexistent'
        called = []
        hive._run_apiary([real, fake], lambda h: called.append(h))
        assert called == [real]


# --- cmd_status apiary tests -------------------------------------------------


class TestCmdStatusApiary:
    def test_apiary_flag_requires_config(self, tmp_path, capsys):
        args = hive.argparse.Namespace(compact=False, apiary=True, color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            with pytest.raises(SystemExit):
                hive.cmd_status(args)

    def test_implicit_apiary_from_outside_hive(self, tmp_path, fake_apiary, capsys):
        """status from outside a hive falls back to apiary."""
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(compact=False, apiary=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', config_file):
                    with patch.object(hive, '_fetch_all_parallel'):
                        with patch.object(hive, '_report_repo_status'):
                            hive.cmd_status(args)
        out = capsys.readouterr().out
        assert 'not in a hive' in out
        assert 'Apiary' in out


# --- cmd_pull apiary tests ---------------------------------------------------


class TestCmdPullApiary:
    def test_no_implicit_apiary(self, tmp_path, fake_apiary, capsys):
        """pull from outside a hive does NOT fall back to apiary."""
        config_file, _ = fake_apiary
        args = hive.argparse.Namespace(
            compact=False, apiary=False, push=False, quiet=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', config_file):
                    with pytest.raises(SystemExit):
                        hive.cmd_pull(args)
        err = capsys.readouterr().err
        assert '--apiary' in err

    def test_apiary_flag_requires_config(self, tmp_path):
        args = hive.argparse.Namespace(
            compact=False, apiary=True, push=False, quiet=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            with pytest.raises(SystemExit):
                hive.cmd_pull(args)


# --- cmd_create apiary tests -------------------------------------------------


class TestCmdCreateApiary:
    def test_apiary_flag_errors(self, capsys):
        args = hive.argparse.Namespace(apiary=True, name_prefix=None, color=False)
        with pytest.raises(SystemExit):
            hive.cmd_create(args)
        err = capsys.readouterr().err
        assert 'not supported in apiary mode' in err


# --- cmd_create URL / sibling-inference tests --------------------------------


class TestCmdCreate:
    """Tests for `hive create [<url>]` — URL handling and sibling inference."""

    def _hive_with_siblings(self, tmp_path, names):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        for name in names:
            ws = hive_root / name
            ws.mkdir()
            (ws / '.git').mkdir()
        return hive_root

    def _make_git_out(self, origins):
        def fake(args, cwd=None):
            if args[:3] == ['remote', 'get-url', 'origin'] and cwd:
                return origins.get(Path(cwd).name)
            return None
        return fake

    def test_explicit_url_clones_from_it(self, tmp_path):
        hive_root = self._hive_with_siblings(tmp_path, [])
        target = hive_root / 'repo-1'
        runs = []

        def fake_git(args, cwd=None, timeout=None):
            runs.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        args = hive.argparse.Namespace(
            apiary=False, url='https://x/y.git', name_prefix=None, color=False)
        with patch.object(hive, '_find_hive_root', return_value=hive_root), \
             patch.object(hive, '_infer_next_repo_dir', return_value=target), \
             patch.object(hive, '_git', side_effect=fake_git):
            hive.cmd_create(args)

        assert runs == [['clone', 'https://x/y.git', str(target)]]

    def test_no_url_infers_from_siblings(self, tmp_path):
        hive_root = self._hive_with_siblings(tmp_path, ['repo-1'])
        target = hive_root / 'repo-2'
        runs = []

        def fake_git(args, cwd=None, timeout=None):
            runs.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        args = hive.argparse.Namespace(
            apiary=False, url=None, name_prefix=None, color=False)
        with patch.object(hive, '_find_hive_root', return_value=hive_root), \
             patch.object(hive, '_infer_next_repo_dir', return_value=target), \
             patch.object(hive, '_git_out',
                          side_effect=self._make_git_out(
                              {'repo-1': 'https://x/y.git'})), \
             patch.object(hive, '_git', side_effect=fake_git):
            hive.cmd_create(args)

        assert runs == [['clone', 'https://x/y.git', str(target)]]

    def test_no_url_empty_hive_exits(self, tmp_path, capsys):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        args = hive.argparse.Namespace(
            apiary=False, url=None, name_prefix=None, color=False)
        with patch.object(hive, '_find_hive_root', return_value=hive_root):
            with pytest.raises(SystemExit):
                hive.cmd_create(args)
        assert 'No shared origin' in capsys.readouterr().err

    def test_no_url_mixed_origins_exits(self, tmp_path, capsys):
        hive_root = self._hive_with_siblings(tmp_path, ['repo-1', 'repo-2'])
        args = hive.argparse.Namespace(
            apiary=False, url=None, name_prefix=None, color=False)
        with patch.object(hive, '_find_hive_root', return_value=hive_root), \
             patch.object(hive, '_git_out',
                          side_effect=self._make_git_out(
                              {'repo-1': 'https://a/x.git',
                               'repo-2': 'https://b/x.git'})):
            with pytest.raises(SystemExit):
                hive.cmd_create(args)
        assert 'No shared origin' in capsys.readouterr().err


class TestInferCloneUrlFromSiblings:
    def test_returns_shared_url_when_all_match(self, tmp_path):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        for name in ['repo-1', 'repo-2']:
            ws = hive_root / name
            ws.mkdir()
            (ws / '.git').mkdir()
        with patch.object(hive, '_git_out',
                          return_value='https://shared/x.git'):
            assert hive._infer_clone_url_from_siblings(hive_root) == \
                'https://shared/x.git'

    def test_returns_none_for_empty_hive(self, tmp_path):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        assert hive._infer_clone_url_from_siblings(hive_root) is None

    def test_returns_none_for_mixed_origins(self, tmp_path):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        for name in ['repo-1', 'repo-2']:
            ws = hive_root / name
            ws.mkdir()
            (ws / '.git').mkdir()
        urls = {'repo-1': 'https://a/x.git', 'repo-2': 'https://b/x.git'}

        def fake(args, cwd=None):
            return urls.get(Path(cwd).name) if cwd else None

        with patch.object(hive, '_git_out', side_effect=fake):
            assert hive._infer_clone_url_from_siblings(hive_root) is None

    def test_ignores_non_git_directories(self, tmp_path):
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        (hive_root / 'notes').mkdir()  # no .git — must be ignored
        ws = hive_root / 'repo-1'
        ws.mkdir()
        (ws / '.git').mkdir()
        with patch.object(hive, '_git_out',
                          return_value='https://x/y.git'):
            assert hive._infer_clone_url_from_siblings(hive_root) == \
                'https://x/y.git'


# --- _save_apiary / _storable_path tests ------------------------------------


class TestSaveApiary:
    def test_creates_config_dir_and_file(self, tmp_path):
        config_file = tmp_path / 'nested' / 'dir' / 'apiary.json'
        hive_dir = tmp_path / 'my-hive'
        hive_dir.mkdir()
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive._save_apiary([hive_dir])
        assert config_file.is_file()
        data = json.loads(config_file.read_text())
        assert len(data['hives']) == 1
        assert Path(data['hives'][0]).expanduser().resolve() == hive_dir.resolve()

    def test_round_trip(self, tmp_path):
        config_file = tmp_path / 'apiary.json'
        hive_a = tmp_path / 'a'
        hive_b = tmp_path / 'b'
        hive_a.mkdir()
        hive_b.mkdir()
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive._save_apiary([hive_a, hive_b])
            loaded = hive._load_apiary()
        assert loaded is not None
        assert [p.resolve() for p in loaded] == [hive_a.resolve(), hive_b.resolve()]


# --- _storable_path / _display_path tests ------------------------------------


class TestStorablePath:
    def test_path_under_home_uses_tilde(self, tmp_path):
        target = tmp_path / 'hive'
        target.mkdir()
        with patch('hive.Path.home', return_value=tmp_path):
            result = hive._storable_path(target)
        assert result == '~/hive'

    def test_path_outside_home_uses_absolute(self, tmp_path):
        fake_home = tmp_path / 'home'
        fake_home.mkdir()
        target = tmp_path / 'other'
        target.mkdir()
        with patch('hive.Path.home', return_value=fake_home):
            result = hive._storable_path(target)
        assert result == str(target.resolve())
        assert '~' not in result


class TestDisplayPath:
    def test_path_under_home_uses_tilde(self, tmp_path):
        target = tmp_path / 'src' / 'flow'
        target.mkdir(parents=True)
        with patch('hive.Path.home', return_value=tmp_path):
            result = hive._display_path(target)
        assert result == '~/src/flow'

    def test_path_outside_home_uses_absolute(self, tmp_path):
        fake_home = tmp_path / 'home'
        fake_home.mkdir()
        target = tmp_path / 'other'
        target.mkdir()
        with patch('hive.Path.home', return_value=fake_home):
            result = hive._display_path(target)
        assert result == str(target)
        assert '~' not in result


# --- cmd_apiary tests --------------------------------------------------------


class TestCmdApiary:
    def test_list_empty(self, tmp_path, capsys):
        args = hive.argparse.Namespace(
            apiary_action='list', apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            hive.cmd_apiary(args)
        out = capsys.readouterr().out
        assert 'No hives configured' in out

    def test_list_shows_hives(self, fake_apiary, capsys):
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            apiary_action='list', apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive.cmd_apiary(args)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == len(hive_roots)
        for h in hive_roots:
            assert any(str(h) in line for line in lines)

    def test_list_marks_missing(self, tmp_path, capsys):
        config_file = tmp_path / 'apiary.json'
        config_file.write_text(json.dumps(
            {'hives': [str(tmp_path / 'gone')]}))
        args = hive.argparse.Namespace(
            apiary_action='list', apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive.cmd_apiary(args)
        out = capsys.readouterr().out
        assert 'not found' in out

    def test_add_new_hive(self, tmp_path, capsys):
        config_file = tmp_path / 'config' / 'apiary.json'
        new_hive = tmp_path / 'my-hive'
        new_hive.mkdir()
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(new_hive), apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive.cmd_apiary(args)
            loaded = hive._load_apiary()
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].resolve() == new_hive.resolve()
        out = capsys.readouterr().out
        assert 'Added' in out

    def test_add_defaults_to_cwd(self, tmp_path, capsys):
        config_file = tmp_path / 'apiary.json'
        cwd_dir = tmp_path / 'cwd-hive'
        cwd_dir.mkdir()
        args = hive.argparse.Namespace(
            apiary_action='add', path=None, apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with patch('hive.Path.cwd', return_value=cwd_dir):
                hive.cmd_apiary(args)
                loaded = hive._load_apiary()
        assert loaded is not None
        assert loaded[0].resolve() == cwd_dir.resolve()

    def test_add_appends_to_existing(self, tmp_path, fake_apiary, capsys):
        config_file, hive_roots = fake_apiary
        new_hive = tmp_path / 'third'
        new_hive.mkdir()
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(new_hive), apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive.cmd_apiary(args)
            loaded = hive._load_apiary()
        assert len(loaded) == 3

    def test_add_duplicate_errors(self, fake_apiary, capsys):
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(hive_roots[0]), apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'Already in apiary' in err

    def test_add_nonexistent_dir_errors(self, tmp_path, capsys):
        config_file = tmp_path / 'apiary.json'
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(tmp_path / 'nope'), apiary=False,
            color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'Not a directory' in err

    def test_remove_hive(self, fake_apiary, capsys):
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            apiary_action='remove', path=str(hive_roots[0]), apiary=False,
            color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            hive.cmd_apiary(args)
            loaded = hive._load_apiary()
        assert len(loaded) == 1
        assert loaded[0].resolve() == hive_roots[1].resolve()
        out = capsys.readouterr().out
        assert 'Removed' in out

    def test_remove_defaults_to_cwd(self, tmp_path, capsys):
        hive_dir = tmp_path / 'my-hive'
        hive_dir.mkdir()
        config_file = tmp_path / 'apiary.json'
        config_file.write_text(json.dumps({'hives': [str(hive_dir)]}))
        args = hive.argparse.Namespace(
            apiary_action='remove', path=None, apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with patch('hive.Path.cwd', return_value=hive_dir):
                hive.cmd_apiary(args)
                loaded = hive._load_apiary()
        assert loaded == []

    def test_remove_not_found_errors(self, fake_apiary, capsys):
        config_file, _ = fake_apiary
        args = hive.argparse.Namespace(
            apiary_action='remove', path='/not/in/apiary', apiary=False,
            color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'Not in apiary' in err

    def test_remove_no_config_errors(self, tmp_path, capsys):
        args = hive.argparse.Namespace(
            apiary_action='remove', path='/some/path', apiary=False,
            color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'No apiary config' in err

    def test_add_child_of_existing_errors(self, tmp_path, capsys):
        """Adding a child of an existing hive is rejected."""
        parent = tmp_path / 'src'
        child = parent / 'flow'
        parent.mkdir()
        child.mkdir()
        config_file = tmp_path / 'apiary.json'
        config_file.write_text(json.dumps({'hives': [str(parent)]}))
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(child), apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'Overlaps' in err

    def test_add_parent_of_existing_errors(self, tmp_path, capsys):
        """Adding a parent of an existing hive is rejected."""
        parent = tmp_path / 'src'
        child = parent / 'flow'
        parent.mkdir()
        child.mkdir()
        config_file = tmp_path / 'apiary.json'
        config_file.write_text(json.dumps({'hives': [str(child)]}))
        args = hive.argparse.Namespace(
            apiary_action='add', path=str(parent), apiary=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with pytest.raises(SystemExit):
                hive.cmd_apiary(args)
        err = capsys.readouterr().err
        assert 'Overlaps' in err


# --- _default_branch tests ----------------------------------------------------


class TestDefaultBranch:
    def test_reads_origin_head(self):
        """Returns the branch name from origin/HEAD."""
        with patch.object(hive, '_git_out',
                          return_value='refs/remotes/origin/infra-dev'):
            assert hive._default_branch(Path('/repo')) == 'infra-dev'

    def test_returns_main_as_default(self):
        with patch.object(hive, '_git_out',
                          return_value='refs/remotes/origin/main'):
            assert hive._default_branch(Path('/repo')) == 'main'

    def test_falls_back_to_main_when_missing(self):
        """When origin/HEAD is not set, falls back to 'main'."""
        with patch.object(hive, '_git_out', return_value=None):
            assert hive._default_branch(Path('/repo')) == 'main'

    def test_falls_back_to_main_on_empty_string(self):
        with patch.object(hive, '_git_out', return_value=''):
            assert hive._default_branch(Path('/repo')) == 'main'

    def test_slash_in_branch_name(self):
        """Branch names with slashes (e.g. release/2026) are preserved."""
        with patch.object(hive, '_git_out',
                          return_value='refs/remotes/origin/release/2026'):
            assert hive._default_branch(Path('/repo')) == 'release/2026'

    def test_deeply_nested_branch_name(self):
        """Deeply nested branch names are preserved."""
        with patch.object(hive, '_git_out',
                          return_value='refs/remotes/origin/feat/team/big-refactor'):
            assert hive._default_branch(Path('/repo')) == 'feat/team/big-refactor'


# --- _classify_pr tests -------------------------------------------------------


class TestClassifyPr:
    def test_open(self):
        assert hive._classify_pr({'state': 'open', 'merged': False}) == 'open'

    def test_merged_via_flag(self):
        assert hive._classify_pr({'state': 'closed', 'merged': True}) == 'merged'

    def test_merged_via_merged_at(self):
        assert hive._classify_pr(
            {'state': 'closed', 'merged_at': '2026-01-01'}) == 'merged'

    def test_closed_not_merged(self):
        assert hive._classify_pr({'state': 'closed', 'merged': False}) == 'closed'

    def test_missing_state_defaults_closed(self):
        assert hive._classify_pr({}) == 'closed'


# --- _get_pr_info tests ------------------------------------------------------


class TestGetPrInfo:
    def _fj_result(self, stdout, rc=0):
        """Helper: create a CompletedProcess for fj mock."""
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr='')

    def test_returns_open_pr(self):
        pr_json = json.dumps([{
            'number': 42,
            'title': 'feat: add scaling',
            'state': 'open',
            'merged': False,
        }])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'feat/scaling')
        assert result == {'number': 42, 'title': 'feat: add scaling', 'state': 'open'}

    def test_returns_merged_pr(self):
        pr_json = json.dumps([{
            'number': 38,
            'title': 'fix: mount race',
            'state': 'closed',
            'merged': True,
        }])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'fix/race')
        assert result == {'number': 38, 'title': 'fix: mount race', 'state': 'merged'}

    def test_returns_closed_not_merged(self):
        pr_json = json.dumps([{
            'number': 10,
            'title': 'abandoned PR',
            'state': 'closed',
            'merged': False,
        }])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'old-branch')
        assert result == {'number': 10, 'title': 'abandoned PR', 'state': 'closed'}

    def test_returns_none_on_null_output(self):
        with patch('hive.subprocess.run', return_value=self._fj_result('null')):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_returns_none_on_empty_array(self):
        with patch('hive.subprocess.run', return_value=self._fj_result('[]')):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_returns_none_on_error_exit(self):
        with patch('hive.subprocess.run', return_value=self._fj_result('', rc=1)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_returns_none_on_timeout(self):
        with patch('hive.subprocess.run',
                   side_effect=subprocess.TimeoutExpired([], 10)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_returns_none_on_fj_not_found(self):
        with patch('hive.subprocess.run', side_effect=FileNotFoundError):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_returns_none_on_malformed_json(self):
        """Non-array JSON (e.g. error object) is handled gracefully."""
        with patch('hive.subprocess.run',
                   return_value=self._fj_result('{"error": "not found"}')):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result is None

    def test_merged_at_field_detected(self):
        """merged_at without merged=True still counts as merged."""
        pr_json = json.dumps([{
            'number': 5,
            'title': 'old PR',
            'state': 'closed',
            'merged_at': '2026-01-01T00:00:00Z',
        }])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result['state'] == 'merged'

    def test_single_pr_uses_that_pr(self):
        """Single matching PR is returned directly."""
        pr_json = json.dumps([
            {'number': 50, 'title': 'only one', 'state': 'open', 'merged': False},
        ])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result == {'number': 50, 'title': 'only one', 'state': 'open'}

    def test_open_pr_wins_over_earlier_merged(self):
        """An open PR is preferred even if a merged PR appears first."""
        pr_json = json.dumps([
            {'number': 40, 'title': 'old merged', 'state': 'closed', 'merged': True},
            {'number': 50, 'title': 'new open', 'state': 'open', 'merged': False},
        ])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result == {'number': 50, 'title': 'new open', 'state': 'open'}

    def test_open_pr_wins_over_later_closed(self):
        """Open first, closed second — still picks the open one."""
        pr_json = json.dumps([
            {'number': 50, 'title': 'open one', 'state': 'open', 'merged': False},
            {'number': 40, 'title': 'closed', 'state': 'closed', 'merged': True},
        ])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result == {'number': 50, 'title': 'open one', 'state': 'open'}

    def test_all_closed_returns_first(self):
        """When all PRs are closed/merged, returns the first."""
        pr_json = json.dumps([
            {'number': 50, 'title': 'newer merged', 'state': 'closed', 'merged': True},
            {'number': 40, 'title': 'older closed', 'state': 'closed', 'merged': False},
        ])
        with patch('hive.subprocess.run', return_value=self._fj_result(pr_json)):
            result = hive._get_pr_info(Path('/repo'), 'branch')
        assert result == {'number': 50, 'title': 'newer merged', 'state': 'merged'}


# --- _clean_pr_branch tests --------------------------------------------------


class TestCleanPrBranch:
    """Tests for _clean_pr_branch.

    All tests patch _default_branch directly so that _git_out mocks only
    handle the porcelain check, not the symbolic-ref call inside
    _default_branch.
    """

    @staticmethod
    def _ok(stdout=''):
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr='')

    @staticmethod
    def _fail(stderr='error'):
        return subprocess.CompletedProcess([], 1, stdout='', stderr=stderr)

    @staticmethod
    def _git_dispatch(results_by_cmd):
        """Return a side_effect that dispatches on the first git arg."""
        def side_effect(args, **kwargs):
            # args = ['checkout', ...] or ['pull', ...] or ['branch', ...]
            cmd = args[0]
            if cmd in results_by_cmd:
                return results_by_cmd[cmd]
            return subprocess.CompletedProcess([], 0, stdout='', stderr='')
        return side_effect

    def test_success_calls_checkout_pull_delete(self):
        """Verifies all three git commands are called in order."""
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value=''):
                with patch.object(hive, '_git') as mock_git:
                    mock_git.return_value = self._ok('Already up to date')
                    result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result == {'success': True, 'error': None}
        cmds = [c.args[0] for c in mock_git.call_args_list]
        assert cmds == [
            ['checkout', 'main'],
            ['pull', '--rebase', 'origin', 'main'],
            ['branch', '-D', 'old-branch'],
        ]

    def test_dirty_repo_skipped(self):
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value='M file.py'):
                result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert 'uncommitted' in result['error']

    def test_multiple_dirty_files(self):
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              return_value='M a.py\nM b.py\n?? c.py'):
                result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert '3 uncommitted files' in result['error']

    def test_status_check_failure(self):
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value=None):
                result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert 'cannot determine' in result['error']

    def test_checkout_failure_does_not_call_pull(self):
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value=''):
                with patch.object(hive, '_git') as mock_git:
                    mock_git.return_value = self._fail()
                    result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert 'checkout' in result['error']
        # Only checkout was attempted — no pull or branch delete
        assert len(mock_git.call_args_list) == 1
        assert mock_git.call_args_list[0].args[0] == ['checkout', 'main']

    def test_pull_failure_aborts_rebase(self):
        dispatch = self._git_dispatch({
            'checkout': self._ok(),
            'pull': self._fail('conflict'),
            'rebase': self._ok(),
        })
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value=''):
                with patch.object(hive, '_git', side_effect=dispatch) as mock_git:
                    result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert 'pull' in result['error']
        # Verify rebase --abort was called after pull failure
        cmds = [c.args[0] for c in mock_git.call_args_list]
        assert ['rebase', '--abort'] in cmds
        # branch -D should NOT have been called
        assert not any(c[0] == 'branch' for c in cmds)

    def test_branch_delete_failure(self):
        dispatch = self._git_dispatch({
            'checkout': self._ok(),
            'pull': self._ok('Already up to date'),
            'branch': self._fail(),
        })
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value=''):
                with patch.object(hive, '_git', side_effect=dispatch):
                    result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is False
        assert 'delete branch' in result['error']

    def test_uses_actual_default_branch(self):
        """Checkout and pull target the repo's actual default branch."""
        with patch.object(hive, '_default_branch', return_value='infra-dev'):
            with patch.object(hive, '_git_out', return_value=''):
                with patch.object(hive, '_git') as mock_git:
                    mock_git.return_value = self._ok('Already up to date')
                    result = hive._clean_pr_branch(Path('/repo'), 'old-branch')
        assert result['success'] is True
        cmds = [c.args[0] for c in mock_git.call_args_list]
        assert cmds == [
            ['checkout', 'infra-dev'],
            ['pull', '--rebase', 'origin', 'infra-dev'],
            ['branch', '-D', 'old-branch'],
        ]


# --- _pr_check_single_hive tests ---------------------------------------------


class TestPrCheckSingleHive:
    """Tests for _pr_check_single_hive.

    All tests patch _default_branch directly so that _git_out mocks only
    handle rev-parse (current branch), not the symbolic-ref call inside
    _default_branch.
    """

    def test_all_on_default_branch(self, fake_hive, capsys):
        """When all repos are on main, shows summary message."""
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value='main'):
                hive._pr_check_single_hive(fake_hive, clean=False)
        out = capsys.readouterr().out
        assert 'All 2 repos on default branch' in out

    def test_non_main_default_branch(self, fake_hive, capsys):
        """Repos on their non-main default branch are not flagged."""
        with patch.object(hive, '_default_branch', return_value='infra-dev'):
            with patch.object(hive, '_git_out', return_value='infra-dev'):
                hive._pr_check_single_hive(fake_hive, clean=False)
        out = capsys.readouterr().out
        assert 'All 2 repos on default branch' in out

    def test_shows_open_pr(self, fake_hive, capsys):
        """Repos with open PRs show green indicator."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'feat/new'
                return 'main'
            return ''

        pr_info = {'number': 42, 'title': 'feat: new feature', 'state': 'open'}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    hive._pr_check_single_hive(fake_hive, clean=False)
        out = capsys.readouterr().out
        assert '#42' in out
        assert 'feat: new feature' in out

    def test_shows_merged_pr_strikethrough(self, fake_hive, capsys):
        """Repos with merged PRs show strikethrough."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'fix/bug'
                return 'main'
            return ''

        pr_info = {'number': 38, 'title': 'fix: bug', 'state': 'merged'}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    hive._pr_check_single_hive(fake_hive, clean=False)
        out = capsys.readouterr().out
        assert '#38' in out
        assert 'merged' in out

    def test_shows_no_pr_branch(self, fake_hive, capsys):
        """Repos with no PR show branch name and question mark."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'local-branch'
                return 'main'
            return ''

        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=None):
                    hive._pr_check_single_hive(fake_hive, clean=False)
        out = capsys.readouterr().out
        assert 'local-branch' in out
        assert 'no PR' in out

    def test_no_repos_in_hive(self, tmp_path, capsys):
        """Empty hive shows error."""
        empty_hive = tmp_path / 'empty'
        empty_hive.mkdir()
        hive._pr_check_single_hive(empty_hive, clean=False)
        out = capsys.readouterr().out
        assert 'No git repos found' in out

    def test_clean_skips_open_prs(self, fake_hive, capsys):
        """--clean does not touch repos with open PRs."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'feat/open'
                return 'main'
            return ''

        pr_info = {'number': 42, 'title': 'open PR', 'state': 'open'}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    with patch.object(hive, '_clean_pr_branch') as mock_clean:
                        hive._pr_check_single_hive(fake_hive, clean=True)
        mock_clean.assert_not_called()
        out = capsys.readouterr().out
        assert 'Nothing to clean' in out

    def test_clean_processes_merged_prs(self, fake_hive, capsys):
        """--clean cleans up repos with merged PRs."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'fix/done'
                return 'main'
            return ''

        pr_info = {'number': 38, 'title': 'done PR', 'state': 'merged'}
        clean_result = {'success': True, 'error': None}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    with patch.object(hive, '_clean_pr_branch',
                                      return_value=clean_result):
                        hive._pr_check_single_hive(fake_hive, clean=True)
        out = capsys.readouterr().out
        assert 'fix/done' in out
        assert 'main' in out
        assert 'Cleaning 1 stale branch' in out

    def test_clean_processes_closed_not_merged_prs(self, fake_hive, capsys):
        """--clean also cleans up closed (not merged) PRs."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'abandoned'
                return 'main'
            return ''

        pr_info = {'number': 10, 'title': 'abandoned PR', 'state': 'closed'}
        clean_result = {'success': True, 'error': None}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    with patch.object(hive, '_clean_pr_branch',
                                      return_value=clean_result) as mock_clean:
                        hive._pr_check_single_hive(fake_hive, clean=True)
        mock_clean.assert_called_once()
        out = capsys.readouterr().out
        assert 'Cleaning 1 stale branch' in out

    def test_clean_skips_no_pr_branches(self, fake_hive, capsys):
        """--clean does not touch repos with no associated PR."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'local-only'
                return 'main'
            return ''

        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=None):
                    with patch.object(hive, '_clean_pr_branch') as mock_clean:
                        hive._pr_check_single_hive(fake_hive, clean=True)
        mock_clean.assert_not_called()
        out = capsys.readouterr().out
        assert 'Nothing to clean' in out

    def test_clean_shows_failure(self, fake_hive, capsys):
        """--clean displays errors from _clean_pr_branch."""
        def git_out_side_effect(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                if cwd and cwd.name == 'repo-1':
                    return 'fix/dirty'
                return 'main'
            return ''

        pr_info = {'number': 38, 'title': 'done PR', 'state': 'merged'}
        clean_result = {'success': False, 'error': '2 uncommitted files'}
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', side_effect=git_out_side_effect):
                with patch.object(hive, '_get_pr_info', return_value=pr_info):
                    with patch.object(hive, '_clean_pr_branch',
                                      return_value=clean_result):
                        hive._pr_check_single_hive(fake_hive, clean=True)
        out = capsys.readouterr().out
        assert '2 uncommitted files' in out


# --- cmd_pr_check tests ------------------------------------------------------


class TestCmdPrCheck:
    def test_apiary_flag_requires_config(self, tmp_path):
        args = hive.argparse.Namespace(
            apiary=True, clean=False, color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            with pytest.raises(SystemExit):
                hive.cmd_pr_check(args)

    def test_clean_without_apiary_from_outside_hive_errors(
            self, tmp_path, fake_apiary, capsys):
        """--clean from outside a hive requires --apiary."""
        config_file, _ = fake_apiary
        args = hive.argparse.Namespace(
            apiary=False, clean=True, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', config_file):
                    with pytest.raises(SystemExit):
                        hive.cmd_pr_check(args)
        err = capsys.readouterr().err
        assert '--apiary' in err

    def test_implicit_apiary_without_clean(
            self, tmp_path, fake_apiary, capsys):
        """pr-check without --clean falls back to apiary from outside hive."""
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            apiary=False, clean=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', config_file):
                    with patch.object(hive, '_pr_check_single_hive'):
                        hive.cmd_pr_check(args)
        out = capsys.readouterr().out
        assert 'not in a hive' in out
        assert 'Apiary' in out

    def test_no_hive_no_apiary_errors(self, tmp_path, capsys):
        """Outside hive with no apiary config errors."""
        args = hive.argparse.Namespace(
            apiary=False, clean=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
                    with pytest.raises(SystemExit):
                        hive.cmd_pr_check(args)


# --- _get_repo_slug tests ---------------------------------------------------


class TestGetRepoSlug:
    def test_https_url(self):
        with patch.object(hive, '_git_out',
                          return_value='https://git.example.com/acme/widget.git'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_https_url_no_dotgit(self):
        with patch.object(hive, '_git_out',
                          return_value='https://git.example.com/acme/widget'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_ssh_url(self):
        with patch.object(hive, '_git_out',
                          return_value='git@git.example.com:acme/widget.git'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_trailing_slash_stripped(self):
        with patch.object(hive, '_git_out',
                          return_value='https://git.example.com/acme/widget.git/'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_ssh_url_no_dotgit(self):
        with patch.object(hive, '_git_out',
                          return_value='git@git.example.com:acme/widget'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_https_with_port(self):
        with patch.object(hive, '_git_out',
                          return_value='https://git.example.com:3000/acme/widget.git'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com:3000/acme/widget'

    def test_https_with_credentials(self):
        """HTTPS URLs with user@ credentials strip the username."""
        with patch.object(hive, '_git_out',
                          return_value='https://user@git.example.com/acme/widget.git'):
            assert hive._get_repo_slug(Path('/repo')) == 'git.example.com/acme/widget'

    def test_different_hosts_produce_different_slugs(self):
        """Same org/repo on different hosts must not collide."""
        with patch.object(hive, '_git_out',
                          return_value='https://git.example.com/acme/widget.git'):
            slug_a = hive._get_repo_slug(Path('/repo-a'))
        with patch.object(hive, '_git_out',
                          return_value='https://git.other.example/acme/widget.git'):
            slug_b = hive._get_repo_slug(Path('/repo-b'))
        assert slug_a != slug_b
        assert slug_a == 'git.example.com/acme/widget'
        assert slug_b == 'git.other.example/acme/widget'

    def test_no_remote_returns_none(self):
        with patch.object(hive, '_git_out', return_value=None):
            assert hive._get_repo_slug(Path('/repo')) is None

    def test_empty_string_returns_none(self):
        with patch.object(hive, '_git_out', return_value=''):
            assert hive._get_repo_slug(Path('/repo')) is None


# --- _get_issues tests -------------------------------------------------------


class TestGetIssues:
    def _make_run(self, stdout, returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')

    def test_returns_issues(self):
        data = json.dumps([
            {'number': 42, 'title': 'Bug report'},
            {'number': 38, 'title': 'Feature request'},
        ])
        with patch('hive.subprocess.run', return_value=self._make_run(data)):
            result = hive._get_issues(Path('/repo'))
        assert len(result) == 2
        assert result[0] == {'number': 42, 'title': 'Bug report'}

    def test_null_output_returns_empty_list(self):
        with patch('hive.subprocess.run',
                   return_value=self._make_run('null')):
            assert hive._get_issues(Path('/repo')) == []

    def test_empty_array_returns_empty_list(self):
        with patch('hive.subprocess.run',
                   return_value=self._make_run('[]')):
            assert hive._get_issues(Path('/repo')) == []

    def test_error_exit_returns_none(self):
        with patch('hive.subprocess.run',
                   return_value=self._make_run('', returncode=1)):
            assert hive._get_issues(Path('/repo')) is None

    def test_timeout_returns_none(self):
        with patch('hive.subprocess.run',
                   side_effect=subprocess.TimeoutExpired([], 15)):
            assert hive._get_issues(Path('/repo')) is None

    def test_fj_not_found_returns_none(self):
        with patch('hive.subprocess.run', side_effect=FileNotFoundError):
            assert hive._get_issues(Path('/repo')) is None

    def test_malformed_json_returns_none(self):
        with patch('hive.subprocess.run',
                   return_value=self._make_run('not json')):
            assert hive._get_issues(Path('/repo')) is None


# --- _issues_display tests ---------------------------------------------------


class TestIssuesDisplay:
    def test_shows_issues(self, tmp_path, capsys):
        """Repos with open issues are displayed."""
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        repo = hive_root / 'repo-1'
        repo.mkdir()
        (repo / '.git').mkdir()

        issues = [{'number': 42, 'title': 'Bug report'}]
        with patch.object(hive, '_get_repo_slug', return_value='acme/widget'):
            with patch.object(hive, '_get_issues', return_value=issues):
                hive._issues_display([hive_root])
        out = capsys.readouterr().out
        assert '#42' in out
        assert 'Bug report' in out
        assert '1 open issue' in out
        assert '1 repos checked' in out

    def test_no_issues(self, tmp_path, capsys):
        """When no repos have issues, shows summary."""
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        repo = hive_root / 'repo-1'
        repo.mkdir()
        (repo / '.git').mkdir()

        with patch.object(hive, '_get_repo_slug', return_value='acme/widget'):
            with patch.object(hive, '_get_issues', return_value=[]):
                hive._issues_display([hive_root])
        out = capsys.readouterr().out
        assert 'No open issues' in out
        assert '1 repos checked' in out

    def test_deduplicates_across_hives(self, tmp_path, capsys):
        """Same repo slug across two hives is queried only once."""
        hive1 = tmp_path / 'hive1'
        hive2 = tmp_path / 'hive2'
        for h in [hive1, hive2]:
            h.mkdir()
            repo = h / 'repo-1'
            repo.mkdir()
            (repo / '.git').mkdir()

        with patch.object(hive, '_get_repo_slug', return_value='infra/same-repo'):
            with patch.object(hive, '_get_issues',
                              return_value=[{'number': 1, 'title': 'Issue'}]) as mock_issues:
                hive._issues_display([hive1, hive2])
        assert mock_issues.call_count == 1

    def test_query_failure_shown(self, fake_hive, capsys):
        """Query failures show a warning."""
        with patch.object(hive, '_get_repo_slug', return_value='infra/broken'):
            with patch.object(hive, '_get_issues', return_value=None):
                hive._issues_display([fake_hive])
        out = capsys.readouterr().out
        assert 'query failed' in out

    def test_no_repos(self, tmp_path, capsys):
        """Empty hive shows error."""
        empty = tmp_path / 'empty'
        empty.mkdir()
        hive._issues_display([empty])
        out = capsys.readouterr().out
        assert 'No git repos found' in out

    def test_multiple_repos_with_issues(self, tmp_path, capsys):
        """Multiple repos each with issues are all shown."""
        hive_root = tmp_path / 'hive'
        hive_root.mkdir()
        for name in ['repo-a', 'repo-b']:
            repo = hive_root / name
            repo.mkdir()
            (repo / '.git').mkdir()

        slugs = iter(['org/alpha', 'org/beta'])

        def slug_side_effect(rp):
            return next(slugs)

        # Key issue data off the repo path to avoid thread-safety issues
        # with a shared counter.  _issues_display passes the repo_path
        # to _get_issues, so we can use the repo directory name.
        def issues_side_effect(rp):
            n = 1 if rp.name == 'repo-a' else 2
            return [{'number': n, 'title': f'Issue {n}'}]

        with patch.object(hive, '_get_repo_slug', side_effect=slug_side_effect):
            with patch.object(hive, '_get_issues',
                              side_effect=issues_side_effect):
                hive._issues_display([hive_root])
        out = capsys.readouterr().out
        assert '#1' in out
        assert '#2' in out
        assert '2 repos checked' in out


# --- cmd_issues tests --------------------------------------------------------


class TestCmdIssues:
    def test_apiary_flag_requires_config(self, tmp_path):
        args = hive.argparse.Namespace(apiary=True, color=False)
        with patch.object(hive, '_APIARY_CONFIG', tmp_path / 'nope.json'):
            with pytest.raises(SystemExit):
                hive.cmd_issues(args)

    def test_implicit_apiary_from_outside_hive(
            self, tmp_path, fake_apiary, capsys):
        """issues falls back to apiary when outside a hive."""
        config_file, _ = fake_apiary
        args = hive.argparse.Namespace(apiary=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG', config_file):
                    with patch.object(hive, '_issues_display'):
                        hive.cmd_issues(args)
        out = capsys.readouterr().out
        assert 'not in a hive' in out

    def test_no_hive_no_apiary_errors(self, tmp_path, capsys):
        args = hive.argparse.Namespace(apiary=False, color=False)
        empty = tmp_path / 'nowhere'
        empty.mkdir()
        with patch.object(hive, '_git_out', return_value=None):
            with patch('hive.Path.cwd', return_value=empty):
                with patch.object(hive, '_APIARY_CONFIG',
                                  tmp_path / 'nope.json'):
                    with pytest.raises(SystemExit):
                        hive.cmd_issues(args)

    def test_single_hive_mode(self, fake_hive, capsys):
        """When inside a hive, operates on that hive only."""
        args = hive.argparse.Namespace(apiary=False, color=False)
        with patch.object(hive, '_find_hive_root', return_value=fake_hive):
            with patch.object(hive, '_issues_display') as mock_display:
                hive.cmd_issues(args)
        mock_display.assert_called_once_with([fake_hive])


# --- _build_resolve_prompt tests ---------------------------------------------


class TestBuildResolvePrompt:
    def test_includes_branch_names(self):
        prompt = hive._build_resolve_prompt('feat/x', 'main')
        assert 'feat/x' in prompt
        assert 'main' in prompt

    def test_includes_safety_rules(self):
        prompt = hive._build_resolve_prompt('feat/x', 'main')
        assert 'NEVER delete' in prompt
        assert 'NEVER git push' in prompt

    def test_includes_all_outcome_types(self):
        prompt = hive._build_resolve_prompt('feat/x', 'main')
        assert 'OUTCOME:merged' in prompt
        assert 'OUTCOME:rebased' in prompt
        assert 'OUTCOME:skipped' in prompt

    def test_uses_actual_default_branch(self):
        """Prompt references the repo's actual default, not hardcoded 'main'."""
        prompt = hive._build_resolve_prompt('fix/bug', 'infra-dev')
        assert 'git checkout infra-dev' in prompt
        assert 'origin/infra-dev' in prompt

    def test_includes_analysis_steps(self):
        prompt = hive._build_resolve_prompt('feat/x', 'main')
        assert 'git fetch origin' in prompt
        assert 'git diff' in prompt
        assert 'merge-base' in prompt


# --- _resolve_branch tests ---------------------------------------------------


class TestDetectPostRunState:
    """Tests for _detect_post_run_state (SHA-based before/after comparison)."""

    def test_detects_checkout_to_default(self):
        """Branch switched to default → 'merged'."""
        with patch.object(hive, '_git_out', return_value='main'):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', 'aaa111')
        assert result == 'merged'

    def test_same_branch_same_sha(self):
        """Still on original branch, SHA unchanged → None (no mutation)."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse' and args[1] == '--abbrev-ref':
                return 'feat/x'
            if args == ['rev-parse', 'HEAD']:
                return 'aaa111'
            return ''

        with patch.object(hive, '_git_out',
                          side_effect=git_out_side_effect):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', 'aaa111')
        assert result is None

    def test_same_branch_different_sha(self):
        """Still on original branch, SHA changed → 'rebased'."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse' and args[1] == '--abbrev-ref':
                return 'feat/x'
            if args == ['rev-parse', 'HEAD']:
                return 'bbb222'
            return ''

        with patch.object(hive, '_git_out',
                          side_effect=git_out_side_effect):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', 'aaa111')
        assert result == 'rebased'

    def test_already_on_default_same_sha(self):
        """If original == default == current and SHA unchanged → None."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse' and args[1] == '--abbrev-ref':
                return 'main'
            if args == ['rev-parse', 'HEAD']:
                return 'aaa111'
            return ''

        with patch.object(hive, '_git_out',
                          side_effect=git_out_side_effect):
            result = hive._detect_post_run_state(
                Path('/repo'), 'main', 'main', 'aaa111')
        assert result is None

    def test_no_pre_sha_skips_rebase_check(self):
        """If pre_sha is None, cannot detect rebase → None."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse' and args[1] == '--abbrev-ref':
                return 'feat/x'
            return ''

        with patch.object(hive, '_git_out',
                          side_effect=git_out_side_effect):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', None)
        assert result is None

    def test_switched_to_non_default_branch(self):
        """Switched to some other branch (not default) → None."""
        with patch.object(hive, '_git_out', return_value='other-branch'):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', 'aaa111')
        assert result is None

    def test_historical_rebase_not_detected(self):
        """SHA unchanged means old rebase is NOT reported as new mutation."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse' and args[1] == '--abbrev-ref':
                return 'feat/x'
            if args == ['rev-parse', 'HEAD']:
                return 'aaa111'  # same as pre_sha
            return ''

        with patch.object(hive, '_git_out',
                          side_effect=git_out_side_effect):
            result = hive._detect_post_run_state(
                Path('/repo'), 'feat/x', 'main', 'aaa111')
        assert result is None


class TestResolveBranch:
    """Tests for OUTCOME parsing and Claude invocation.

    _detect_post_run_state is mocked to None (autouse) so these tests
    focus on the subprocess call and OUTCOME-line parsing in isolation.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self):
        """Isolate OUTCOME parsing from pre-snapshot and post-run detection."""
        with patch.object(hive, '_detect_post_run_state', return_value=None):
            with patch.object(hive, '_git_out', return_value='fake-sha'):
                yield

    @staticmethod
    def _claude_result(stdout, rc=0, stderr=''):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def test_parses_merged_outcome(self):
        output = 'Analysis...\nOUTCOME:merged:squash merge found in main'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'
        assert 'squash merge found' in result['detail']

    def test_parses_rebased_outcome(self):
        output = 'Analysis...\nOUTCOME:rebased:branch has unique work'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'rebased'
        assert 'unique work' in result['detail']

    def test_parses_rebase_failed_outcome(self):
        output = 'Analysis...\nOUTCOME:rebase-failed:conflicts in file.py'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'rebase-failed'
        assert 'conflicts' in result['detail']

    def test_parses_skipped_outcome(self):
        output = 'Analysis...\nOUTCOME:skipped:uncertain about merge status'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'skipped'

    def test_outcome_found_from_end_of_output(self):
        """OUTCOME line buried after verbose analysis is still found."""
        output = 'line1\nline2\nline3\nOUTCOME:merged:done\n'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'

    def test_outcome_detail_is_optional(self):
        """OUTCOME with no detail part still parses."""
        output = 'OUTCOME:merged'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'
        assert result['detail'] == ''

    def test_no_outcome_returns_skipped(self):
        output = 'Claude did not produce an outcome line'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'skipped'
        assert 'no OUTCOME' in result['detail']

    def test_empty_output_returns_skipped(self):
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('')):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'skipped'

    def test_invalid_outcome_value_ignored(self):
        """Unknown outcome values are ignored, falls through to skipped."""
        output = 'OUTCOME:invalid:bad value'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'skipped'

    def test_claude_not_found_returns_error(self):
        with patch('hive.subprocess.run', side_effect=FileNotFoundError):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'error'
        assert 'not found' in result['detail']

    def test_timeout_returns_error(self):
        with patch('hive.subprocess.run',
                   side_effect=subprocess.TimeoutExpired([], 180)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'error'
        assert 'timed out' in result['detail']

    def test_nonzero_exit_returns_error_with_stderr(self):
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('', rc=1, stderr='API error')):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'error'
        assert 'API error' in result['detail']

    def test_nonzero_exit_empty_stderr_shows_exit_code(self):
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('', rc=1)):
            result = hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'error'
        assert 'exit code 1' in result['detail']

    @staticmethod
    def _find_claude_call(mock_run):
        """Find the claude subprocess call among all subprocess.run calls."""
        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get('args', [])
            if args and args[0] == 'claude':
                return c
        raise AssertionError('No claude subprocess call found')

    def test_passes_prompt_via_stdin(self):
        """Prompt is passed via stdin (input kwarg), not as a CLI arg."""
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('OUTCOME:skipped:test')) as mock_run:
            hive._resolve_branch(Path('/my/repo'), 'feat/x', 'main')
        _, kwargs = self._find_claude_call(mock_run)
        assert 'input' in kwargs
        assert 'feat/x' in kwargs['input']
        assert 'main' in kwargs['input']

    def test_passes_correct_cwd_and_timeout(self):
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('OUTCOME:skipped:test')) as mock_run:
            hive._resolve_branch(Path('/my/repo'), 'feat/x', 'main')
        _, kwargs = self._find_claude_call(mock_run)
        assert kwargs['cwd'] == Path('/my/repo')
        assert kwargs['timeout'] == hive._RESOLVE_TIMEOUT

    def test_invokes_claude_with_opus_model(self):
        with patch('hive.subprocess.run',
                   return_value=self._claude_result('OUTCOME:skipped:test')) as mock_run:
            hive._resolve_branch(Path('/repo'), 'feat/x', 'main')
        cmd = self._find_claude_call(mock_run)[0][0]
        assert cmd[0] == 'claude'
        assert '-p' in cmd
        # Model should be opus
        model_idx = cmd.index('--model')
        assert cmd[model_idx + 1] == 'opus'


# --- _resolve_branch reconciliation tests ------------------------------------


class TestResolveBranchReconciliation:
    """Tests for reconciliation between claimed OUTCOME and observed state."""

    @staticmethod
    def _claude_result(stdout, rc=0, stderr=''):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def test_claimed_skipped_but_observed_merged(self):
        """Claude says skipped, but repo actually switched to default."""
        output = 'OUTCOME:skipped:not sure'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='merged'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'
        assert 'observed: merged' in result['detail']

    def test_claimed_skipped_but_observed_rebased(self):
        """Claude says skipped, but reflog shows rebase."""
        output = 'OUTCOME:skipped:uncertain'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='rebased'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'rebased'
        assert 'observed: rebased' in result['detail']

    def test_claimed_merged_observation_agrees(self):
        """Claude says merged and observation agrees — claimed wins."""
        output = 'OUTCOME:merged:squash merge in log'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='merged'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'
        assert result['detail'] == 'squash merge in log'

    def test_claimed_merged_no_observation(self):
        """Claude says merged, no detection needed."""
        output = 'OUTCOME:merged:done'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value=None):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'

    def test_no_outcome_but_observed_merged(self):
        """No OUTCOME marker, but repo actually switched to default."""
        output = 'Analysis complete, checkout done'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='merged'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'merged'
        assert 'no OUTCOME marker' in result['detail']
        assert 'observed: merged' in result['detail']

    def test_no_outcome_but_observed_rebased(self):
        """No OUTCOME marker, but reflog shows rebase."""
        output = 'Rebased successfully'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='rebased'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'rebased'
        assert 'observed: rebased' in result['detail']

    def test_no_outcome_no_observation(self):
        """No OUTCOME, no detected change → skipped."""
        output = 'Could not determine'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value=None):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        assert result['outcome'] == 'skipped'
        assert 'no OUTCOME' in result['detail']

    def test_claimed_rebased_not_overridden(self):
        """Non-skipped claimed outcomes are not overridden by observation."""
        output = 'OUTCOME:rebased:branch has unique work'
        with patch('hive.subprocess.run',
                   return_value=self._claude_result(output)):
            with patch.object(hive, '_detect_post_run_state',
                              return_value='merged'):
                result = hive._resolve_branch(
                    Path('/repo'), 'feat/x', 'main')
        # claimed 'rebased' is not 'skipped', so observation doesn't override
        assert result['outcome'] == 'rebased'
        assert result['detail'] == 'branch has unique work'


# --- _resolve_branches_for_hive tests ----------------------------------------


class TestResolveBranchesForHive:
    def test_all_on_default_branch(self, fake_hive, capsys):
        """No candidates when all repos are on default branch."""
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out', return_value='main'):
                hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'nothing to resolve' in out

    def test_empty_hive(self, tmp_path, capsys):
        """Empty hive produces no output."""
        empty = tmp_path / 'empty'
        empty.mkdir()
        hive._resolve_branches_for_hive(empty)
        out = capsys.readouterr().out
        assert out == ''

    def test_skips_dirty_repos(self, fake_hive, capsys):
        """Dirty repos on non-default branches are excluded from candidates."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                return 'feat/x'
            if args[0] == 'status':
                return 'M dirty.py'
            return ''

        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'nothing to resolve' in out

    def test_skips_detached_head(self, fake_hive, capsys):
        """Repos in detached HEAD state are excluded from candidates."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                return 'HEAD'
            if args[0] == 'status':
                return ''
            return ''

        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'nothing to resolve' in out

    def test_claude_not_available_shows_error(self, fake_hive, capsys):
        """When claude CLI is missing, shows error and returns."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                return 'feat/x'
            if args[0] == 'status':
                return ''
            return ''

        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run',
                           side_effect=FileNotFoundError):
                    hive._resolve_branches_for_hive(fake_hive)
        err = capsys.readouterr().err
        assert 'claude CLI not found' in err

    def test_calls_resolve_for_non_default_branches(self, fake_hive, capsys):
        """Repos on non-default branches are passed to _resolve_branch."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'repo-1':
                    return 'feat/x'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        resolve_result = {'outcome': 'merged', 'detail': 'squash merged'}
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      return_value=resolve_result) as mock_resolve:
                        hive._resolve_branches_for_hive(fake_hive)
        mock_resolve.assert_called_once()
        call_args = mock_resolve.call_args[0]
        assert call_args[0] == fake_hive / 'repo-1'
        assert call_args[1] == 'feat/x'
        assert call_args[2] == 'main'

    def test_shows_merged_output(self, fake_hive, capsys):
        """Merged outcome displays arrow notation."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'repo-1':
                    return 'feat/x'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        resolve_result = {'outcome': 'merged', 'detail': 'squash merged'}
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      return_value=resolve_result):
                        hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'feat/x' in out
        assert 'main' in out
        assert 'squash merged' in out

    def test_shows_rebased_output(self, fake_hive, capsys):
        """Rebased outcome displays rebase message."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'repo-1':
                    return 'feat/x'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        resolve_result = {'outcome': 'rebased', 'detail': 'unique work'}
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      return_value=resolve_result):
                        hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'rebased onto main' in out

    def test_shows_error_output(self, fake_hive, capsys):
        """Error outcomes display the error detail."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'repo-1':
                    return 'feat/x'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        resolve_result = {'outcome': 'error', 'detail': 'timed out'}
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      return_value=resolve_result):
                        hive._resolve_branches_for_hive(fake_hive)
        out = capsys.readouterr().out
        assert 'error' in out
        assert 'timed out' in out

    def test_multiple_repos_resolved(self, fake_hive, capsys):
        """Both repos on non-default branches are resolved."""
        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'repo-1':
                    return 'feat/a'
                if cwd and cwd.name == 'repo-2':
                    return 'feat/b'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        results = iter([
            {'outcome': 'merged', 'detail': 'done'},
            {'outcome': 'rebased', 'detail': 'active'},
        ])
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      side_effect=lambda *a: next(results)
                                      ) as mock_resolve:
                        hive._resolve_branches_for_hive(fake_hive)
        assert mock_resolve.call_count == 2
        out = capsys.readouterr().out
        assert 'Resolving 2 non-default branches' in out

    def test_includes_nested_repos(self, fake_hive, capsys):
        """Nested repos in .local/ are also checked."""
        # Create a nested repo inside repo-1/.local/
        local_dir = fake_hive / 'repo-1' / '.local'
        local_dir.mkdir()
        nested = local_dir / 'nested-repo'
        nested.mkdir()
        (nested / '.git').mkdir()

        def git_out_side_effect(args, cwd=None):
            if args[0] == 'rev-parse':
                if cwd and cwd.name == 'nested-repo':
                    return 'fix/nested'
                return 'main'
            if args[0] == 'status':
                return ''
            return ''

        resolve_result = {'outcome': 'rebased', 'detail': 'rebased'}
        claude_ok = subprocess.CompletedProcess(
            [], 0, stdout='1.0.0', stderr='')
        with patch.object(hive, '_default_branch', return_value='main'):
            with patch.object(hive, '_git_out',
                              side_effect=git_out_side_effect):
                with patch('hive.subprocess.run', return_value=claude_ok):
                    with patch.object(hive, '_resolve_branch',
                                      return_value=resolve_result) as mock_resolve:
                        hive._resolve_branches_for_hive(fake_hive)
        mock_resolve.assert_called_once()
        call_args = mock_resolve.call_args[0]
        assert call_args[0] == nested
        assert call_args[1] == 'fix/nested'


# --- cmd_pull --resolve-branches tests ---------------------------------------


class TestCmdPullResolveBranches:
    def test_flag_passed_to_pull_single_hive(self, fake_hive, capsys):
        """--resolve-branches flag is forwarded to _pull_single_hive."""
        args = hive.argparse.Namespace(
            compact=False, apiary=False, push=False, quiet=False,
            resolve_branches=True, color=False)
        with patch.object(hive, '_find_hive_root', return_value=fake_hive):
            with patch.object(hive, '_pull_single_hive') as mock_pull:
                hive.cmd_pull(args)
        mock_pull.assert_called_once_with(
            fake_hive, False, False, True, pull_cache=ANY, quiet=False)

    def test_flag_passed_in_apiary_mode(self, fake_apiary, capsys):
        """--resolve-branches is forwarded in apiary mode."""
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            compact=False, apiary=True, push=False, quiet=False,
            resolve_branches=True, color=False)
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with patch.object(hive, '_pull_single_hive') as mock_pull:
                hive.cmd_pull(args)
        # Called once per hive
        assert mock_pull.call_count == len(hive_roots)
        for c in mock_pull.call_args_list:
            assert c[0][3] is True  # resolve_branches=True
            assert 'pull_cache' in c[1]  # pull_cache passed as kwarg

    def test_disabled_by_default(self, fake_hive, capsys):
        """Without the flag, resolve_branches is False."""
        args = hive.argparse.Namespace(
            compact=False, apiary=False, push=False, quiet=False,
            resolve_branches=False, color=False)
        with patch.object(hive, '_find_hive_root', return_value=fake_hive):
            with patch.object(hive, '_pull_single_hive') as mock_pull:
                hive.cmd_pull(args)
        mock_pull.assert_called_once_with(
            fake_hive, False, False, False, pull_cache=ANY, quiet=False)


class TestPullQuiet:
    def test_is_notable_for_off_default_branch(self):
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='feature/x',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.NONE,
        )
        assert hive._is_notable(result, 'main') is True

    def test_is_notable_false_for_clean_default_branch(self):
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='main',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.NONE,
        )
        assert hive._is_notable(result, 'main') is False

    def test_pull_single_hive_quiet_filters_clean_repos(self, fake_hive, capsys):
        results = iter([
            hive.RepoStatus(
                path=fake_hive / 'repo-1',
                branch='main',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.NONE,
                up_to_date=True,
            ),
            hive.RepoStatus(
                path=fake_hive / 'repo-2',
                branch='feature/x',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.PULL,
                pulled=True,
            ),
        ])

        with patch.object(hive, 'analyze_repo', side_effect=lambda *args, **kwargs: next(results)):
            with patch.object(hive, 'execute_sync', side_effect=lambda status, **kwargs: status):
                with patch.object(hive, '_default_branch', return_value='main'):
                    summary = hive._pull_single_hive(
                        fake_hive, compact=True, push=False, quiet=True)

        out = capsys.readouterr().out
        assert 'repo-1' not in out
        assert 'repo-2' in out
        assert '1 repo clean / up to date' in out
        assert summary == {
            'repo_count': 2,
            'clean_count': 1,
            'all_clean': False,
            'lines': summary['lines'],
        }

    def test_pull_single_hive_quiet_calls_resolve_branches(self, fake_hive):
        results = iter([
            hive.RepoStatus(
                path=fake_hive / 'repo-1',
                branch='main',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.NONE,
                up_to_date=True,
            ),
            hive.RepoStatus(
                path=fake_hive / 'repo-2',
                branch='main',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.NONE,
                up_to_date=True,
            ),
        ])

        with patch.object(hive, 'analyze_repo', side_effect=lambda *args, **kwargs: next(results)):
            with patch.object(hive, 'execute_sync', side_effect=lambda status, **kwargs: status):
                with patch.object(hive, '_default_branch', return_value='main'):
                    with patch.object(hive, '_resolve_branches_for_hive') as mock_resolve:
                        hive._pull_single_hive(
                            fake_hive,
                            compact=True,
                            push=False,
                            resolve_branches=True,
                            quiet=True,
                        )

        mock_resolve.assert_called_once_with(fake_hive)

    def test_cmd_pull_quiet_implies_compact(self, fake_hive):
        args = hive.argparse.Namespace(
            compact=False, apiary=False, push=False, quiet=True,
            resolve_branches=False, color=False)
        with patch.object(hive, '_find_hive_root', return_value=fake_hive):
            with patch.object(hive, '_pull_single_hive') as mock_pull:
                hive.cmd_pull(args)
        mock_pull.assert_called_once_with(
            fake_hive, True, False, False, pull_cache=ANY, quiet=True)

    def test_cmd_pull_apiary_quiet_collapses_all_clean_hive(self, fake_apiary, capsys):
        config_file, hive_roots = fake_apiary
        args = hive.argparse.Namespace(
            compact=False, apiary=True, push=False, quiet=True,
            resolve_branches=False, color=False)
        summaries = [
            {'repo_count': 2, 'clean_count': 2, 'all_clean': True, 'lines': []},
            {'repo_count': 1, 'clean_count': 0, 'all_clean': False,
             'lines': ['  proj-1  ✗ feature/x — rebase failed']},
        ]
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with patch.object(hive, '_pull_single_hive', side_effect=summaries):
                hive.cmd_pull(args)

        out = capsys.readouterr().out
        assert f'━━ {hive._display_path(hive_roots[0])} ━━  (all 2 repos clean)' in out
        assert f'━━ {hive._display_path(hive_roots[1])} ━━' in out
        assert 'proj-1  ✗ feature/x — rebase failed' in out


# --- Pull cache tests --------------------------------------------------------


class TestPullCache:
    def test_normalize_origin_url(self):
        """URL normalization strips trailing .git and /."""
        assert hive._normalize_origin_url(
            'ssh://git@host/org/repo.git') == 'ssh://git@host/org/repo'
        assert hive._normalize_origin_url(
            'ssh://git@host/org/repo/') == 'ssh://git@host/org/repo'
        assert hive._normalize_origin_url(
            'ssh://git@host/org/repo.git/') == 'ssh://git@host/org/repo'
        assert hive._normalize_origin_url(
            'ssh://git@host/org/repo') == 'ssh://git@host/org/repo'

    def test_normalize_strips_https_userinfo(self):
        """HTTPS URLs with user@ are normalized to match without."""
        assert hive._normalize_origin_url(
            'https://user@host/org/repo.git') == 'https://host/org/repo'
        assert hive._normalize_origin_url(
            'https://host/org/repo.git') == 'https://host/org/repo'
        assert (hive._normalize_origin_url('https://user@host/org/repo')
                == hive._normalize_origin_url('https://host/org/repo'))


class TestRemoteCache:
    def test_get_set_remote_sha(self):
        cache = hive.RemoteCache()
        assert cache.get_remote_sha('ssh://git@host/org/repo', 'main') is None
        cache.set_remote_sha('ssh://git@host/org/repo', 'main', 'abc123')
        assert cache.get_remote_sha('ssh://git@host/org/repo', 'main') == 'abc123'

    def test_get_set_synced_path(self, tmp_path):
        cache = hive.RemoteCache()
        assert cache.get_synced_path('ssh://git@host/org/repo', 'main') is None
        cache.set_synced_path('ssh://git@host/org/repo', 'main', tmp_path)
        assert cache.get_synced_path('ssh://git@host/org/repo', 'main') == tmp_path


class TestPullEngine:
    @staticmethod
    def _pull(repo, cache=None, push=False):
        status = hive.analyze_repo(repo, hive._ORIGIN_REMOTE, remote_cache=cache)
        return hive.execute_sync(status, remote_cache=cache, push=push)

    def test_cache_hit_skips_pull(self, tmp_path):
        """When HEAD matches cached SHA for same origin+branch, pull is skipped."""
        repo = tmp_path / 'repo'
        repo.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        head_sha = 'deadbeef1234'
        cache = hive.RemoteCache()
        cache.set_remote_sha(origin_url, 'main', head_sha)

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return origin_url + '.git'
            if args == ['rev-parse', 'HEAD']:
                return head_sha
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git') as mock_git:
                result = self._pull(repo, cache=cache)

        assert result.cached is True
        assert result.up_to_date is True
        assert result.pulled is False
        mock_git.assert_not_called()

    def test_cache_miss_pulls_and_updates_cache(self, tmp_path):
        """When HEAD doesn't match cache, pull proceeds and cache is updated."""
        repo = tmp_path / 'repo'
        repo.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        old_sha = 'oldsha111'
        new_sha = 'newsha222'
        cache = hive.RemoteCache()
        cache.set_remote_sha(origin_url, 'main', old_sha)

        call_count = {'rev_parse_head': 0}

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return origin_url
            if args == ['rev-parse', 'HEAD']:
                call_count['rev_parse_head'] += 1
                if call_count['rev_parse_head'] == 1:
                    return 'differentsha'
                return new_sha
            if args == ['status', '--porcelain']:
                return ''
            return None

        fake_pull = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='Already up to date.', stderr='')

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git', return_value=fake_pull) as mock_git:
                result = self._pull(repo, cache=cache)

        assert result.cached is False
        assert result.up_to_date is True
        assert cache.get_remote_sha(origin_url, 'main') == new_sha
        mock_git.assert_called_once()

    def test_no_cache_skips_cache_logic(self, tmp_path):
        """When pull_cache is None, no origin URL lookup happens."""
        repo = tmp_path / 'repo'
        repo.mkdir()

        git_out_calls = []

        def fake_git_out(args, cwd=None):
            git_out_calls.append(args)
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['status', '--porcelain']:
                return ''
            return None

        fake_pull = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='Already up to date.', stderr='')

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git', return_value=fake_pull):
                result = self._pull(repo, cache=None)

        assert result.cached is False
        assert result.up_to_date is True
        origin_calls = [c for c in git_out_calls
                        if c == ['config', '--get', 'remote.origin.url']]
        assert origin_calls == []

    def test_cache_shared_across_repos(self, tmp_path):
        """Two clones of same origin+branch: first pulls, second hits cache."""
        repo1 = tmp_path / 'repo1'
        repo2 = tmp_path / 'repo2'
        repo1.mkdir()
        repo2.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        head_sha = 'abc123'
        cache = hive.RemoteCache()

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return origin_url
            if args == ['rev-parse', 'HEAD']:
                return head_sha
            if args == ['status', '--porcelain']:
                return ''
            return None

        fake_pull = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='Already up to date.', stderr='')

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git', return_value=fake_pull) as mock_git:
                r1 = self._pull(repo1, cache=cache)

        assert r1.cached is False
        assert cache.get_remote_sha(origin_url, 'main') == head_sha
        mock_git.assert_called_once()

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git') as mock_git2:
                r2 = self._pull(repo2, cache=cache)

        assert r2.cached is True
        assert r2.up_to_date is True
        mock_git2.assert_not_called()

    def test_pull_failure_does_not_update_cache(self, tmp_path):
        """A failed pull must not write to the cache."""
        repo = tmp_path / 'repo'
        repo.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        cache = hive.RemoteCache()

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return origin_url
            if args == ['rev-parse', 'HEAD']:
                return 'currentsha'
            if args == ['status', '--porcelain']:
                return ''
            return None

        failed_pull = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr='conflict')

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git', return_value=failed_pull):
                result = self._pull(repo, cache=cache)

        assert result.pull_failed is True
        assert cache.get_remote_sha(origin_url, 'main') is None

    def test_no_origin_remote_still_pulls(self, tmp_path):
        """Repo with no origin remote pulls normally when cache is provided."""
        repo = tmp_path / 'repo'
        repo.mkdir()
        cache = hive.RemoteCache()

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return None
            if args == ['status', '--porcelain']:
                return ''
            return None

        fake_pull = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='Already up to date.', stderr='')

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git', return_value=fake_pull) as mock_git:
                result = self._pull(repo, cache=cache)

        assert result.cached is False
        assert result.up_to_date is True
        mock_git.assert_called_once()
        assert cache.remote_shas == {}

    def test_dirty_repo_with_cache_hit_returns_skipped(self, tmp_path):
        """A dirty repo is always reported as dirty, even with a cache hit."""
        repo = tmp_path / 'repo'
        repo.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        head_sha = 'abc123'
        cache = hive.RemoteCache()
        cache.set_remote_sha(origin_url, 'main', head_sha)

        def fake_git_out(args, cwd=None):
            if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                return 'main'
            if args == ['config', '--get', 'remote.origin.url']:
                return origin_url
            if args == ['status', '--porcelain']:
                return 'M dirty-file.txt'
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git') as mock_git:
                result = self._pull(repo, cache=cache)

        assert result.cached is False
        assert result.skipped is True
        assert result.dirty_count == 1
        mock_git.assert_not_called()

    def test_different_branches_same_origin_no_collision(self, tmp_path):
        """Two clones of same origin on different branches must not collide."""
        repo_main = tmp_path / 'repo-main'
        repo_feat = tmp_path / 'repo-feat'
        repo_main.mkdir()
        repo_feat.mkdir()
        origin_url = 'ssh://git@host/org/repo'
        head_sha = 'same_sha_on_both'
        cache = hive.RemoteCache()

        def make_fake_git_out(branch):
            def fake_git_out(args, cwd=None):
                if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
                    return branch
                if args == ['config', '--get', 'remote.origin.url']:
                    return origin_url
                if args == ['rev-parse', 'HEAD']:
                    return head_sha
                if args == ['status', '--porcelain']:
                    return ''
                return None
            return fake_git_out

        fake_pull = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='Already up to date.', stderr='')

        with patch.object(hive, '_git_out', side_effect=make_fake_git_out('main')):
            with patch.object(hive, '_git', return_value=fake_pull):
                r1 = self._pull(repo_main, cache=cache)

        assert r1.cached is False
        assert cache.get_remote_sha(origin_url, 'main') == head_sha

        with patch.object(hive, '_git_out', side_effect=make_fake_git_out('feature')):
            with patch.object(hive, '_git', return_value=fake_pull) as mock_git:
                r2 = self._pull(repo_feat, cache=cache)

        assert r2.cached is False
        mock_git.assert_called_once()
        assert cache.get_remote_sha(origin_url, 'feature') == head_sha


class TestFormatPullSegmentCached:
    def test_cached_label(self):
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='main',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.NONE,
            up_to_date=True,
            cached=True,
        )
        seg = hive._format_pull_segment(result)
        assert 'cached' in seg
        assert 'up to date' in seg

    def test_not_cached_label(self):
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='main',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.NONE,
            up_to_date=True,
            cached=False,
        )
        seg = hive._format_pull_segment(result)
        assert 'cached' not in seg
