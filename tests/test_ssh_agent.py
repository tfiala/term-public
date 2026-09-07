"""Tests for the ssh-agent adoption block in bash/bashrc.

macOS gets SSH_AUTH_SOCK from launchd for free.  On Linux the socket-activated
user agent exists but its SSH_AUTH_SOCK lives in the systemd user manager's
environment, which an sshd-spawned shell never inherits -- so bashrc has to
point at the socket itself.
"""

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASHRC = str(REPO_ROOT / "bash" / "bashrc")


@pytest.fixture
def runtime_dir():
    """A short-pathed scratch dir.

    Unix socket paths cap at ~104 bytes on macOS, which pytest's tmp_path
    blows through on its own; mkdtemp under TMPDIR stays well inside it.
    """
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _listening_socket(path):
    """Bind and listen on path so bashrc's -S test sees a live socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def _source_bashrc(home, **env_overrides):
    """Source the real bashrc non-interactively and report what it set.

    Non-interactive on purpose: `ssh host 'git fetch'` is the case that needs
    an agent, and it returns from bashrc's interactive-only cutoff.  Reaching
    it proves the block sits above that cutoff.

    TERM_PUBLIC_ROOT points at an empty directory so the per-machine overlay
    and the Neovim manifest stay out of the run.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TERM_PUBLIC_ROOT": str(home / "no-such-checkout"),
    }
    env.update(env_overrides)
    return subprocess.run(
        ["bash", "--norc", "-c",
         f'source "{BASHRC}"; '
         'echo "SOCK=${SSH_AUTH_SOCK-<unset>}"; '
         'echo "LEAK=${_tp_agent_sock+set}"'],
        capture_output=True, text=True, env=env)


def _reported(result, key):
    for line in result.stdout.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not reported: {result.stdout!r} {result.stderr!r}")


class TestSshAgentAdoption:

    @pytest.mark.parametrize("name", ["openssh_agent", "ssh-agent.socket"])
    def test_adopts_a_listening_agent_socket(self, runtime_dir, name):
        """Both packaged spellings of the socket-activated user agent."""
        sock = _listening_socket(runtime_dir / name)
        try:
            result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))
        finally:
            sock.close()

        assert _reported(result, "SOCK") == str(runtime_dir / name)

    def test_prefers_the_debian_socket_when_both_exist(self, runtime_dir):
        a = _listening_socket(runtime_dir / "openssh_agent")
        b = _listening_socket(runtime_dir / "ssh-agent.socket")
        try:
            result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))
        finally:
            a.close()
            b.close()

        assert _reported(result, "SOCK") == str(runtime_dir / "openssh_agent")

    def test_never_overrides_an_inherited_agent(self, runtime_dir):
        """launchd's socket, or a forwarded agent, always wins."""
        sock = _listening_socket(runtime_dir / "openssh_agent")
        try:
            result = _source_bashrc(
                runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir),
                SSH_AUTH_SOCK="/var/run/com.apple.launchd.Test/Listeners")
        finally:
            sock.close()

        assert _reported(result, "SOCK") == \
            "/var/run/com.apple.launchd.Test/Listeners"

    def test_ignores_a_path_that_is_not_a_socket(self, runtime_dir):
        """A leftover regular file at the socket path is not an agent."""
        (runtime_dir / "openssh_agent").write_text("stale\n")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == "<unset>"

    def test_no_runtime_dir_leaves_the_variable_unset(self, runtime_dir):
        """macOS has no XDG_RUNTIME_DIR; the block must be a clean no-op."""
        result = _source_bashrc(runtime_dir)

        assert _reported(result, "SOCK") == "<unset>"

    def test_leaves_no_loop_variable_behind(self, runtime_dir):
        sock = _listening_socket(runtime_dir / "openssh_agent")
        try:
            result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))
        finally:
            sock.close()

        assert _reported(result, "LEAK") == ""

    def test_bashrc_never_starts_an_agent(self, runtime_dir):
        """A per-shell `ssh-agent` would orphan one agent per login.

        Behavioral rather than textual: the block legitimately names
        `ssh-agent.socket` as a candidate path, so grepping the file for the
        string would reject the very code it is meant to protect.  A shim
        first on PATH records any actual invocation.
        """
        shim = runtime_dir / "shim"
        shim.mkdir()
        marker = runtime_dir / "agent-was-started"
        started = shim / "ssh-agent"
        started.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
        started.chmod(0o755)

        result = _source_bashrc(
            runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir),
            PATH=f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}")

        assert not marker.exists(), "bashrc spawned an ssh-agent"
        assert _reported(result, "SOCK") == "<unset>"
