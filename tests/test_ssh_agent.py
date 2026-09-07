"""Tests for the ssh-agent adoption block in bash/bashrc.

macOS gets SSH_AUTH_SOCK from launchd for free.  On Linux the socket-activated
user agent exists but its SSH_AUTH_SOCK lives in the systemd user manager's
environment, which an sshd-spawned shell never inherits -- so bashrc has to
find the socket itself, and has to prove the agent behind it answers.

The live cases run a real `ssh-agent`; nothing here fakes the protocol.  A
socket that accepts connections but never replies would hang the probe rather
than exercise it.
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASHRC = str(REPO_ROOT / "bash" / "bashrc")
AGENT_UNIT = REPO_ROOT / "setup" / "ssh-agent.service"

pytestmark = pytest.mark.skipif(
    not shutil.which("ssh-agent") or not shutil.which("ssh-add"),
    reason="needs a real ssh-agent/ssh-add to probe reachability")


@pytest.fixture
def runtime_dir():
    """A short-pathed scratch dir standing in for XDG_RUNTIME_DIR.

    Unix socket paths cap at ~104 bytes on macOS, which pytest's tmp_path
    blows through on its own; mkdtemp under TMPDIR stays well inside it.
    """
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def agents():
    """Start real ssh-agents on demand; reap them all at teardown."""
    started = []

    def start(path):
        out = subprocess.run(["ssh-agent", "-a", str(path)],
                             capture_output=True, text=True, check=True).stdout
        match = re.search(r"SSH_AGENT_PID=(\d+)", out)
        assert match, f"no pid in ssh-agent output: {out!r}"
        pid = int(match.group(1))
        started.append(pid)
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.exists(), f"ssh-agent never bound {path}"
        return pid

    try:
        yield start
    finally:
        for pid in started:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def _stale_socket_node(path):
    """Leave a socket inode behind that nothing is listening on.

    This is the shape a stopped ssh-agent.socket leaves: none of the shipped
    units set RemoveOnStop=, and systemd's default is off.  `[[ -S ]]` still
    passes; connecting gets ECONNREFUSED.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    sock.close()
    assert path.is_socket()


def _source_bashrc(home, **env_overrides):
    """Source the real bashrc non-interactively and report what it set.

    Non-interactive on purpose: `ssh host 'git fetch'` is the case that needs
    an agent, and it returns at bashrc's interactive-only cutoff.  Reaching it
    proves the block sits above that cutoff.

    TERM_PUBLIC_ROOT points at a directory that does not exist, so the
    per-machine overlay and the Neovim manifest stay out of the run.
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
         'echo "LEAK=${_tp_agent_sock+set}${_tp_agent_probe+set}"'],
        capture_output=True, text=True, env=env)


def _reported(result, key):
    for line in result.stdout.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not reported: {result.stdout!r} {result.stderr!r}")


class TestSshAgentAdoption:

    @pytest.mark.parametrize("name", ["openssh_agent", "ssh-agent.socket"])
    def test_adopts_a_live_agent_socket(self, runtime_dir, agents, name):
        """Both packaged spellings of the socket-activated user agent."""
        agents(runtime_dir / name)

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(runtime_dir / name)

    def test_prefers_the_debian_socket_when_both_are_live(self, runtime_dir, agents):
        agents(runtime_dir / "openssh_agent")
        agents(runtime_dir / "ssh-agent.socket")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(runtime_dir / "openssh_agent")

    def test_ignores_a_stale_socket_node(self, runtime_dir):
        """A socket inode with nothing behind it is not an agent."""
        _stale_socket_node(runtime_dir / "openssh_agent")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == "<unset>"

    def test_a_stale_first_candidate_does_not_mask_a_live_second(
            self, runtime_dir, agents):
        """The search must continue past a dead candidate, not break on it."""
        _stale_socket_node(runtime_dir / "openssh_agent")
        agents(runtime_dir / "ssh-agent.socket")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(runtime_dir / "ssh-agent.socket")

    def test_never_overrides_an_inherited_agent(self, runtime_dir, agents):
        """launchd's socket, or a forwarded agent, always wins."""
        agents(runtime_dir / "openssh_agent")

        result = _source_bashrc(
            runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir),
            SSH_AUTH_SOCK="/var/run/com.apple.launchd.Test/Listeners")

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

    def test_leaves_no_loop_variables_behind(self, runtime_dir, agents):
        agents(runtime_dir / "openssh_agent")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

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


def _unit_section(name):
    """Parse one section of the shipped unit into a list of (key, value).

    A list, not a dict: systemd allows a key to repeat, and asserting on a
    field means asserting on what the unit actually declares.
    """
    entries = []
    current = None
    for line in AGENT_UNIT.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == name and "=" in line:
            key, value = line.split("=", 1)
            entries.append((key, value))
    return entries


def _unit_agent_socket(runtime_dir):
    """The socket path the shipped unit's ExecStart actually binds.

    Resolved from the unit's own ExecStart rather than restated here, so the
    binding between the unit and bashrc is proven at both ends.
    """
    exec_starts = [v for k, v in _unit_section("Service") if k == "ExecStart"]
    assert len(exec_starts) == 1, exec_starts
    argv = exec_starts[0].split()
    path = argv[argv.index("-a") + 1]
    return Path(path.replace("%t", str(runtime_dir)))


class TestShippedAgentUnit:
    """The fallback unit for releases that ship no socket-activated agent."""

    def test_bashrc_adopts_the_socket_the_unit_binds(self, runtime_dir, agents):
        """The unit and bashrc must agree on the path, proven end to end."""
        agents(_unit_agent_socket(runtime_dir))

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(_unit_agent_socket(runtime_dir))

    def test_unit_clears_a_stale_socket_node_before_starting(self, runtime_dir):
        """ssh-agent will not bind over the node an unclean stop leaves."""
        socket_path = str(_unit_agent_socket(runtime_dir)).replace(
            str(runtime_dir), "%t")
        pre = [v for k, v in _unit_section("Service") if k == "ExecStartPre"]

        assert any(socket_path in v and "rm" in v for v in pre), pre
        assert all(v.startswith("-") for v in pre), \
            "a missing socket node must not fail the start"

    def test_unit_is_installable(self):
        """No [Install] section is exactly why the distro service is unusable."""
        assert ("WantedBy", "default.target") in _unit_section("Install")

    def test_unit_does_not_rely_on_socket_activation(self):
        """The releases this covers do not all support a passed-in socket."""
        unit = AGENT_UNIT.read_text()

        assert "[Socket]" not in unit
        assert "ListenStream" not in unit
