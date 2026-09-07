"""Contract tests for setup/bootstrap-linux.sh.

The script's real work is package installs and downloads, which no unit
test should perform against the real world (CI runs it for real on Ubuntu
and in an AlmaLinux 9 container).  These tests pin the parts that must
hold regardless, in two ways:

- ``--dry-run`` plans, produced from any host through a sandbox PATH that
  holds only the coreutils the dry run needs, so every tool reads as
  missing and each distribution's plan is deterministic;
- real-mode runs against a *fake system*: the same sandbox plus fake
  ``sudo``, ``apt-get`` and ``curl`` that log every call and can be told to
  fail selectively, so the recovery paths (a dead package index, a package
  the manager cannot provide, a transient download failure, a rerun over a
  partial install, no root at all) are exercised for real, end to end,
  with upstream binaries landing in a temp ``--bin-dir``.
"""

import os
import platform
import re
import shutil
import stat
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
# rpm, getent and every installed tool are deliberately absent.
DRY_RUN_TOOLS = ("bash", "uname", "id", "basename", "grep", "cut", "head", "cat")
# What a real-mode run additionally needs (all real coreutils).
REAL_MODE_TOOLS = DRY_RUN_TOOLS + (
    "env", "awk", "mkdir", "rm", "mktemp", "find", "tar", "install", "ln", "chmod")

# Packages the plan must request when the sandbox PATH hides every tool.
# bash-completion is absent here on purpose: the script detects it by file,
# not by command, so its line depends on the host (see
# test_bash_completion_follows_the_host_file).
DNF_PLAN = ("tmux", "git", "jq", "fzf", "ripgrep", "fd-find", "bat", "eza",
            "gh", "starship", "python3")
APT_PLAN = ("git", "tmux", "fzf", "ripgrep", "fd-find", "bat", "eza", "gh",
            "starship", "ncurses-term", "python3")
BASH_COMPLETION_FILE = Path("/usr/share/bash-completion/bash_completion")
UPSTREAM_FALLBACKS = {
    "starship": "starship/starship", "ripgrep": "BurntSushi/ripgrep",
    "fd": "sharkdp/fd", "bat": "sharkdp/bat", "eza": "eza-community/eza",
    "gh": "cli/cli", "jq": "jqlang/jq", "fzf": "junegunn/fzf",
}
# System locations where a distribution package puts fzf's bindings.  A
# host that has one cannot exercise the "binary without bindings" paths.
SYSTEM_FZF_BINDINGS = (
    Path("/usr/share/fzf/shell/key-bindings.bash"),
    Path("/usr/share/doc/fzf/examples/key-bindings.bash"),
)

MACHINE = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
STARSHIP_TARGET = {"x86_64": "x86_64-unknown-linux-gnu",
                   "aarch64": "aarch64-unknown-linux-musl"}.get(MACHINE)
GO_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(MACHINE)

STUB = '#!/bin/sh\nif [ "$1" = --version ]; then echo "%s (fake)"; fi\n'

FAKE_SUDO = '#!/bin/sh\nexec "$@"\n'

# Logs every call; fails for the subcommands or subcommand:package tokens in
# $FAKE_APT_FAIL; a successful `install PKG` drops a stub command named for
# the package's binary into $FAKE_BIN, the way the real package would.
FAKE_APT_GET = r'''#!/bin/sh
echo "apt-get $*" >> "$FAKE_LOG"
sub=""; pkg=""
for a in "$@"; do
  case "$a" in
    update|install) sub="$a" ;;
    -*) ;;
    *) [ -n "$sub" ] && pkg="$a" ;;
  esac
done
for f in $FAKE_APT_FAIL; do
  if [ "$f" = "$sub" ] || [ "$f" = "$sub:$pkg" ]; then
    echo "E: fake apt-get failure for '$sub $pkg'" >&2
    exit 100
  fi
done
if [ "$sub" = install ]; then
  case "$pkg" in
    ripgrep) cmd=rg ;;
    fd-find) cmd=fd ;;
    ncurses-bin) cmd=tic ;;
    bash-completion|ncurses-term) cmd="" ;;
    *) cmd="$pkg" ;;
  esac
  if [ -n "$cmd" ]; then
    printf '#!/bin/sh\nif [ "$1" = --version ]; then echo "0.74.3 (fake)"; fi\n' > "$FAKE_BIN/$cmd"
    chmod 755 "$FAKE_BIN/$cmd"
  fi
fi
exit 0
'''

