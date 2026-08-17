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
    # Leaving an identical copy in place lets a linked dotfile silently go
    # stale as the repo moves on; it must become a symlink. No backup
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


def test_preserves_live_foreign_symlink_as_bak(tmp_path):
    """A destination symlink into another dotfiles repo is preserved:
    the link itself becomes the .bak, still pointing at the foreign file."""
    src = tmp_path / "source.txt"
    src.write_text("ours")
    foreign = tmp_path / "unrelated-dotfiles" / "bashrc"
    foreign.parent.mkdir()
    foreign.write_text("export THEIRS=1\n")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(foreign)

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert dest.is_symlink()
    assert os.readlink(str(dest)) == str(src)
    bak = tmp_path / "dest.txt.bak"
    assert bak.is_symlink()
    assert bak.read_text() == "export THEIRS=1\n"


def test_leaves_already_correct_symlink(tmp_path):
    """An idempotent re-run neither relinks nor creates a backup."""
    src = tmp_path / "source.txt"
    src.write_text("ours")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(src)

    result = run_backup_and_link(str(src), str(dest))

    assert result.returncode == 0
    assert os.readlink(str(dest)) == str(src)
    assert not os.path.lexists(tmp_path / "dest.txt.bak")


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


_SCAFFOLD_ORIGIN = "git@github.com:example/term-public.git"


# --- backup_and_copy_file unit tests ------------------------------------------


def _extract_copy_function() -> str:
    with open(SETUP_SH) as f:
        text = f.read()
    match = re.search(
        r"^(backup_and_copy_file\(\) \{.*?^})", text, re.MULTILINE | re.DOTALL)
    assert match, "Could not find backup_and_copy_file in setup.sh"
    return match.group(1)


_COPY_FUNCTION_DEF = _extract_copy_function()


def run_backup_and_copy(source: str, dest: str) -> subprocess.CompletedProcess:
    script = _COPY_FUNCTION_DEF + '\nbackup_and_copy_file "$1" "$2"'
    return subprocess.run(
        ["bash", "--norc", "-c", script, "bash", source, dest],
        capture_output=True,
        text=True,
    )


def _assert_installed_copy(src: Path, dest: Path):
    assert dest.is_file() and not dest.is_symlink()
    assert os.access(dest, os.X_OK)
    assert dest.read_bytes() == src.read_bytes()


