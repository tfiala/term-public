"""Contract tests for setup.sh install semantics."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = REPO_ROOT / 'setup.sh'


class TestBinInstallsAreCopies:
    """~/bin executables must be installed as copies, never symlinks.

    infra/home-dc's scripts/copy-scripts.py also installs hive by copying
    from this repo's main; a symlink at ~/bin lets that write travel
    through into scripts/ and overwrite the canonical source (2026-08-16
    incident, where a copy of main clobbered an in-review branch).
    """

    BIN_DESTS = (
        '"$HOME/bin/hive"',
        '"$HOME/bin/hive-ci-popup"',
        '"$HOME/bin/term-theme"',
    )

    def test_copy_function_defined(self):
        assert 'backup_and_copy_file() {' in SETUP_SH.read_text()

    def test_bin_entries_installed_by_copy(self):
        text = SETUP_SH.read_text()
        for dest in self.BIN_DESTS:
            lines = [line for line in text.splitlines() if dest in line]
            assert lines, f'no install line targets {dest}'
            for line in lines:
                assert line.strip().startswith('backup_and_copy_file '), (
                    f'{dest} must be installed with backup_and_copy_file, '
                    f'got: {line.strip()}')