# Logs every URL; fails for URLs containing a token of $FAKE_CURL_FAIL and
# for every .sha256 (as a project that publishes none); answers a HEAD on
# releases/latest with a tag; otherwise writes a plausible asset: a tarball
# holding a stub binary named as the real asset would, a stub shell file,
# or a stub bare binary.
FAKE_CURL = r'''#!/bin/sh
dest=""; url=""; head=0; prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then dest="$a"; prev=""; continue; fi
  if [ -n "$prev" ]; then prev=""; continue; fi
  case "$a" in
    -o|-w|--retry|--connect-timeout) prev="$a" ;;
    -*I*) head=1 ;;
    -*) ;;
    *) url="$a" ;;
  esac
done
echo "curl $url" >> "$FAKE_LOG"
for f in $FAKE_CURL_FAIL; do
  case "$url" in *"$f"*) echo "curl: (22) fake failure for $url" >&2; exit 22 ;; esac
done
case "$url" in *.sha256) exit 22 ;; esac
if [ "$head" = 1 ]; then
  printf '%s\n' "${url%/releases/latest}/releases/tag/v9.9.9"
  exit 0
fi
[ -n "$dest" ] || exit 0
mkdir -p "${dest%/*}"
case "$url" in
  *.tar.gz)
    base="${url##*/}"
    case "$base" in
      starship-*) bin=starship ;; ripgrep-*) bin=rg ;; fd-v*) bin=fd ;; bat-v*) bin=bat ;;
      eza_*) bin=eza ;; gh_*) bin=gh ;; fzf-*) bin=fzf ;; *) bin=unknown ;;
    esac
    work="$(mktemp -d "${TMPDIR:-/tmp}/fakecurl.XXXXXX")"
    mkdir -p "$work/pkg/bin"
    printf '#!/bin/sh\nif [ "$1" = --version ]; then echo "9.9.9 (fake)"; fi\n' > "$work/pkg/bin/$bin"
    chmod 755 "$work/pkg/bin/$bin"
    tar -czf "$dest" -C "$work" pkg
    rm -rf "$work" ;;
  *key-bindings.bash) printf '__fzf_history__() { :; }\n' > "$dest" ;;
  *completion.bash) printf '# fake completion\n' > "$dest" ;;
  *) printf '#!/bin/sh\nif [ "$1" = --version ]; then echo "9.9.9 (fake)"; fi\n' > "$dest"; chmod 755 "$dest" ;;
esac
exit 0
'''


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_sandbox(tmp_path, tools, *, sudo):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in tools:
        real = shutil.which(tool)
        assert real, f"{tool} not found on this host"
        (bindir / tool).symlink_to(real)
    if sudo:
        _write_exec(bindir / "sudo", FAKE_SUDO)
    home = tmp_path / "home"
    home.mkdir()
    # Upstream binaries go to a temp --bin-dir: the script prepends BIN_DIR
    # to PATH for its own presence checks, so a real system directory there
    # would let host binaries leak into the plan.
    return {"bin": bindir, "home": home, "tmp": tmp_path, "bin_out": tmp_path / "bin-out"}


@pytest.fixture
def sandbox(tmp_path):
    """Dry-run sandbox with a fake sudo, so the package-manager plan prints."""
    return _make_sandbox(tmp_path, DRY_RUN_TOOLS, sudo=True)


@pytest.fixture
def sandbox_nosudo(tmp_path):
    return _make_sandbox(tmp_path, DRY_RUN_TOOLS, sudo=False)


@pytest.fixture
def fake_system(tmp_path):
    """Real-mode sandbox: coreutils plus fake sudo, apt-get and curl, a log
    they all append to, and a temp --bin-dir for upstream binaries."""
    sb = _make_sandbox(tmp_path, REAL_MODE_TOOLS, sudo=True)
    _write_exec(sb["bin"] / "apt-get", FAKE_APT_GET)
    _write_exec(sb["bin"] / "curl", FAKE_CURL)
    sb["log"] = tmp_path / "calls.log"
    sb["log"].touch()
    return sb


