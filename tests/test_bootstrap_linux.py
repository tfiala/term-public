"""Contract tests for setup/bootstrap-linux.sh.

The script's real work is package installs and downloads, which no unit
test should perform (CI runs it for real on Ubuntu and in an AlmaLinux 9
container).  These tests pin the parts that must hold regardless:

- it never runs a distribution upgrade, only per-package installs;
- for each tool the package manager is tried before an upstream release;
- ``--dry-run`` produces each distribution's plan from any host, prints
  the privileged commands instead of running them, changes nothing, and
  needs neither root nor sudo;
- an unsupported distribution is refused up front.

Dry runs use a sandbox PATH holding only the coreutils the script needs,
so every tool reads as missing and the plan is the same on every host.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "setup" / "bootstrap-linux.sh"
BASHRC = REPO_ROOT / "bash" / "bashrc"
TROUBLESHOOT = REPO_ROOT / "scripts" / "term-public-troubleshoot.sh"
SMOKE = REPO_ROOT / "tests" / "smoke" / "bootstrap-linux.sh"

RHEL = (
    'NAME="Red Hat Enterprise Linux"\nID="rhel"\nID_LIKE="fedora"\n'
    'VERSION_ID="9.6"\nPRETTY_NAME="Red Hat Enterprise Linux 9.6 (Plow)"\n'
)
ROCKY = 'ID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.4"\n'
FEDORA = 'ID=fedora\nVERSION_ID=41\n'
UBUNTU = 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n'
DEBIAN = 'ID=debian\nVERSION_ID="12"\n'
ARCH = 'ID=arch\n'

# Everything the dry-run code path executes from PATH.  Package managers,
# rpm, sudo, getent and every installed tool are deliberately absent.
SANDBOX_TOOLS = ("bash", "uname", "id", "basename", "grep", "cut", "head", "cat")

# The tools the script installs, with the command that proves each present.
DNF_PLAN = {
    "tmux": "tmux", "git": "git", "jq": "jq", "fzf": "fzf",
    "ripgrep": "ripgrep", "fd-find": "fd-find", "bat": "bat", "eza": "eza",
    "gh": "gh", "starship": "starship", "bash-completion": "bash-completion",
    "python3": "python3",
}
UPSTREAM_FALLBACKS = {
    "starship": "starship/starship", "ripgrep": "BurntSushi/ripgrep",
    "fd": "sharkdp/fd", "bat": "sharkdp/bat", "eza": "eza-community/eza",
    "gh": "cli/cli", "jq": "jqlang/jq", "fzf": "junegunn/fzf",
}


@pytest.fixture
def sandbox(tmp_path):
    """A PATH with only the coreutils the dry run needs, plus an empty HOME."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in SANDBOX_TOOLS:
        real = shutil.which(tool)
        assert real, f"{tool} not found on this host"
        (bindir / tool).symlink_to(real)
    home = tmp_path / "home"
    home.mkdir()
    return {"bin": bindir, "home": home, "tmp": tmp_path}


