"""Tests for the backup_and_link_file function in setup.sh."""

import os
import re
import subprocess
from pathlib import Path

import pytest


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


def test_setup_preserves_existing_zshenv(tmp_path):
    """A pre-existing .zshenv is backed up and sourced by the new one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    # Pre-existing user .zshenv with critical exports
    (home / ".zshenv").write_text('export MY_CRITICAL_VAR=hello\n')

    (repo_root / "setup.sh").write_text(Path(SETUP_SH).read_text())
    (repo_root / "ghostty").mkdir()
    (repo_root / "zsh").mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "ghostty" / "config").write_text("ghostty = true\n")
    zshenv_src = Path(__file__).resolve().parents[1] / "zsh" / "zshenv"
    (repo_root / "zsh" / "zshenv").write_text(zshenv_src.read_text())
    (repo_root / "zsh" / "zshrc").write_text("# zshrc\n")
    (repo_root / "zsh" / "hive-shell-prompt.zsh").write_text("# prompt\n")
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

    # The original is backed up
    assert (home / ".zshenv.bak").exists()
    assert (home / ".zshenv.bak").read_text() == 'export MY_CRITICAL_VAR=hello\n'

    # The new .zshenv sources the backup, so the user's var is still set
    r = subprocess.run(
        ["zsh", "-f", "-c",
         f'HOME="{home}" source "{home}/.zshenv"; echo "$MY_CRITICAL_VAR"'],
        capture_output=True,
        text=True,
    )
    assert r.stdout.strip() == "hello"


# --- Ghostty terminfo tic installation tests ---------------------------------


def _scaffold_repo(tmp_path):
    """Create a minimal fake repo and home for setup.sh tests."""
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
    (repo_root / "zsh" / "zshrc").write_text("# zshrc\n")
    (repo_root / "zsh" / "hive-shell-prompt.zsh").write_text("# prompt\n")
    (repo_root / "p10k.zsh").write_text("# p10k\n")
    (repo_root / "scripts" / "hive.py").write_text("#!/usr/bin/env python3\n")

    return repo_root, home


def _run_setup(repo_root, home, extra_env=None):
    """Run setup.sh in a fake repo with the given HOME.

    Inherits the real environment (including TERMINFO if set by Ghostty)
    so tests exercise the same code path a user would hit.
    """
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["zsh", "setup.sh"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


_GHOSTTY_TI = Path("/Applications/Ghostty.app/Contents/Resources/terminfo")


@pytest.mark.skipif(
    not _GHOSTTY_TI.is_dir(),
    reason="Ghostty app bundle not installed",
)
class TestTerminfoTicInstall:
    """Tests for the tic-based terminfo installation in setup.sh."""

    def test_installs_ghostty_terminfo(self, tmp_path):
        """setup.sh installs xterm-ghostty into ~/.terminfo when missing."""
        repo_root, home = _scaffold_repo(tmp_path)
        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert "Installed xterm-ghostty terminfo" in result.stdout

        # Verify infocmp can resolve it from the user terminfo dir
        r = subprocess.run(
            ["infocmp", "xterm-ghostty"],
            env={**os.environ, "HOME": str(home), "TERMINFO": ""},
            capture_output=True,
            text=True,
        )
        # The entry should exist under home's .terminfo
        ti_dir = home / ".terminfo"
        assert ti_dir.is_dir()
        # Find the compiled entry (stored under first-char subdir or hex subdir)
        entries = list(ti_dir.rglob("xterm-ghostty"))
        assert len(entries) > 0, f"No xterm-ghostty entry under {ti_dir}"

    def test_skips_when_already_resolvable(self, tmp_path):
        """setup.sh does not reinstall if xterm-ghostty is already resolvable."""
        repo_root, home = _scaffold_repo(tmp_path)

        # First run installs it
        r1 = _run_setup(repo_root, home)
        assert "Installed xterm-ghostty terminfo" in r1.stdout

        # Second run should skip (already resolvable via ~/.terminfo)
        r2 = _run_setup(repo_root, home)
        assert "Installed xterm-ghostty terminfo" not in r2.stdout

    def test_skips_without_ghostty_bundle(self, tmp_path):
        """setup.sh skips terminfo install when Ghostty app is not present."""
        repo_root, home = _scaffold_repo(tmp_path)

        # Patch setup.sh to use a nonexistent bundle path
        setup_text = (repo_root / "setup.sh").read_text()
        setup_text = setup_text.replace(
            '/Applications/Ghostty.app/Contents/Resources/terminfo',
            '/nonexistent/ghostty/terminfo',
        )
        (repo_root / "setup.sh").write_text(setup_text)

        result = _run_setup(repo_root, home)
        assert result.returncode == 0
        assert "Installed xterm-ghostty terminfo" not in result.stdout