def _env(sb, os_release, **extra):
    osr = sb["tmp"] / "os-release"
    osr.write_text(os_release)
    env = {
        "PATH": str(sb["bin"]),
        "HOME": str(sb["home"]),
        "SHELL": "/bin/bash",
        "TMPDIR": str(sb["tmp"]),
        "TERM_PUBLIC_OS_RELEASE": str(osr),
    }
    env.update(extra)
    return env


def _run(sb, os_release, *args, bin_dir=True, **extra):
    argv = list(args)
    if bin_dir and not any(a.startswith("--bin-dir") for a in argv):
        argv += ["--bin-dir", str(sb["bin_out"])]
    return subprocess.run(
        ["bash", str(SCRIPT), *argv],
        capture_output=True, text=True, env=_env(sb, os_release, **extra), timeout=60,
    )


def _real_run(sb, os_release, *args, apt_fail="", curl_fail="", bin_dir=True):
    """A real-mode run against the fake system.  The call log is truncated
    first so each run's assertions see only its own calls."""
    sb["log"].write_text("")
    r = _run(sb, os_release, *args, bin_dir=bin_dir,
             FAKE_LOG=str(sb["log"]), FAKE_BIN=str(sb["bin"]),
             FAKE_APT_FAIL=apt_fail, FAKE_CURL_FAIL=curl_fail)
    r.log = sb["log"].read_text().splitlines()
    return r


