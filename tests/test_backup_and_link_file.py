"""Tests for the backup_and_link_file function in setup.sh."""

import os
import re
import subprocess
from pathlib import Path


SETUP_SH = os.path.join(os.path.dirname(__file__), "..", "setup.sh")


def _extract_function() -> str:
    with open(SETUP_SH) as f:
        text = f.read()
    match = re.search(r"^(backup_and_link_file\(\) \{.*?^})", text, re.MULTILINE | re.DOTALL)
    assert match, "Could not find backup_and_link_file in setup.sh"
    return match.group(1)


_FUNCTION_DEF = _extract_function()


def run_backup_and_link(source: str, dest: str) -> subprocess.CompletedProcess:
    script = _FUNCTION_DEF + '\nbackup_and_link_file "$1" "$2"'
    return subprocess.run(
        ["zsh", "-f", "-c", script, "zsh", source, dest],
        capture_output=True,
        text=True,
    )


def test_creates_symlink_for_new_destination(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("hello")
    dest = tmp_path / "dest.txt"

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert dest.is_symlink()
    assert os.readlink(str(dest)) == str(src)


def test_replaces_existing_symlink(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("new")
    old_target = tmp_path / "old_target.txt"
    old_target.write_text("old")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(old_target)

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert dest.is_symlink()
    assert os.readlink(str(dest)) == str(src)


def test_backs_up_changed_file(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("new content")
    dest = tmp_path / "dest.txt"
    dest.write_text("old content")

    run_backup_and_link(str(src), str(dest))

    backup = tmp_path / "dest.txt.bak"
    assert backup.exists()
    assert backup.read_text() == "old content"
    assert dest.is_symlink()


def test_leaves_same_file_unchanged(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("same")
    dest = tmp_path / "dest.txt"
    dest.write_text("same")

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert not dest.is_symlink()
    assert dest.read_text() == "same"


def test_backs_up_directory(tmp_path):
    src = tmp_path / "source_dir"
    src.mkdir()
    dest = tmp_path / "dest_dir"
    dest.mkdir()
    (dest / "precious.txt").write_text("keep me")

    run_backup_and_link(str(src), str(dest))

    backup = tmp_path / "dest_dir.bak"
    assert backup.is_dir()
    assert (backup / "precious.txt").read_text() == "keep me"


def test_setup_creates_local_overlay_skeleton(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    (repo_root / "setup.sh").write_text(Path(SETUP_SH).read_text())
    (repo_root / "ghostty").mkdir()
    (repo_root / "zsh").mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "ghostty" / "config").write_text("ghostty = true\n")
    (repo_root / "zsh" / "zshenv").write_text("# zshenv\n")
    (repo_root / "zsh" / "zshrc").write_text("export TEST_ZSHRC=1\n")
    (repo_root / "zsh" / "hive-shell-prompt.zsh").write_text("# prompt helper\n")
    (repo_root / "p10k.zsh").write_text("# p10k\n")
    (repo_root / "scripts" / "hive.py").write_text("#!/usr/bin/env python3\n")

    result = subprocess.run(
        ["zsh", "setup.sh"],
        cwd=repo_root,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert (repo_root / "local" / "bin").is_dir()
    assert (repo_root / "local" / "env.local").exists()
    assert (repo_root / "local" / "zshrc.local").exists()
    assert (home / ".zshenv").is_symlink()
    assert (home / ".zshrc").is_symlink()
    assert (home / "bin" / "hive").is_symlink()
