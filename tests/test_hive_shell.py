"""Tests for hive.py shell and apiary helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hive  # noqa: E402


@pytest.fixture
def fake_hive(tmp_path):
    hive_root = tmp_path / "hive"
    hive_root.mkdir()
    for name in ["repo-1", "repo-2", "repo-3"]:
        repo = hive_root / name
        repo.mkdir()
        (repo / ".git").mkdir()
    return hive_root


@pytest.fixture
def fake_apiary(tmp_path, fake_hive):
    hive2 = tmp_path / "hive2"
    hive2.mkdir()
    (hive2 / "proj-1").mkdir()
    (hive2 / "proj-1" / ".git").mkdir()

    config_dir = tmp_path / "config" / "hive"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "apiary.json"
    config_file.write_text(json.dumps({"hives": [str(fake_hive), str(hive2)]}))
    return config_file, [fake_hive, hive2]


@pytest.fixture
def dtach_dir(tmp_path):
    directory = tmp_path / "hive-dtach"
    directory.mkdir()
    return directory


def test_workspace_number():
    assert hive._workspace_number("repo-3") == "3"
    assert hive._workspace_number("home-dc-5") == "5"
    assert hive._workspace_number("repo") is None


def test_zdotdir_defaults_to_home(tmp_path):
    """When no ZDOTDIR is set, _launch_dtach writes $HOME as HIVE_REAL_ZDOTDIR."""
    home = tmp_path / "home"
    home.mkdir()
    hive_root = tmp_path / "hive"
    hive_root.mkdir()
    workspace = hive_root / "repo-1"
    workspace.mkdir()
    dtach_dir = tmp_path / "hive-dtach"
    dtach_dir.mkdir()

    (home / "bin").mkdir()
    (home / "bin" / "hive-shell-prompt.zsh").write_text(":\n")

    captured: dict[str, object] = {}

    def capture_execvpe(file, args, env):
        captured["env"] = env
        raise RuntimeError("stop")

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", return_value=False), \
         patch.object(hive, "_write_sidecar"), \
         patch.object(hive, "_hive_color", return_value={"name": "blue", "rgb": "97;150;255", "c256": "75"}), \
         patch.object(hive.Path, "home", return_value=home), \
         patch("os.chdir"), \
         patch("os.execvpe", side_effect=capture_execvpe), \
         patch.dict(os.environ, {"HOME": str(home)}, clear=False):
        # Remove ZDOTDIR so the default (home) is used
        os.environ.pop("ZDOTDIR", None)
        with pytest.raises(RuntimeError, match="stop"):
            hive._launch_dtach(hive_root, "hive", workspace, "1")

    zdotdir = Path(captured["env"]["ZDOTDIR"])
    zshenv = (zdotdir / ".zshenv").read_text()
    assert f'HIVE_REAL_ZDOTDIR="{home}"' in zshenv


def test_zdotdir_resolves_nested_hive(tmp_path):
    """When ZDOTDIR points to a hive wrapper dir, use HIVE_REAL_ZDOTDIR."""
    home = tmp_path / "home"
    home.mkdir()
    original = tmp_path / "real-zsh"
    original.mkdir()
    hive_root = tmp_path / "hive"
    hive_root.mkdir()
    workspace = hive_root / "repo-1"
    workspace.mkdir()
    dtach_dir = tmp_path / "hive-dtach"
    dtach_dir.mkdir()

    (home / "bin").mkdir()
    (home / "bin" / "hive-shell-prompt.zsh").write_text(":\n")

    # Simulate being inside an existing hive shell
    wrapper_zdotdir = str(dtach_dir / ".zdotdir-outer-1")

    captured: dict[str, object] = {}

    def capture_execvpe(file, args, env):
        captured["env"] = env
        raise RuntimeError("stop")

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", return_value=False), \
         patch.object(hive, "_write_sidecar"), \
         patch.object(hive, "_hive_color", return_value={"name": "blue", "rgb": "97;150;255", "c256": "75"}), \
         patch.object(hive.Path, "home", return_value=home), \
         patch("os.chdir"), \
         patch("os.execvpe", side_effect=capture_execvpe), \
         patch.dict(os.environ, {
             "HOME": str(home),
             "ZDOTDIR": wrapper_zdotdir,
             "HIVE_REAL_ZDOTDIR": str(original),
         }, clear=False):
        with pytest.raises(RuntimeError, match="stop"):
            hive._launch_dtach(hive_root, "hive", workspace, "1")

    zdotdir = Path(captured["env"]["ZDOTDIR"])
    zshenv = (zdotdir / ".zshenv").read_text()
    assert f'HIVE_REAL_ZDOTDIR="{original}"' in zshenv


def test_socket_and_sidecar_paths():
    with patch.object(hive, "_DTACH_DIR", Path("/tmp/hive-dtach")):
        assert hive._socket_path("infra", "3") == Path("/tmp/hive-dtach/infra-3.sock")
        assert hive._sidecar_path("infra", "3") == Path("/tmp/hive-dtach/infra-3.json")


def test_socket_alive_false_when_missing(dtach_dir):
    assert hive._socket_alive(dtach_dir / "missing.sock") is False


def test_socket_alive_true_when_dtach_probe_succeeds(dtach_dir):
    sock = dtach_dir / "test.sock"
    sock.touch()
    with patch.object(hive.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert hive._socket_alive(sock) is True
        assert mock_run.call_args[0][0] == ["dtach", "-p", str(sock)]
        assert mock_run.call_args[1]["stdin"] == subprocess.DEVNULL


def test_hive_color_uses_apiary_order(fake_hive, fake_apiary):
    config_file, _ = fake_apiary
    with patch.object(hive, "_APIARY_CONFIG", config_file):
        color = hive._hive_color(fake_hive)
    assert color["name"] == "blue"
    assert color["c256"] == "75"


def test_find_workspace_for_reattach(fake_hive, dtach_dir):
    workspace = fake_hive / "repo-2"

    def alive(sock):
        return sock == hive._socket_path("hive", "2")

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", side_effect=alive), \
         patch.object(Path, "cwd", return_value=workspace):
        result = hive._find_workspace_for_reattach(fake_hive, "hive")

    assert result == (workspace, "2")


def test_find_clean_workspace_skips_dirty_and_busy(fake_hive, dtach_dir):
    dirty = {fake_hive / "repo-1"}

    def alive(sock):
        return sock == hive._socket_path("hive", "2")

    def git_out(args, cwd=None):
        cwd = Path(cwd)
        if args[0] == "rev-parse":
            return "main"
        if args[0] == "status":
            return "M file.txt" if cwd in dirty else ""
        return None

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", side_effect=alive), \
         patch.object(hive, "_default_branch", return_value="main"), \
         patch.object(hive, "_git_out", side_effect=git_out):
        result = hive._find_clean_workspace(fake_hive, "hive")

    assert result == (fake_hive / "repo-3", "3")


def test_write_sidecar(fake_hive, dtach_dir):
    workspace = fake_hive / "repo-1"
    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_git_out", return_value="main"):
        hive._write_sidecar("hive", "1", workspace)

    data = json.loads((dtach_dir / "hive-1.json").read_text())
    assert data["hive"] == "hive"
    assert data["workspace"] == str(workspace)
    assert data["number"] == 1
    assert data["branch_at_checkout"] == "main"


def test_launch_dtach_sets_env_and_args(fake_hive, dtach_dir):
    workspace = fake_hive / "repo-1"
    captured: dict[str, object] = {}

    def capture_execvpe(file, args, env):
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", return_value=False), \
         patch.object(hive, "_write_sidecar"), \
         patch.object(hive, "_hive_color", return_value={"name": "blue", "rgb": "97;150;255", "c256": "75"}), \
         patch("os.chdir"), \
         patch("os.execvpe", side_effect=capture_execvpe), \
         patch.dict(os.environ, {}, clear=False):
        try:
            hive._launch_dtach(fake_hive, "hive", workspace, "1")
        except TypeError:
            pass

    assert captured["file"] == "dtach"
    args = captured["args"]
    assert args[0] == "dtach"
    assert "-A" in args
    assert "-Ez" in args
    assert "zsh" in args
    env = captured["env"]
    assert env["HIVE_ROOT"] == str(fake_hive)
    assert env["HIVE_NAME"] == "hive"
    assert env["HIVE_WORKSPACE"] == "repo-1"
    assert env["HIVE_NUMBER"] == "1"


def test_shell_list_and_cleanup(dtach_dir, fake_hive, capsys):
    sock = dtach_dir / "hive-1.sock"
    sock.touch()
    sidecar = dtach_dir / "hive-1.json"
    sidecar.write_text(json.dumps({"workspace": str(fake_hive / "repo-1")}))

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", return_value=False):
        hive._shell_list()
        list_out = capsys.readouterr().out
        assert "(dead)" in list_out

        hive._shell_cleanup()
        cleanup_out = capsys.readouterr().out
        assert "Removed dead session: hive-1" in cleanup_out

    assert not sock.exists()
    assert not sidecar.exists()


def test_generated_zsh_wrapper_sources_home_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    hive_root = tmp_path / "hive"
    hive_root.mkdir()
    workspace = hive_root / "repo-1"
    workspace.mkdir()
    dtach_dir = tmp_path / "hive-dtach"
    dtach_dir.mkdir()

    (home / ".zshenv").write_text("export FROM_REAL_ZSHENV=1\n")
    (home / ".zshrc").write_text("export FROM_REAL_ZSHRC=1\n")
    (home / "bin").mkdir()
    (home / "bin" / "hive-shell-prompt.zsh").write_text(":\n")

    captured: dict[str, object] = {}

    def capture_execvpe(file, args, env):
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env
        raise RuntimeError("stop after wrapper generation")

    with patch.object(hive, "_DTACH_DIR", dtach_dir), \
         patch.object(hive, "_socket_alive", return_value=False), \
         patch.object(hive, "_write_sidecar"), \
         patch.object(hive, "_hive_color", return_value={"name": "blue", "rgb": "97;150;255", "c256": "75"}), \
         patch.object(hive.Path, "home", return_value=home), \
         patch("os.chdir"), \
         patch("os.execvpe", side_effect=capture_execvpe), \
         patch.dict(os.environ, {"HOME": str(home)}, clear=False):
        with pytest.raises(RuntimeError, match="stop after wrapper generation"):
            hive._launch_dtach(hive_root, "hive", workspace, "1")

    env = captured["env"]
    result = subprocess.run(
        ["zsh", "-i", "-c", 'print -r -- "$HIVE_REAL_ZDOTDIR|$HISTFILE|$FROM_REAL_ZSHENV|$FROM_REAL_ZSHRC"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == f"{home}|{home}/.zsh_history|1|1"


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


# --- Quiet-mode pull tests ---------------------------------------------------


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

    def test_is_notable_true_when_pulled_on_default_branch(self):
        """A repo on main that actually pulled new commits is notable."""
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='main',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.PULL,
            pulled=True,
        )
        assert hive._is_notable(result, 'main') is True

    def test_is_notable_true_when_pushed_on_default_branch(self):
        """A repo on main that pushed is notable."""
        result = hive.RepoStatus(
            path=Path('/repo'),
            branch='main',
            remote_profile=hive._ORIGIN_REMOTE,
            action=hive.SyncAction.PULL,
            pushed=True,
        )
        assert hive._is_notable(result, 'main') is True

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
            hive.RepoStatus(
                path=fake_hive / 'repo-3',
                branch='main',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.NONE,
                up_to_date=True,
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
        assert 'repo-3' not in out
        assert '2 repos clean / up to date' in out
        assert summary == {
            'repo_count': 3,
            'clean_count': 2,
            'all_clean': False,
            'lines': summary['lines'],
        }

    def test_pull_single_hive_quiet_shows_pulled_default_branch(self, fake_hive, capsys):
        """A repo on main that pulled new commits must appear in quiet output."""
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
                action=hive.SyncAction.PULL,
                pulled=True,
            ),
            hive.RepoStatus(
                path=fake_hive / 'repo-3',
                branch='main',
                remote_profile=hive._ORIGIN_REMOTE,
                action=hive.SyncAction.NONE,
                up_to_date=True,
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
        assert 'repo-3' not in out
        assert '2 repos clean / up to date' in out
        assert summary['clean_count'] == 2
        assert summary['all_clean'] is False

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
            hive.RepoStatus(
                path=fake_hive / 'repo-3',
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
             'lines': ['  proj-1  \u2717 feature/x \u2014 rebase failed']},
        ]
        with patch.object(hive, '_APIARY_CONFIG', config_file):
            with patch.object(hive, '_pull_single_hive', side_effect=summaries):
                hive.cmd_pull(args)

        out = capsys.readouterr().out
        assert f'\u2501\u2501 {hive._display_path(hive_roots[0])} \u2501\u2501  (all 2 repos clean)' in out
        assert f'\u2501\u2501 {hive._display_path(hive_roots[1])} \u2501\u2501' in out
        assert 'proj-1  \u2717 feature/x \u2014 rebase failed' in out


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


# --- Pull engine tests -------------------------------------------------------


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
        """A dirty repo is always reported as dirty, even on cache hit."""
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
            if args == ['rev-parse', 'HEAD']:
                return head_sha
            if args == ['status', '--porcelain']:
                return 'M dirty-file.txt'
            return None

        with patch.object(hive, '_git_out', side_effect=fake_git_out):
            with patch.object(hive, '_git') as mock_git:
                result = self._pull(repo, cache=cache)

        assert result.skipped is True
        assert result.dirty_count == 1
        assert result.cached is False
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


# --- Format pull segment tests -----------------------------------------------


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
