"""Tests for history recall: Up/Down prefix search + fzf keybindings.

The zsh-era suggest-as-you-type recall came from zsh-autosuggestions,
which died with the #19 cutover; #24 restored the history data layer
(timestamps, share_history, import).  This feature restores the recall
UX on top of it: readline prefix search on Up/Down, and fzf's Ctrl-R
(fuzzy history), Ctrl-T (fuzzy file insert), and Alt-C (fuzzy cd).
"""

import os
import re
import select
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASHRC = REPO_ROOT / "bash" / "bashrc"
GHOSTTY_CONFIG = REPO_ROOT / "ghostty" / "config"
BOOTSTRAP = REPO_ROOT / "setup" / "bootstrap-macos.sh"

# Must stay in sync with the _tp_fzf loop in bash/bashrc;
# test_bashrc_names_these_paths asserts the sync.
FZF_KEYBINDING_PATHS = [
    "/opt/homebrew/opt/fzf/shell/key-bindings.bash",
    "/usr/local/opt/fzf/shell/key-bindings.bash",
    "/usr/share/doc/fzf/examples/key-bindings.bash",
]

INSTALLED_FZF_KEYBINDINGS = next(
    (p for p in FZF_KEYBINDING_PATHS if Path(p).is_file()), None)


def _interactive(home, snippet):
    return subprocess.run(
        ["bash", "--rcfile", str(BASHRC), "-i", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": str(home), "TERM": "xterm-256color"},
        timeout=15,
    )


class TestPrefixSearch:
    """Up/Down prefix-search the history instead of blind prev/next."""

    # CSI is the terminal's normal arrow encoding; SS3 is what it sends
    # in application cursor mode (DECCKM), which full-screen programs
    # can leave behind.
    ARROW_BINDINGS = [
        (r"\e[A", "history-search-backward"),
        (r"\e[B", "history-search-forward"),
        (r"\eOA", "history-search-backward"),
        (r"\eOB", "history-search-forward"),
    ]

    @pytest.mark.parametrize("keymap", ["vi-insert", "vi-command"])
    def test_arrows_bound_in_keymap(self, tmp_path, keymap):
        r = _interactive(tmp_path, f"bind -m {keymap} -p")
        assert r.returncode == 0
        for seq, func in self.ARROW_BINDINGS:
            assert f'"{seq}": {func}' in r.stdout, (
                f"{seq} not bound to {func} in {keymap}")

    def test_up_arrow_executes_prefix_match(self, tmp_path):
        """End-to-end under a pty: with `echo` typed, Up must recall the
        newest history line *starting with* `echo`, skipping the newer
        non-matching entry that plain previous-history would recall.
        The recalled entry's payload is a command substitution, so its
        executed output (tp_ok_42) cannot come from terminal echo of the
        typed/recalled line itself."""
        (tmp_path / ".bash_history").write_text(
            "echo tp_ok_$((40+2))\n"
            "true tp_last_entry\n")
        out = self._pty_session(tmp_path, "echo\x1b[A\rexit\r")
        assert "tp_ok_42" in out, f"prefix search did not recall/execute:\n{out}"

    def _pty_session(self, home, keystrokes, timeout=15):
        import pty

        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["bash", "--rcfile", str(BASHRC), "-i"],
            stdin=slave, stdout=slave, stderr=slave,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(home), "TERM": "xterm-256color"},
            start_new_session=True,
        )
        os.close(slave)
        os.write(master, keystrokes.encode())
        out = bytearray()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:  # child exited, pty torn down
                        break
                    if not chunk:
                        break
                    out += chunk
                elif proc.poll() is not None:
                    break
        finally:
            os.close(master)
            proc.wait(timeout=5)
        return out.decode(errors="replace")


class TestFzfKeybindings:
    """fzf's Ctrl-R / Ctrl-T / Alt-C wiring and its external contracts."""

    def test_bashrc_names_these_paths(self):
        """Both ends of the path contract: the candidate list this test
        module probes must be exactly what the bashrc loop probes."""
        text = BASHRC.read_text()
        m = re.search(r"for _tp_fzf in \\\n(.*?); do", text, re.S)
        assert m, "bashrc no longer has the _tp_fzf candidate loop"
        assert re.findall(r"(/\S+)", m.group(1)) == FZF_KEYBINDING_PATHS

    def test_source_is_guarded_and_first_match_wins(self):
        text = BASHRC.read_text()
        assert '[[ -r "$_tp_fzf" ]]' in text
        assert 'source "$_tp_fzf"' in text
        block = text[text.index("for _tp_fzf"):]
        block = block[:block.index("done")]
        assert "break" in block, "loop must stop at the first readable file"

    def test_ghostty_delivers_alt_c(self):
        """Alt-C only reaches bash if Ghostty treats Option as Alt;
        without this line the binding silently types ç instead."""
        assert re.search(r"^macos-option-as-alt = true$",
                         GHOSTTY_CONFIG.read_text(), re.M)

    def test_bootstrap_installs_fzf(self):
        assert re.search(r"^brew install .*\bfzf\b", BOOTSTRAP.read_text(),
                         re.M)

    @pytest.mark.skipif(INSTALLED_FZF_KEYBINDINGS is None,
                        reason="fzf key-bindings.bash not installed")
    def test_widgets_bound_after_sourcing_bashrc(self, tmp_path):
        """With fzf present, an interactive shell ends up with all three
        widgets live in the vi keymaps (bash>=4 binds Ctrl-R/Ctrl-T via
        bind -x; Alt-C is a macro chaining to the emacs-keymap widget)."""
        r = _interactive(
            tmp_path,
            "bind -m vi-insert -X; bind -m vi-command -X; "
            "bind -m vi-insert -s; bind -m emacs-standard -s")
        assert r.returncode == 0
        for keymap_dump in (r.stdout,):
            assert "__fzf_history__" in keymap_dump   # Ctrl-R
            assert "fzf-file-widget" in keymap_dump   # Ctrl-T
            assert "__fzf_cd__" in keymap_dump        # Alt-C target
        assert re.search(r'^"\\ec": ', r.stdout, re.M)  # Alt-C in vi-insert