def _code_lines(text):
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _is_exec(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


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

    def test_index_refresh_is_advisory(self):
        """`_pm_update` is the only metadata refresh, and its failure is
        caught at the call site rather than aborting under `set -e`."""
        assert re.search(r"apt-get -qq update", self.code)
        assert re.search(r"^_pm_update \|\| echo \"warning: package index refresh failed",
                         self.code, re.MULTILINE)

    def test_one_package_per_install_call(self):
        """`_pm_install` takes exactly one argument, so one unavailable
        package cannot abort a transaction that carries the others."""
        assert re.search(r'dnf -y -q install "\$1"', self.code)
        assert re.search(r'install --no-install-recommends "\$1"', self.code)

    def test_package_manager_before_upstream_release(self):
        """Inside the tool driver the package manager is consulted first and
        the upstream release only after it fails or leaves the tool
        incomplete."""
        body = self.code[self.code.index("_tool() {"):]
        body = body[:body.index("\n}\n")]
        assert body.index("_pm_install") < body.index('"$manual"')
        assert "still incomplete; repairing" in body

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

    def test_fzf_bindings_paths_are_the_ones_bashrc_probes(self):
        """Both ends of the contract: every location the bootstrap accepts
        as 'bindings present' is one bash/bashrc will actually source."""
        m = re.search(r"FZF_BINDINGS_PATHS=\(\n(.*?)\n\)", self.code, re.S)
        assert m, "FZF_BINDINGS_PATHS array missing"
        bootstrap_paths = [tok.strip().strip('"') for tok in m.group(1).splitlines()]
        assert bootstrap_paths, "FZF_BINDINGS_PATHS is empty"
        bashrc_text = BASHRC.read_text()
        for p in bootstrap_paths:
            assert p in bashrc_text, f"{p} accepted by bootstrap but not probed by bashrc"


class TestDryRunPlans:
    def test_rhel_plan(self, sandbox):
        r = _run(sandbox, RHEL, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "dry run" in r.stdout
        assert "/ dnf / " in r.stdout
        for pkg in DNF_PLAN:
            assert f"+ dnf -y -q install {pkg}\n" in r.stdout, pkg
        # No rpm in the sandbox reads as "EPEL not enabled": on RHEL proper
        # it comes from fedoraproject.org, keyed on the major version.
        assert "+ dnf -y -q install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm" in r.stdout
        for tool, repo in UPSTREAM_FALLBACKS.items():
            assert f"upstream release fallback (github.com/{repo}) would run:" in r.stdout, tool
        assert "apt-get" not in r.stdout
        assert "Nothing was changed" in r.stdout

    @pytest.mark.skipif(STARSHIP_TARGET is None, reason=f"no release target for {MACHINE}")
    def test_upstream_fallback_plan_is_concrete(self, sandbox):
        """The dry run shows the actual download, verification and install
        commands a fallback would execute — with <latest> where a version
        would be resolved over the network — not just the project name."""
        r = _run(sandbox, RHEL, "--dry-run")
        out = r.stdout
        starship = f"https://github.com/starship/starship/releases/latest/download/starship-{STARSHIP_TARGET}.tar.gz"
        assert f"+ curl -fsSL -o <tmp>/asset.tar.gz {starship}\n" in out
        assert f"+ curl -fsSL -o <tmp>/asset.tar.gz.sha256 {starship}.sha256  (verify with sha256sum -c if published)\n" in out
        assert "+ tar -xzf <tmp>/asset.tar.gz -C <tmp>\n" in out
        assert f"+ install -m 0755 <tmp>/starship {sandbox['bin_out']}/starship\n" in out
        assert (f"https://github.com/BurntSushi/ripgrep/releases/download/<latest>/"
                f"ripgrep-<latest>-{MACHINE}-unknown-linux-musl.tar.gz") in out
        assert f"https://github.com/jqlang/jq/releases/latest/download/jq-linux-{GO_ARCH}\n" in out
        assert f"https://github.com/cli/cli/releases/download/v<latest>/gh_<latest>_linux_{GO_ARCH}.tar.gz" in out
        # fzf's fallback is two artifacts: the binary and the shell files.
        assert f"fzf-<latest>-linux_{GO_ARCH}.tar.gz" in out
        home = sandbox["home"]
        assert (f"+ curl -fsSL -o {home}/.fzf/shell/key-bindings.bash "
                "https://raw.githubusercontent.com/junegunn/fzf/v<latest>/shell/key-bindings.bash\n") in out
        assert f"+ curl -fsSL -o {home}/.fzf/shell/completion.bash " in out

    def test_rocky_uses_the_epel_release_package(self, sandbox):
        r = _run(sandbox, ROCKY, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "+ dnf -y -q install epel-release\n" in r.stdout
        assert "fedoraproject.org" not in r.stdout

    def test_fedora_skips_epel(self, sandbox):
        r = _run(sandbox, FEDORA, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "==> EPEL" not in r.stdout
        assert "epel-release" not in r.stdout
        assert "+ dnf -y -q install starship\n" in r.stdout

    def test_ubuntu_plan(self, sandbox):
        r = _run(sandbox, UBUNTU, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "/ apt / " in r.stdout
        assert "+ env DEBIAN_FRONTEND=noninteractive apt-get -qq update" in r.stdout
        for pkg in APT_PLAN:
            assert re.search(
                rf"^\+ env DEBIAN_FRONTEND=noninteractive apt-get -qq -y .*install --no-install-recommends {re.escape(pkg)}$",
                r.stdout, re.MULTILINE), pkg
        assert "dnf" not in r.stdout
        assert "==> EPEL" not in r.stdout

    @pytest.mark.parametrize("os_release,install_line", [
        (RHEL, "+ dnf -y -q install bash-completion\n"),
        (UBUNTU, "install --no-install-recommends bash-completion\n"),
    ])
    def test_bash_completion_follows_the_host_file(self, sandbox, os_release, install_line):
        """bash-completion has no command to probe, so presence is the
        package's script on disk — which the sandbox PATH cannot hide."""
        r = _run(sandbox, os_release, "--dry-run")
        assert r.returncode == 0, r.stderr
        if BASH_COMPLETION_FILE.is_file():
            assert install_line not in r.stdout
            assert re.search(r"^  already present: .*\bbash-completion\b", r.stdout, re.MULTILINE)
        else:
            assert install_line in r.stdout

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

    def test_dry_run_without_sudo_plans_the_unprivileged_mode(self, sandbox_nosudo):
        """No sudo on PATH: the plan skips the package manager, sends the
        upstream releases to ~/bin, and still completes with status 0."""
        r = _run(sandbox_nosudo, RHEL, "--dry-run", bin_dir=False)
        assert r.returncode == 0, r.stderr
        assert "No root or sudo: package-manager steps are skipped" in r.stdout
        assert "+ dnf" not in r.stdout
        assert f"+ install -m 0755 <tmp>/starship {sandbox_nosudo['home']}/bin/starship\n" in r.stdout

    def test_login_shell_already_bash_is_left_alone(self, sandbox):
        r = _run(sandbox, RHEL, "--dry-run")
        assert "Login shell is already bash" in r.stdout
        assert "chsh" not in r.stdout

    def test_login_shell_switch_is_planned_when_not_bash(self, sandbox):
        r = _run(sandbox, RHEL, "--dry-run", SHELL="/bin/zsh")
        assert r.returncode == 0, r.stderr
        assert re.search(r"^\+ chsh -s /(usr/)?bin/bash$", r.stdout, re.MULTILINE)

    def test_help_and_bad_arguments(self, sandbox):
        assert _run(sandbox, RHEL, "--help").returncode == 0
        r = _run(sandbox, RHEL, "--frobnicate")
        assert r.returncode == 2
        assert "unknown argument" in r.stderr
        r = _run(sandbox, RHEL, "--bin-dir")
        assert r.returncode == 2


@pytest.mark.skipif(STARSHIP_TARGET is None, reason=f"no release target for {MACHINE}")
class TestRecovery:
    """Real-mode runs against the fake system: the failure and rerun paths
    the script promises to survive."""

    def test_index_refresh_failure_does_not_abort(self, fake_system):
        """A dead apt source fails `apt-get update`; every per-tool install
        must still be attempted, and the upstream fallbacks must still run
        for what apt cannot provide."""
        r = _real_run(fake_system, UBUNTU, apt_fail="update install:starship install:eza")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "warning: package index refresh failed" in r.stderr
        update_idx = next(i for i, l in enumerate(r.log) if l.startswith("apt-get") and " update" in l)
        installs = [l for l in r.log if l.startswith("apt-get") and " install " in l]
        assert installs, "no package install attempted after the failed refresh"
        assert all(r.log.index(l) > update_idx for l in installs)
        for pkg in ("git", "tmux", "ripgrep", "fzf", "starship", "eza"):
            assert any(l.endswith(f" {pkg}") for l in installs), pkg
        starship = f"https://github.com/starship/starship/releases/latest/download/starship-{STARSHIP_TARGET}.tar.gz"
        assert f"curl {starship}" in r.log
        assert _is_exec(fake_system["bin_out"] / "starship")
        assert _is_exec(fake_system["bin_out"] / "eza")
        assert f"starship (upstream release -> {fake_system['bin_out']})" in r.stdout
        assert f"eza (upstream release -> {fake_system['bin_out']})" in r.stdout
        # Tools apt did provide were not fetched upstream.
        assert not any("ripgrep" in l for l in r.log if l.startswith("curl"))

    def test_package_manager_failure_alone_is_not_fatal(self, fake_system):
        """Every apt install fails (unreachable repos): the run still
        completes, installs what has an upstream release, and reports the
        rest instead of aborting on the first error."""
        r = _real_run(fake_system, UBUNTU, apt_fail="install")
        assert r.returncode == 1
        assert "NOT installed:" in r.stderr
        for tool in ("git", "tmux"):
            assert re.search(rf"^NOT installed: .*\b{tool}\b", r.stderr, re.MULTILINE), tool
        for binary in ("starship", "rg", "fd", "bat", "eza", "gh", "jq", "fzf"):
            assert _is_exec(fake_system["bin_out"] / binary), binary
        assert "Bootstrap complete" in r.stdout

    @pytest.mark.skipif(any(p.is_file() for p in SYSTEM_FZF_BINDINGS),
                        reason="a system fzf package's bindings would satisfy the check")
    def test_fzf_partial_install_is_repaired_on_rerun(self, fake_system):
        """The review reproduction: a bare fzf binary with no shell bindings
        (an interrupted earlier run, or a hand-installed binary) must not be
        skipped as present; the next run installs the bindings at the
        binary's own version."""
        _write_exec(fake_system["bin"] / "fzf", STUB % "0.74.3")
        bindings = fake_system["home"] / ".fzf" / "shell" / "key-bindings.bash"

        # Run 1: apt has no fzf package and the bindings download fails.
        r1 = _real_run(fake_system, UBUNTU, apt_fail="install:fzf", curl_fail="key-bindings")
        assert r1.returncode == 1
        assert re.search(r"^NOT installed: .*\bfzf\b", r1.stderr, re.MULTILINE)
        assert _is_exec(fake_system["bin"] / "fzf")   # the binary survives
        assert not bindings.exists()
        assert not any("fzf-" in l and l.endswith(".tar.gz") for l in r1.log), \
            "binary was already present; it must not be downloaded again"

        # Run 2: network is back.  fzf must be re-attempted, not skipped.
        r2 = _real_run(fake_system, UBUNTU, apt_fail="install:fzf")
        assert r2.returncode == 0, r2.stderr + r2.stdout
        assert "curl https://raw.githubusercontent.com/junegunn/fzf/v0.74.3/shell/key-bindings.bash" in r2.log
        assert "curl https://raw.githubusercontent.com/junegunn/fzf/v0.74.3/shell/completion.bash" in r2.log
        assert bindings.is_file()
        assert (fake_system["home"] / ".fzf" / "shell" / "completion.bash").is_file()
        assert not any("fzf-" in l and l.endswith(".tar.gz") for l in r2.log)
        assert "fzf (upstream release" in r2.stdout

        # Run 3: complete, so nothing is fetched and fzf reads as present.
        r3 = _real_run(fake_system, UBUNTU, apt_fail="install:fzf")
        assert r3.returncode == 0, r3.stderr
        assert not any("fzf" in l for l in r3.log)
        assert re.search(r"^  already present: .*\bfzf\b", r3.stdout, re.MULTILINE)

    @pytest.mark.skipif(any(p.is_file() for p in SYSTEM_FZF_BINDINGS),
                        reason="a system fzf package's bindings would satisfy the check")
    def test_package_manager_success_that_leaves_fzf_incomplete_is_repaired(self, fake_system):
        """apt 'installs' fzf but ships no bindings (or reports an existing
        bare binary as installed): the post-install check catches it and the
        shell files come from upstream in the same run."""
        r = _real_run(fake_system, UBUNTU)
        assert r.returncode == 0, r.stderr + r.stdout
        assert any(l.endswith(" install --no-install-recommends fzf") for l in r.log)
        assert "reported fzf installed but fzf is still incomplete" in r.stderr
        assert (fake_system["home"] / ".fzf" / "shell" / "key-bindings.bash").is_file()
        assert not any("fzf-" in l and l.endswith(".tar.gz") for l in r.log), \
            "the binary apt installed must be kept, only the bindings fetched"

    def test_without_root_or_sudo_upstream_releases_go_to_home_bin(self, tmp_path):
        """The documented unprivileged mode: no package-manager calls at
        all, upstream releases into ~/bin, base tools reported missing."""
        sb = _make_sandbox(tmp_path, REAL_MODE_TOOLS, sudo=False)
        _write_exec(sb["bin"] / "apt-get", FAKE_APT_GET)
        _write_exec(sb["bin"] / "curl", FAKE_CURL)
        sb["log"] = tmp_path / "calls.log"
        sb["log"].touch()
        r = _real_run(sb, UBUNTU, bin_dir=False)
        assert r.returncode == 1
        assert "No root or sudo: package-manager steps are skipped" in r.stdout
        assert not any(l.startswith("apt-get") for l in r.log)
        home_bin = sb["home"] / "bin"
        for binary in ("starship", "rg", "jq", "fzf"):
            assert _is_exec(home_bin / binary), binary
        assert f"jq (upstream release -> {home_bin})" in r.stdout
        assert re.search(r"^NOT installed: .*\bgit\b.*\btmux\b", r.stderr, re.MULTILINE)
        assert "no root or sudo for the package manager" in r.stderr

    def test_nothing_touches_the_real_system(self, fake_system):
        """Belt and braces for the fixture itself: a full successful run
        writes only under the sandbox."""
        r = _real_run(fake_system, UBUNTU)
        assert r.returncode == 0, r.stderr + r.stdout
        written = {Path(l.split(" ", 1)[1]).name for l in r.log if l.startswith("curl ") and l.endswith(".tar.gz")}
        assert written <= {f"{n}" for n in (
            "starship-%s.tar.gz" % STARSHIP_TARGET,
            "ripgrep-9.9.9-%s-unknown-linux-musl.tar.gz" % MACHINE,
            "fd-v9.9.9-%s-unknown-linux-musl.tar.gz" % MACHINE,
            "bat-v9.9.9-%s-unknown-linux-musl.tar.gz" % MACHINE,
            "eza_x86_64-unknown-linux-musl.tar.gz", "eza_aarch64-unknown-linux-gnu.tar.gz",
            "gh_9.9.9_linux_%s.tar.gz" % GO_ARCH,
            "fzf-9.9.9-linux_%s.tar.gz" % GO_ARCH,
        )}, written
        assert not any(p.name.startswith("term-public-bootstrap.") for p in fake_system["tmp"].iterdir()), \
            "download scratch directories must be cleaned up"


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
