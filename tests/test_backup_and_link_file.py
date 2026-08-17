"""Tests for the backup_and_link_file function and link layout in setup.sh."""

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
        ["bash", "--norc", "-c", script, "bash", source, dest],
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


def test_replaces_identical_file_with_symlink(tmp_path):
    # Leaving an identical copy in place lets it silently go stale as the
    # repo moves on (bit ~/bin/hive); it must become a symlink. No backup
    # is taken since no content would be lost.
    src = tmp_path / "source.txt"
    src.write_text("same")
    dest = tmp_path / "dest.txt"
    dest.write_text("same")

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert dest.is_symlink()
    assert dest.read_text() == "same"
    assert not (tmp_path / "dest.txt.bak").exists()


def test_replaces_dangling_symlink(tmp_path):
    """A dangling destination symlink must be replaced, not break ln -s."""
    src = tmp_path / "source.txt"
    src.write_text("new")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(tmp_path / "deleted-target.txt")

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert result.stderr == ""
    assert dest.is_symlink()
    assert os.readlink(str(dest)) == str(src)


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


# --- Full setup.sh runs -------------------------------------------------------


def _scaffold_repo(tmp_path):
    """Create a minimal fake repo and home for setup.sh tests."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    (repo_root / "setup.sh").write_text(Path(SETUP_SH).read_text())
    (repo_root / "ghostty").mkdir()
    (repo_root / "bash").mkdir()
    (repo_root / "starship").mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "tmux").mkdir()
    (repo_root / "ghostty" / "config").write_text("ghostty = true\n")
    (repo_root / "bash" / "bash_profile").write_text("# bash_profile\n")
    (repo_root / "bash" / "bashrc").write_text("export TEST_BASHRC=1\n")
    (repo_root / "bash" / "inputrc").write_text("# inputrc\n")
    (repo_root / "starship" / "starship.toml").write_text("# starship\n")
    (repo_root / "tmux" / "tmux.conf").write_text("# tmux\n")
    (repo_root / "scripts" / "hive.py").write_text("#!/usr/bin/env python3\n")
    (repo_root / "scripts" / "hive-ci-popup.py").write_text("#!/usr/bin/env python3\n")

    return repo_root, home


def _run_setup(repo_root, home, extra_env=None):
    """Run setup.sh in a fake repo with the given HOME.

    Inherits the real environment (including TERMINFO if set by Ghostty)
    so tests exercise the same code path a user would hit.  HOME and
    XDG_CONFIG_HOME are overridden so nothing touches the real home.
    """
    env = {**os.environ, "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / ".config")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "setup.sh"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_setup_creates_local_overlay_skeleton(tmp_path):
    repo_root, home = _scaffold_repo(tmp_path)

    result = _run_setup(repo_root, home)

    assert result.returncode == 0
    assert (repo_root / "local" / "bin").is_dir()
    assert (repo_root / "local" / "env.local").exists()
    assert (repo_root / "local" / "bashrc.local").exists()
    assert (home / ".bash_profile").is_symlink()
    assert (home / ".bashrc").is_symlink()
    assert (home / ".inputrc").is_symlink()
    assert (home / ".config" / "starship.toml").is_symlink()
    assert (home / "bin" / "hive").is_symlink()


def test_setup_backs_up_existing_bashrc(tmp_path):
    """A pre-existing .bashrc is preserved as .bashrc.bak."""
    repo_root, home = _scaffold_repo(tmp_path)
    (home / ".bashrc").write_text("export MY_CRITICAL_VAR=hello\n")

    result = _run_setup(repo_root, home)

    assert result.returncode == 0
    assert (home / ".bashrc").is_symlink()
    assert (home / ".bashrc.bak").read_text() == "export MY_CRITICAL_VAR=hello\n"


def test_setup_repairs_dangling_bashrc_link(tmp_path):
    """A dangling ~/.bashrc symlink is replaced, not left broken."""
    repo_root, home = _scaffold_repo(tmp_path)
    (home / ".bashrc").symlink_to(tmp_path / "gone" / "bashrc")

    result = _run_setup(repo_root, home)

    assert result.returncode == 0
    assert (home / ".bashrc").is_symlink()
    assert os.readlink(home / ".bashrc") == str(repo_root / "bash" / "bashrc")


def test_setup_fails_fast_on_link_error(tmp_path):
    """setup.sh must exit nonzero when a step fails, not report success."""
    repo_root, home = _scaffold_repo(tmp_path)
    # mkdir -p "$HOME/bin" collides with a regular file.
    (home / "bin").write_text("not a directory\n")

    result = _run_setup(repo_root, home)

    assert result.returncode != 0
    assert "Linked config into place." not in result.stdout


def _use_real_bash_config(repo_root):
    """Copy the real bash config into the scaffold for behavior tests."""
    real = Path(SETUP_SH).resolve().parent
    (repo_root / "bash" / "bashrc").write_text((real / "bash" / "bashrc").read_text())
    (repo_root / "bash" / "bash_profile").write_text(
        (real / "bash" / "bash_profile").read_text())


class TestPriorConfigStaysEffective:
    """Backed-up bash config must keep working, not just exist as bytes."""

    def test_prior_bashrc_export_survives(self, tmp_path):
        repo_root, home = _scaffold_repo(tmp_path)
        _use_real_bash_config(repo_root)
        (home / ".bashrc").write_text("export MY_CRITICAL_VAR=hello\n")

        assert _run_setup(repo_root, home).returncode == 0

        r = subprocess.run(
            ["bash", "--norc", "-c",
             'source "$HOME/.bashrc"; echo "VAR=$MY_CRITICAL_VAR"'],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)},
        )
        assert r.returncode == 0
        assert "VAR=hello" in r.stdout

    def test_prior_bash_profile_export_survives(self, tmp_path):
        repo_root, home = _scaffold_repo(tmp_path)
        _use_real_bash_config(repo_root)
        (home / ".bash_profile").write_text("export MY_LOGIN_VAR=world\n")

        assert _run_setup(repo_root, home).returncode == 0

        r = subprocess.run(
            ["bash", "--norc", "-c",
             'source "$HOME/.bash_profile"; echo "VAR=$MY_LOGIN_VAR"'],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)},
        )
        assert r.returncode == 0
        assert "VAR=world" in r.stdout

    def test_migration_notice_for_zsh_config(self, tmp_path):
        """Leftover zsh config gets explicit migration guidance."""
        repo_root, home = _scaffold_repo(tmp_path)
        (home / ".zshrc").write_text("export FROM_ZSH=1\n")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert "zsh-era config remains" in result.stdout
        assert "env.local" in result.stdout


def _make_old_term_public_checkout(path):
    """Create a fake zsh-era term-public checkout with its provenance marker."""
    (path / "zsh").mkdir(parents=True)
    (path / "scripts").mkdir()
    (path / "zsh" / "zshrc").write_text("# old term-public zshrc\n")
    (path / "zsh" / "zshenv").write_text("# old term-public zshenv\n")
    (path / "p10k.zsh").write_text("# old term-public p10k\n")
    (path / "scripts" / "hive.py").write_text("#!/usr/bin/env python3\n")


class TestStaleZshLinkCleanup:
    """setup.sh removes zsh-era links only with proven term-public provenance."""

    def test_removes_links_to_verified_checkout(self, tmp_path):
        repo_root, home = _scaffold_repo(tmp_path)
        old_checkout = tmp_path / "old-checkout"
        _make_old_term_public_checkout(old_checkout)
        (home / ".zshrc").symlink_to(old_checkout / "zsh" / "zshrc")
        (home / ".zshenv").symlink_to(old_checkout / "zsh" / "zshenv")
        (home / ".p10k.zsh").symlink_to(old_checkout / "p10k.zsh")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert not os.path.lexists(home / ".zshrc")
        assert not os.path.lexists(home / ".zshenv")
        assert not os.path.lexists(home / ".p10k.zsh")

    def test_restores_pre_term_public_backup(self, tmp_path):
        """The .bak made when the zsh link was first created comes back."""
        repo_root, home = _scaffold_repo(tmp_path)
        old_checkout = tmp_path / "old-checkout"
        _make_old_term_public_checkout(old_checkout)
        (home / ".zshrc").symlink_to(old_checkout / "zsh" / "zshrc")
        (home / ".zshrc.bak").write_text("# my original zshrc\n")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert not (home / ".zshrc").is_symlink()
        assert (home / ".zshrc").read_text() == "# my original zshrc\n"

    def test_keeps_foreign_link_with_warning(self, tmp_path):
        """A live link into a non-term-public dotfiles repo is never removed."""
        repo_root, home = _scaffold_repo(tmp_path)
        foreign = tmp_path / "unrelated-dotfiles"
        (foreign / "zsh").mkdir(parents=True)
        (foreign / "zsh" / "zshrc").write_text("# someone else's zshrc\n")
        (home / ".zshrc").symlink_to(foreign / "zsh" / "zshrc")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert (home / ".zshrc").is_symlink()
        assert (home / ".zshrc").read_text() == "# someone else's zshrc\n"
        assert "could not be verified" in result.stderr

    def test_keeps_dangling_link_with_warning(self, tmp_path):
        """A dangling link (deleted checkout) has no provable provenance."""
        repo_root, home = _scaffold_repo(tmp_path)
        (home / ".zshrc").symlink_to(tmp_path / "deleted-checkout" / "zsh" / "zshrc")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert os.path.lexists(home / ".zshrc")
        assert "could not be verified" in result.stderr

    def test_leaves_foreign_zshrc_alone(self, tmp_path):
        """A user-managed .zshrc (not a term-public link) is untouched."""
        repo_root, home = _scaffold_repo(tmp_path)
        (home / ".zshrc").write_text("# hand-written\n")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert not (home / ".zshrc").is_symlink()
        assert (home / ".zshrc").read_text() == "# hand-written\n"


# --- Ghostty terminfo tic installation tests ---------------------------------


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
