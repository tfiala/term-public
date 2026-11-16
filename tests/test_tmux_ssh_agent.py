"""Regression tests for tmux's ssh-agent socket handling (PR #50).

macOS launchd runs one ssh-agent per login, and the tmux server's global
environment carries its socket.  tmux's default ``update-environment``
includes ``SSH_AUTH_SOCK``, so a session created or attached from a terminal
without it marks the variable removed in that session, and every pane opened
there afterwards starts with no agent (2026-11-11, every hive session).
``tmux/tmux.conf`` therefore drops ``SSH_AUTH_SOCK`` from the list on Darwin
only.  Linux keeps tmux's default, so a forwarded agent follows the attach.

The behavioral tests load the real config into a private tmux server, in its
own ``TMUX_TMPDIR`` with a fake ``uname`` first on ``PATH`` choosing the
platform branch, and compare against the installed tmux's own default.  The
static test pins the guard where tmux is not installed.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TMUX_CONF = Path(__file__).resolve().parents[1] / "tmux" / "tmux.conf"

_DARWIN_UPDATE_ENVIRONMENT = re.compile(
    r"""if-shell 'test "\$\(uname -s\)" = Darwin' """
    r"""'set -g update-environment "(?P<names>[A-Z0-9_ ]+)"'"""
)

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


def test_update_environment_is_set_only_under_the_darwin_guard():
    commands = [
        line.strip()
        for line in TMUX_CONF.read_text().splitlines()
        if "update-environment" in line and not line.lstrip().startswith("#")
    ]
    assert len(commands) == 1, commands
    match = _DARWIN_UPDATE_ENVIRONMENT.fullmatch(commands[0])
    assert match, commands[0]
    names = match["names"].split()
    assert "SSH_AUTH_SOCK" not in names
    assert len(names) == len(set(names)), names


@pytest.fixture
def tmux_dir():
    """A private, short TMUX_TMPDIR, so no server shares the user's sockets.

    pytest's tmp_path can exceed the 104-byte socket path limit on macOS.
    """
    path = Path(tempfile.mkdtemp(prefix="tpa-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _tmux_env(tmux_dir: Path, system: str) -> dict[str, str]:
    """An environment outside any tmux whose ``uname -s`` prints ``system``."""
    bin_dir = tmux_dir / f"bin-{system}"
    bin_dir.mkdir(exist_ok=True)
    uname = bin_dir / "uname"
    uname.write_text(f"#!/bin/sh\necho {system}\n")
    uname.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["HOME"] = str(tmux_dir)
    env["TMUX_TMPDIR"] = str(tmux_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SSH_AUTH_SOCK"] = str(tmux_dir / "agent.sock")
    return env


def _tmux(env: dict[str, str], *args: str, check: bool = True):
    return subprocess.run(
        ["tmux", "-L", "probe", *args],
        capture_output=True, text=True, env=env, check=check, timeout=20,
    )


def _update_environment(tmux_dir: Path, config: str, system: str) -> list[str]:
    env = _tmux_env(tmux_dir, system)
    try:
        shown = _tmux(env, "-f", config, "start-server", ";",
                      "show-options", "-gv", "update-environment")
    finally:
        _tmux(env, "kill-server", check=False)
    return shown.stdout.split()


@needs_tmux
def test_tmux_default_includes_ssh_auth_sock(tmux_dir):
    # The premise of the fix: by default tmux copies the socket per attach.
    assert "SSH_AUTH_SOCK" in _update_environment(tmux_dir, os.devnull, "Darwin")


@needs_tmux
def test_macos_drops_only_ssh_auth_sock_from_tmux_default(tmux_dir):
    default = _update_environment(tmux_dir, os.devnull, "Darwin")
    macos = _update_environment(tmux_dir, str(TMUX_CONF), "Darwin")
    assert "SSH_AUTH_SOCK" not in macos
    assert set(default) - {"SSH_AUTH_SOCK"} <= set(macos), (
        "tmux.conf's macOS list drops more than SSH_AUTH_SOCK from this "
        f"tmux's default: {sorted(set(default) - set(macos))}"
    )


@needs_tmux
def test_linux_keeps_tmux_default(tmux_dir):
    default = _update_environment(tmux_dir, os.devnull, "Linux")
    linux = _update_environment(tmux_dir, str(TMUX_CONF), "Linux")
    assert linux == default


@needs_tmux
def test_macos_session_from_agentless_terminal_keeps_global_socket(tmux_dir):
    """The 2026-11-11 incident, end to end on a private server."""
    with_agent = _tmux_env(tmux_dir, "Darwin")
    without_agent = {k: v for k, v in with_agent.items()
                     if k != "SSH_AUTH_SOCK"}
    try:
        _tmux(with_agent, "-f", str(TMUX_CONF),
              "new-session", "-d", "-s", "first", "sleep 600")
        _tmux(without_agent, "new-session", "-d", "-s", "second", "sleep 600")
        global_env = _tmux(without_agent,
                           "show-environment", "-g", "SSH_AUTH_SOCK")
        session_env = _tmux(without_agent,
                            "show-environment", "-t", "=second", check=False)
    finally:
        _tmux(with_agent, "kill-server", check=False)
    expected = f"SSH_AUTH_SOCK={with_agent['SSH_AUTH_SOCK']}"
    assert global_env.stdout.strip() == expected
    assert session_env.returncode == 0, session_env.stderr
    assert "-SSH_AUTH_SOCK" not in session_env.stdout.splitlines()