def test_copy_creates_regular_file_for_new_destination(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "bin" / "tool"

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    # The decisive property: writing the copy must not reach the source.
    dest.write_text("clobbered")
    assert src.read_text() == "content"


def test_copy_identical_noop_normalizes_executable_bit(tmp_path):
    """An identical destination is left in place (same inode) but must
    still come out executable — a bare copy may have lost the bit."""
    src = tmp_path / "source.txt"
    src.write_text("same")
    dest = tmp_path / "dest.txt"
    dest.write_text("same")
    dest.chmod(0o644)
    ino_before = os.stat(dest).st_ino

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    assert os.stat(dest).st_ino == ino_before
    assert os.access(dest, os.X_OK)
    assert not (tmp_path / "dest.txt.bak").exists()


def test_copy_replaces_hard_link_alias_to_source(tmp_path):
    """A hard link to the source passes cmp but is still a write-through
    alias: it must be replaced by an independent inode, with no hard-link
    backup keeping the shared inode alive."""
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest.txt"
    os.link(src, dest)
    assert os.stat(dest).st_ino == os.stat(src).st_ino

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    assert os.stat(dest).st_ino != os.stat(src).st_ino
    assert not (tmp_path / "dest.txt.bak").exists()
    dest.write_text("clobbered")
    assert src.read_text() == "content"


def test_copy_backs_up_changed_file(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("new content")
    dest = tmp_path / "dest.txt"
    dest.write_text("old content")

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    backup = tmp_path / "dest.txt.bak"
    assert backup.read_text() == "old content"


def test_copy_migrates_symlink_to_source(tmp_path):
    """The legacy install shape — a symlink to our own source — becomes
    a copy without .bak churn."""
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(src)

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    assert not (tmp_path / "dest.txt.bak").exists()


def test_copy_migrates_relative_symlink_to_source(tmp_path):
    """A relative link resolving to the source is still our own install
    shape — identity is by resolution (-ef), not readlink text."""
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest.txt"
    os.symlink("source.txt", dest)

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    assert not (tmp_path / "dest.txt.bak").exists()


def test_copy_replaces_dangling_symlink(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(tmp_path / "nonexistent")

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    assert not (tmp_path / "dest.txt.bak").exists()


def test_copy_preserves_live_foreign_symlink_as_bak(tmp_path):
    """A destination symlink into another location is preserved: the link
    itself becomes the .bak, still pointing at the foreign file."""
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("foreign content")
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest.txt"
    dest.symlink_to(foreign)

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    backup = tmp_path / "dest.txt.bak"
    assert backup.is_symlink()
    assert backup.read_text() == "foreign content"


def test_copy_backs_up_directory(tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("content")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "inner.txt").write_text("inner")

    result = run_backup_and_copy(str(src), str(dest))

    assert result.returncode == 0
    _assert_installed_copy(src, dest)
    backup = tmp_path / "dest.bak"
    assert backup.is_dir()
    assert (backup / "inner.txt").read_text() == "inner"


def _git_init_with_origin(path, url):
    """Make path a git repo whose origin is url (no commits needed)."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", url], check=True)


def _scaffold_repo(tmp_path):
    """Create a minimal fake repo and home for setup.sh tests.

    The scaffold is a git repo with an origin remote because setup.sh
    proves zsh-era link provenance by comparing normalized origins.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _git_init_with_origin(repo_root, _SCAFFOLD_ORIGIN)

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
    (repo_root / "scripts" / "term-theme").write_text("#!/usr/bin/env bash\n")

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
    assert (home / "bin" / "hive").is_file()
    assert not (home / "bin" / "hive").is_symlink()


def test_setup_installs_bin_scripts_as_copies(tmp_path):
    """~/bin executables are copies, not symlinks — an external installer
    (infra/home-dc copy-scripts.py) overwriting ~/bin must not write
    through into the repo's canonical scripts/ (2026-08-16 incident)."""
    repo_root, home = _scaffold_repo(tmp_path)

    result = _run_setup(repo_root, home)

    assert result.returncode == 0
    for name, src in [("hive", "hive.py"),
                      ("hive-ci-popup", "hive-ci-popup.py"),
                      ("term-theme", "term-theme")]:
        dest = home / "bin" / name
        assert dest.is_file() and not dest.is_symlink()
        assert os.access(dest, os.X_OK)
        assert dest.read_bytes() == (repo_root / "scripts" / src).read_bytes()
        # The decisive property: writing the installed copy must not
        # reach the repo source.
        dest.write_text("clobbered by external installer\n")
        assert (repo_root / "scripts" / src).read_text() \
            != "clobbered by external installer\n"


def test_setup_migrates_bin_symlink_to_copy(tmp_path):
    """A legacy symlinked ~/bin/hive from an earlier setup.sh becomes a
    copy, without .bak churn."""
    repo_root, home = _scaffold_repo(tmp_path)
    bin_dir = home / "bin"
    bin_dir.mkdir()
    (bin_dir / "hive").symlink_to(repo_root / "scripts" / "hive.py")

    result = _run_setup(repo_root, home)

    assert result.returncode == 0
    dest = bin_dir / "hive"
    assert dest.is_file() and not dest.is_symlink()
    assert not (bin_dir / "hive.bak").exists()


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

    def test_prior_symlinked_configs_survive(self, tmp_path):
        """Prior .bashrc/.bash_profile that are live symlinks into another
        dotfiles repo (the #19 review probe) stay effective via the .bak
        links after setup."""
        repo_root, home = _scaffold_repo(tmp_path)
        _use_real_bash_config(repo_root)
        foreign = tmp_path / "unrelated-dotfiles"
        foreign.mkdir()
        (foreign / "bashrc").write_text("export MY_CRITICAL_VAR=hello\n")
        (foreign / "bash_profile").write_text("export MY_LOGIN_VAR=world\n")
        (home / ".bashrc").symlink_to(foreign / "bashrc")
        (home / ".bash_profile").symlink_to(foreign / "bash_profile")

        assert _run_setup(repo_root, home).returncode == 0

        assert (home / ".bashrc.bak").is_symlink()
        assert (home / ".bash_profile.bak").is_symlink()
        r = subprocess.run(
            ["bash", "--norc", "-c",
             'source "$HOME/.bash_profile"; '
             'echo "RC=$MY_CRITICAL_VAR LOGIN=$MY_LOGIN_VAR"'],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)},
        )
        assert r.returncode == 0
        assert "RC=hello LOGIN=world" in r.stdout

    def test_migration_notice_for_zsh_config(self, tmp_path):
        """Leftover zsh config gets explicit migration guidance."""
        repo_root, home = _scaffold_repo(tmp_path)
        (home / ".zshrc").write_text("export FROM_ZSH=1\n")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert "zsh-era config remains" in result.stdout
        assert "env.local" in result.stdout


def _make_old_term_public_checkout(path):
    """Create a fake zsh-era checkout of the SAME repository.

    Its origin uses the https form while the scaffold uses the scp form,
    so the test also proves origin normalization.  Deliberately contains
    no scripts/hive.py: identity must come from git, not path contents.
    """
    (path / "zsh").mkdir(parents=True)
    (path / "zsh" / "zshrc").write_text("# old term-public zshrc\n")
    (path / "zsh" / "zshenv").write_text("# old term-public zshenv\n")
    (path / "p10k.zsh").write_text("# old term-public p10k\n")
    _git_init_with_origin(path, "https://github.com/example/term-public.git")


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

    def test_removes_relative_link_to_verified_checkout(self, tmp_path):
        """A relative symlink target into a same-origin checkout is
        resolved from the link's directory and removed."""
        repo_root, home = _scaffold_repo(tmp_path)
        old_checkout = tmp_path / "old-checkout"
        _make_old_term_public_checkout(old_checkout)
        # Relative to $HOME: tmp_path/home/../old-checkout/zsh/zshrc
        (home / ".zshrc").symlink_to("../old-checkout/zsh/zshrc")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert not os.path.lexists(home / ".zshrc")

    def test_relative_target_resolved_from_link_dir_not_cwd(self, tmp_path):
        """The #19 review probe: a relative link whose cwd-relative
        resolution hits a same-origin decoy while its real target (from
        the link's directory) is a live foreign repo.  Identity must
        describe the actual target, so the link survives."""
        repo_root, _ = _scaffold_repo(tmp_path)
        nested = tmp_path / "nested"
        home = nested / "home"
        home.mkdir(parents=True)
        # Actual target, resolved from $HOME: a live foreign dotfiles repo.
        foreign = nested / "foreign"
        (foreign / "zsh").mkdir(parents=True)
        (foreign / "zsh" / "zshrc").write_text("# someone else's zshrc\n")
        _git_init_with_origin(foreign, "git@github.com:someone/dotfiles.git")
        # Decoy at the path a cwd-relative resolution would hit
        # (setup runs with cwd=tmp_path/repo): a same-origin checkout.
        _make_old_term_public_checkout(tmp_path / "foreign")
        (home / ".zshrc").symlink_to("../foreign/zsh/zshrc")

        result = _run_setup(repo_root, home)

        assert result.returncode == 0
        assert (home / ".zshrc").is_symlink()
        assert (home / ".zshrc").read_text() == "# someone else's zshrc\n"
        assert "could not be verified" in result.stderr

    def test_forged_marker_does_not_grant_provenance(self, tmp_path):
        """A foreign repo containing scripts/hive.py (the #19 review probe)
        is still foreign: identity is the git origin, not path contents."""
        repo_root, home = _scaffold_repo(tmp_path)
        foreign = tmp_path / "unrelated-dotfiles"
        (foreign / "zsh").mkdir(parents=True)
        (foreign / "scripts").mkdir()
        (foreign / "zsh" / "zshrc").write_text("# someone else's zshrc\n")
        (foreign / "scripts" / "hive.py").write_text("# forged marker\n")
        _git_init_with_origin(foreign, "git@github.com:someone/dotfiles.git")
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
