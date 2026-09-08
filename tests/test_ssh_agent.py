"""Tests for the ssh-agent adoption block in bash/bashrc.

macOS gets SSH_AUTH_SOCK from launchd for free.  On Linux the socket-activated
user agent exists but its SSH_AUTH_SOCK lives in the systemd user manager's
environment, which an sshd-spawned shell never inherits -- so bashrc has to
find the socket itself, and has to prove the agent behind it answers.

The live cases run a real `ssh-agent`; nothing here fakes the protocol.  The
negative cases prove that a socket which accepts but never replies is bounded,
and that an undocumented probe status fails closed.
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
BASH = shutil.which("bash") or "bash"
BASHRC = str(REPO_ROOT / "bash" / "bashrc")
AGENT_UNIT = REPO_ROOT / "setup" / "term-public-ssh-agent.service"
VENDOR_AGENT_SOCKET_PATHS = {"%t/openssh_agent", "%t/ssh-agent.socket"}

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


@pytest.fixture(autouse=True)
def bounded_command(runtime_dir, monkeypatch):
    """Supply GNU-timeout semantics on macOS, where coreutils is not built in.

    Supported Linux hosts exercise their real `timeout`; this shim keeps the
    same behavioral tests portable to the macOS development baseline.
    """
    if shutil.which("timeout"):
        return

    bin_dir = runtime_dir / "timeout-bin"
    bin_dir.mkdir()
    timeout = bin_dir / "timeout"
    timeout.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[0].startswith('--kill-after=')\n"
        "seconds = float(args[1].removesuffix('s'))\n"
        "try:\n"
        "    result = subprocess.run(args[2:], timeout=seconds)\n"
        "except subprocess.TimeoutExpired:\n"
        "    raise SystemExit(124)\n"
        "raise SystemExit(result.returncode)\n")
    timeout.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")


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

    This is the shape a stopped Debian/Ubuntu or Fedora ssh-agent.socket can
    leave because those units omit RemoveOnStop= and systemd's default is off.
    (Arch sets it to yes.) `[[ -S ]]` still passes; connecting gets ECONNREFUSED.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    sock.close()
    assert path.is_socket()


def _nonresponsive_socket(path):
    """Listen without answering the agent protocol."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def _source_bashrc(home, bash_options=(), **env_overrides):
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
        [BASH, "--norc", *bash_options, "-c",
         f'source "{BASHRC}"; '
         'echo "SOCK=${SSH_AUTH_SOCK-<unset>}"; '
         'echo "LEAK=${_tp_agent_sock+set}${_tp_agent_probe+set}"'],
        capture_output=True, text=True, env=env, timeout=3)


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

    def test_nonresponsive_socket_is_bounded_and_rejected(self, runtime_dir):
        """A listening endpoint that never answers must not hang bashrc."""
        sock = _nonresponsive_socket(runtime_dir / "openssh_agent")
        started = time.monotonic()
        try:
            result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))
        finally:
            sock.close()

        assert time.monotonic() - started < 1.5
        assert _reported(result, "SOCK") == "<unset>"

    def test_unexpected_probe_status_fails_closed(self, runtime_dir):
        """Only documented live statuses 0 and 1 establish reachability."""
        _stale_socket_node(runtime_dir / "openssh_agent")
        bin_dir = runtime_dir / "failing-ssh-add"
        bin_dir.mkdir()
        ssh_add = bin_dir / "ssh-add"
        ssh_add.write_text("#!/bin/sh\nexit 42\n")
        ssh_add.chmod(0o755)

        result = _source_bashrc(
            runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir),
            PATH=f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")

        assert _reported(result, "SOCK") == "<unset>"

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

    def test_no_timeout_command_fails_closed(self, runtime_dir):
        """A host without GNU timeout must never run an unbounded probe."""
        _stale_socket_node(runtime_dir / "openssh_agent")
        bin_dir = runtime_dir / "no-timeout-bin"
        bin_dir.mkdir()
        marker = runtime_dir / "ssh-add-was-run"
        ssh_add = bin_dir / "ssh-add"
        ssh_add.write_text(
            "#!/bin/sh\n: > \"${SSH_ADD_MARKER:?}\"\nexit 0\n")
        ssh_add.chmod(0o755)

        result = _source_bashrc(
            runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir), PATH=str(bin_dir),
            SSH_ADD_MARKER=str(marker))

        assert not marker.exists()
        assert _reported(result, "SOCK") == "<unset>"

    def test_leaves_no_loop_variables_behind(self, runtime_dir, agents):
        agents(runtime_dir / "openssh_agent")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "LEAK") == ""

    def test_empty_agent_is_adopted_with_errexit(self, runtime_dir, agents):
        """The expected status 1 must not trip a caller's `set -e`."""
        agents(runtime_dir / "openssh_agent")

        result = _source_bashrc(
            runtime_dir, bash_options=("-e",),
            XDG_RUNTIME_DIR=str(runtime_dir))

        assert result.returncode == 0
        assert _reported(result, "SOCK") == str(runtime_dir / "openssh_agent")

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


