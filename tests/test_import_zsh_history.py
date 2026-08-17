"""Tests for scripts/import-zsh-history.py — the one-shot zsh -> bash
history import that accompanies the bash cutover's history parity."""

import importlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(_SCRIPTS_DIR))
importer = importlib.import_module('import-zsh-history')

REPO_ROOT = Path(__file__).resolve().parents[1]
BASHRC = REPO_ROOT / "bash" / "bashrc"
SETUP_SH = REPO_ROOT / "setup.sh"


class TestUnmetafy:
    def test_plain_bytes_pass_through(self):
        assert importer.unmetafy(b"echo hello") == b"echo hello"

    def test_meta_pair_restored(self):
        # zsh stores a metafied byte as 0x83 followed by byte ^ 0x20.
        assert importer.unmetafy(b"\x83\x89") == b"\xa9"

    def test_utf8_command_round_trips(self):
        # "café": zsh metafies both UTF-8 continuation bytes of é.
        metafied = b"caf\x83\xe3\x83\x89"
        assert importer.unmetafy(metafied).decode("utf-8") == "café"


class TestParse:
    def test_extended_entry(self):
        entries = importer.parse_zsh_history(": 1700000000:0;git status\n")
        assert entries == [(1700000000, "git status")]

    def test_plain_line_has_no_timestamp(self):
        assert importer.parse_zsh_history("ls -la\n") == [(None, "ls -la")]

    def test_multiline_entry_unfolds(self):
        text = ": 1700000001:0;echo one \\\ntwo\n: 1700000002:0;pwd\n"
        assert importer.parse_zsh_history(text) == [
            (1700000001, "echo one \ntwo"),
            (1700000002, "pwd"),
        ]

    def test_semicolons_in_command_survive(self):
        entries = importer.parse_zsh_history(": 1700000003:0;a; b; c\n")
        assert entries == [(1700000003, "a; b; c")]

    def test_blank_lines_skipped(self):
        assert importer.parse_zsh_history("\n\n: 1700000004:0;ls\n\n") == [
            (1700000004, "ls"),
        ]


class TestRender:
    def test_timestamped_entry(self):
        out = importer.render_bash_history([(1700000000, "git status")])
        assert out == "#1700000000\ngit status\n"

    def test_untimestamped_entry_has_no_marker(self):
        assert importer.render_bash_history([(None, "ls")]) == "ls\n"


class TestImport:
    def _seed(self, tmp_path, zsh_text=": 1700000000:0;echo imported\n",
              bash_text="existing\n"):
        zsh = tmp_path / ".zsh_history"
        bash = tmp_path / ".bash_history"
        zsh.write_text(zsh_text)
        if bash_text is not None:
            bash.write_text(bash_text)
        return zsh, bash

    def test_appends_and_backs_up(self, tmp_path):
        zsh, bash = self._seed(tmp_path)
        assert importer.import_history(zsh, bash) == 0
        assert bash.read_text() == "existing\n#1700000000\necho imported\n"
        backup = tmp_path / (".bash_history" + importer.BACKUP_SUFFIX)
        assert backup.read_text() == "existing\n"

    def test_missing_bash_history_created(self, tmp_path):
        zsh, bash = self._seed(tmp_path, bash_text=None)
        assert importer.import_history(zsh, bash) == 0
        assert bash.read_text() == "#1700000000\necho imported\n"

    def test_existing_history_without_newline_kept_separate(self, tmp_path):
        zsh, bash = self._seed(tmp_path, bash_text="existing")
        assert importer.import_history(zsh, bash) == 0
        assert bash.read_text() == "existing\n#1700000000\necho imported\n"

    def test_backup_and_new_destination_created_private(self, tmp_path):
        """History can contain secrets: with no pre-existing destination,
        both new files must be 0600 regardless of umask."""
        zsh, bash = self._seed(tmp_path, bash_text=None)
        old_umask = os.umask(0o022)
        try:
            assert importer.import_history(zsh, bash) == 0
        finally:
            os.umask(old_umask)
        backup = tmp_path / (".bash_history" + importer.BACKUP_SUFFIX)
        assert stat.S_IMODE(bash.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_existing_destination_mode_untouched_backup_private(self, tmp_path):
        """An existing destination keeps its mode (never loosened); the
        backup of it is still created 0600."""
        zsh, bash = self._seed(tmp_path)
        bash.chmod(0o600)
        old_umask = os.umask(0o022)
        try:
            assert importer.import_history(zsh, bash) == 0
        finally:
            os.umask(old_umask)
        backup = tmp_path / (".bash_history" + importer.BACKUP_SUFFIX)
        assert stat.S_IMODE(bash.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_concurrent_append_survives_import(self, tmp_path, monkeypatch):
        """A `history -a` from a live shell that lands between the
        pre-import snapshot and the final write must not be discarded.
        The interleave fires deterministically when the importer opens
        the destination for its append."""
        zsh, bash = self._seed(tmp_path)
        real_os_open = os.open

        def interleaving_open(path, flags, *args, **kwargs):
            if Path(path) == bash and flags & os.O_APPEND:
                with open(bash, "ab") as f:
                    f.write(b"concurrent_entry\n")
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(importer.os, "open", interleaving_open)
        assert importer.import_history(zsh, bash) == 0
        content = bash.read_text()
        assert "concurrent_entry" in content
        assert "echo imported" in content
        # The snapshot backup predates the concurrent append by design.
        backup = tmp_path / (".bash_history" + importer.BACKUP_SUFFIX)
        assert backup.read_text() == "existing\n"

    def test_second_run_refuses(self, tmp_path):
        zsh, bash = self._seed(tmp_path)
        assert importer.import_history(zsh, bash) == 0
        after_first = bash.read_text()
        assert importer.import_history(zsh, bash) == 1
        assert bash.read_text() == after_first

    def test_missing_zsh_history_fails(self, tmp_path):
        bash = tmp_path / ".bash_history"
        assert importer.import_history(tmp_path / "nope", bash) == 1
        assert not bash.exists()

    def test_setup_sh_names_the_real_backup_marker(self):
        """setup.sh's already-imported guard must test the exact file the
        importer writes (literal-constant contract)."""
        text = SETUP_SH.read_text()
        marker = f'$HOME/.bash_history{importer.BACKUP_SUFFIX}'
        assert f'! -f "{marker}"' in text

    def test_imported_entry_visible_to_bash(self, tmp_path):
        """End to end: bash parses the converted format — the imported
        command shows up in a live interactive shell's history.  The grep
        pattern is split so its own history line never matches."""
        zsh = tmp_path / ".zsh_history"
        zsh.write_text(": 1700000000:0;echo tp_zsh_marker\n")
        bash_hist = tmp_path / ".bash_history"
        assert importer.import_history(zsh, bash_hist) == 0
        r = subprocess.run(
            ["bash", "--rcfile", str(BASHRC), "-i"],
            input='history | grep -c "tp_zsh_mark""er"\n',
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(tmp_path), "TERM": "xterm-256color"},
            timeout=15,
        )
        assert r.returncode == 0
        assert r.stdout.splitlines()[-1].strip() == "1"
