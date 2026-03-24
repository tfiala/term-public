"""Tests for hive.py shell and apiary helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    config_dir = tmp_path / "config" / "hive"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "apiary.json"
    config_file.write_text(json.dumps({"hives": [str(fake_hive)]}))
    return config_file


@pytest.fixture
def dtach_dir(tmp_path):
    directory = tmp_path / "hive-dtach"
    directory.mkdir()
    return directory


def test_workspace_number():
    assert hive._workspace_number("repo-3") == "3"
    assert hive._workspace_number("home-dc-5") == "5"
    assert hive._workspace_number("repo") is None


def test_real_zdotdir_defaults_to_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    with patch.object(hive.Path, "home", return_value=home):
        assert hive._real_zdotdir({}) == str(home)
        assert hive._real_zdotdir({"ZDOTDIR": str(tmp_path / "legacy-zsh")}) == str(home)


def test_real_zdotdir_preserves_nested_hive_original(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    original = tmp_path / "real-zsh"
    original.mkdir()

    with patch.object(hive.Path, "home", return_value=home):
        assert hive._real_zdotdir({"HIVE_REAL_ZDOTDIR": str(original)}) == str(original)


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
    with patch.object(hive, "_APIARY_CONFIG", fake_apiary):
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