def _unit_agent_socket_spec():
    """The socket path expression the shipped unit's ExecStart binds."""
    exec_starts = [v for k, v in _unit_section("Service") if k == "ExecStart"]
    assert len(exec_starts) == 1, exec_starts
    argv = exec_starts[0].split()
    return argv[argv.index("-a") + 1]


def _unit_agent_socket(runtime_dir):
    """Resolve the shipped unit's socket path for a test runtime directory.

    Resolved from the unit's own ExecStart rather than restated here, so the
    binding between the unit and bashrc is proven at both ends.
    """
    return Path(_unit_agent_socket_spec().replace("%t", str(runtime_dir)))


class TestShippedAgentUnit:
    """The fallback unit for releases that ship no socket-activated agent."""

    def test_bashrc_adopts_the_socket_the_unit_binds(self, runtime_dir, agents):
        """The unit and bashrc must agree on the path, proven end to end."""
        socket_path = _unit_agent_socket(runtime_dir)
        socket_path.parent.mkdir()
        agents(socket_path)

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(_unit_agent_socket(runtime_dir))

    def test_vendor_socket_wins_after_an_upgrade(self, runtime_dir, agents):
        """A newly available distribution agent supersedes the fallback."""
        fallback = _unit_agent_socket(runtime_dir)
        fallback.parent.mkdir()
        agents(fallback)
        agents(runtime_dir / "openssh_agent")

        result = _source_bashrc(runtime_dir, XDG_RUNTIME_DIR=str(runtime_dir))

        assert _reported(result, "SOCK") == str(runtime_dir / "openssh_agent")

    def test_unit_clears_a_stale_socket_node_before_starting(self, runtime_dir):
        """ssh-agent will not bind over the node an unclean stop leaves."""
        pre = [v for k, v in _unit_section("Service") if k == "ExecStartPre"]

        assert pre == [f"-/bin/rm -f {_unit_agent_socket_spec()}"]

    def test_unit_is_installable(self):
        """No [Install] section is exactly why the distro service is unusable."""
        assert ("WantedBy", "default.target") in _unit_section("Install")

    def test_unit_identity_and_socket_cannot_shadow_the_vendor(self):
        """An OS upgrade may add vendor ssh-agent.service and .socket units."""
        runtime_dirs = [
            v for k, v in _unit_section("Service") if k == "RuntimeDirectory"]

        assert AGENT_UNIT.name == "term-public-ssh-agent.service"
        assert _unit_agent_socket_spec() not in VENDOR_AGENT_SOCKET_PATHS
        assert runtime_dirs == ["term-public-ssh-agent"]
        assert _unit_agent_socket_spec().startswith(
            f"%t/{runtime_dirs[0]}/")

    def test_unit_does_not_rely_on_socket_activation(self):
        """The releases this covers do not all support a passed-in socket."""
        unit = AGENT_UNIT.read_text()

        assert "[Socket]" not in unit
        assert "ListenStream" not in unit