def _run(sandbox, os_release, *args):
    osr = sandbox["tmp"] / "os-release"
    osr.write_text(os_release)
    env = {
        "PATH": str(sandbox["bin"]),
        "HOME": str(sandbox["home"]),
        "SHELL": "/bin/bash",
        "TERM_PUBLIC_OS_RELEASE": str(osr),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _code_lines(text):
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class TestStatic:
    def setup_method(self):
        self.text = SCRIPT.read_text()
        self.code = _code_lines(self.text)

    def test_executable_and_parses(self):
        assert os.access(SCRIPT, os.X_OK)
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_never_runs_a_distribution_upgrade(self):
        """A host with a lapsed subscription or a dead mirror must still be
        bootstrappable from what its reachable repos provide."""
        assert not re.search(
            r"\b(dnf|yum)\b[^\n]*\b(upgrade|update|distro-sync)\b", self.code)
        assert not re.search(
            r"\bapt(-get)?\b[^\n]*\b(upgrade|dist-upgrade|full-upgrade)\b", self.code)

    def test_apt_metadata_refresh_is_the_only_update(self):
        assert re.search(r"apt-get -qq update", self.code)

    def test_one_package_per_install_call(self):
        """`_pm_install` takes exactly one argument, so one unavailable
        package cannot abort a transaction that carries the others."""
        assert re.search(r'dnf -y -q install "\$1"', self.code)
        assert re.search(r'install --no-install-recommends "\$1"', self.code)

    def test_package_manager_before_upstream_release(self):
        """Inside the tool driver the package manager is consulted first and
        the upstream release only after it fails."""
        body = self.code[self.code.index("_tool() {"):]
        body = body[:body.index("\n}\n")]
        assert body.index("_pm_install") < body.index('"$manual"')

    def test_every_upstream_fallback_names_its_project(self):
        for tool, repo in UPSTREAM_FALLBACKS.items():
            assert re.search(rf"^_tool\s+{re.escape(tool)}\s.*\s{re.escape(repo)}\s*$",
                             self.code, re.MULTILINE), (tool, repo)
            assert f"https://github.com/{repo}/releases/" in self.code, repo

    def test_no_pipe_to_shell(self):
        """Downloads are files that get verified where a checksum exists —
        never `curl | sh`."""
        assert not re.search(r"curl[^\n|]*\|\s*(ba)?sh\b", self.code)

    def test_checksum_verified_where_published(self):
        assert "sha256sum -c" in self.code
        assert '_verify_if_published "$url"' in self.code

    def test_dry_run_gate_covers_privilege_and_network(self):
        assert '_priv() {' in self.code
        assert 'if (( DRY_RUN )); then' in self.code


class TestDryRunPlans:
    def test_rhel_plan(self, sandbox):
        r = _run(sandbox, RHEL, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "dry run" in r.stdout
        assert "/ dnf / " in r.stdout
        for pkg in DNF_PLAN.values():
            assert f"+ dnf -y -q install {pkg}\n" in r.stdout, pkg
        # No rpm in the sandbox reads as "EPEL not enabled": on RHEL proper
        # it comes from fedoraproject.org, keyed on the major version.
        assert "+ dnf -y -q install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm" in r.stdout
        for tool, repo in UPSTREAM_FALLBACKS.items():
            assert f"upstream release from github.com/{repo}" in r.stdout, tool
        assert "apt-get" not in r.stdout
        assert "Nothing was changed" in r.stdout

    def test_rocky_uses_the_epel_release_package(self, sandbox):
        r = _run(sandbox, ROCKY, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "+ dnf -y -q install epel-release\n" in r.stdout
        assert "fedoraproject.org" not in r.stdout

    def test_fedora_skips_epel(self, sandbox):
        r = _run(sandbox, FEDORA, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "epel" not in r.stdout.lower()
        assert "+ dnf -y -q install starship\n" in r.stdout

    def test_ubuntu_plan(self, sandbox):
        r = _run(sandbox, UBUNTU, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "/ apt / " in r.stdout
        assert "+ env DEBIAN_FRONTEND=noninteractive apt-get -qq update" in r.stdout
        for pkg in ("bash-completion", "git", "tmux", "fzf", "ripgrep",
                    "fd-find", "bat", "eza", "gh", "starship", "ncurses-term",
                    "python3"):
            assert re.search(
                rf"^\+ env DEBIAN_FRONTEND=noninteractive apt-get -qq -y .*install --no-install-recommends {re.escape(pkg)}$",
                r.stdout, re.MULTILINE), pkg
        assert "dnf" not in r.stdout
        assert "epel" not in r.stdout.lower()

    def test_debian_takes_the_apt_path(self, sandbox):
        r = _run(sandbox, DEBIAN, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "/ apt / " in r.stdout

    def test_unsupported_distribution_is_refused(self, sandbox):
        r = _run(sandbox, ARCH, "--dry-run")
        assert r.returncode == 1
        assert "unsupported distribution" in r.stderr
        assert "+ " not in r.stdout

    def test_dry_run_changes_nothing(self, sandbox):
        _run(sandbox, UBUNTU, "--dry-run")
        _run(sandbox, RHEL, "--dry-run")
        assert list(sandbox["home"].iterdir()) == []

    def test_dry_run_needs_neither_root_nor_sudo(self, sandbox):
        """The sandbox PATH has no sudo; a dry run must still complete."""
        assert not (sandbox["bin"] / "sudo").exists()
        r = _run(sandbox, RHEL, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "+ " in r.stdout

    def test_login_shell_already_bash_is_left_alone(self, sandbox):
        r = _run(sandbox, RHEL, "--dry-run")
        assert "Login shell is already bash" in r.stdout
        assert "chsh" not in r.stdout

    def test_login_shell_switch_is_planned_when_not_bash(self, sandbox):
        osr = sandbox["tmp"] / "os-release"
        osr.write_text(RHEL)
        env = {
            "PATH": str(sandbox["bin"]), "HOME": str(sandbox["home"]),
            "SHELL": "/bin/zsh", "TERM_PUBLIC_OS_RELEASE": str(osr),
        }
        r = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        assert re.search(r"^\+ chsh -s /(usr/)?bin/bash$", r.stdout, re.MULTILINE)

    def test_help_and_bad_argument(self, sandbox):
        assert _run(sandbox, RHEL, "--help").returncode == 0
        r = _run(sandbox, RHEL, "--frobnicate")
        assert r.returncode == 2
        assert "unknown argument" in r.stderr


class TestLinuxAwareConfig:
    """The shell config finds the Linux package layouts the bootstrap
    produces, and points at the right bootstrap script when starship is
    missing."""

    def test_bashrc_fzf_paths_cover_every_install_route(self):
        text = BASHRC.read_text()
        for path in ("/usr/share/fzf/shell/key-bindings.bash",          # RHEL/Fedora (EPEL)
                     "/usr/share/doc/fzf/examples/key-bindings.bash",   # Debian/Ubuntu
                     '"$HOME/.fzf/shell/key-bindings.bash"'):           # upstream fallback
            assert path in text, path

    def test_bashrc_completion_paths_cover_distributions(self):
        text = BASHRC.read_text()
        assert "/etc/profile.d/bash_completion.sh" in text
        assert "/usr/share/bash-completion/bash_completion" in text

    def test_missing_starship_hint_is_per_os(self):
        text = BASHRC.read_text()
        assert "bootstrap-macos.sh" in text
        assert "bootstrap-linux.sh" in text
        assert 'case "$OSTYPE" in' in text

    def test_troubleshoot_is_per_os(self):
        text = TROUBLESHOOT.read_text()
        assert "bootstrap-linux.sh" in text
        assert "getent passwd" in text
        r = subprocess.run(["bash", "-n", str(TROUBLESHOOT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_smoke_script_exists_and_parses(self):
        assert os.access(SMOKE, os.X_OK)
        r = subprocess.run(["bash", "-n", str(SMOKE)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
