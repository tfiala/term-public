#!/usr/bin/env bash
# term-public Linux bootstrap — the Linux counterpart of bootstrap-macos.sh.
#
# Supported: the RHEL 9 family (RHEL, Rocky, AlmaLinux, CentOS Stream; Fedora
# takes the same dnf path) and Ubuntu / Debian via apt.
#
# Linux already ships bash 5.x, so unlike macOS nothing replaces the system
# shell.  This script:
#   1. installs the prompt and tool stack (starship, tmux, fzf, ripgrep, fd,
#      bat, eza, gh, jq, git, bash-completion, python3 for hive), trying the
#      distribution's package manager FIRST for every tool and, only where
#      that fails or leaves the tool incomplete, an upstream release from the
#      project's GitHub releases;
#   2. makes sure the login shell is bash (chsh only when it is not);
#   3. reports what it could not install.
#
# Invariants:
#   - never a distribution upgrade (`dnf upgrade`, `apt-get upgrade`): a host
#     whose subscription lapsed or whose base repos are unreachable still gets
#     everything its reachable repos provide, and the upstream fallback covers
#     the rest;
#   - a failed package-index refresh is advisory, and one package per
#     package-manager call, so one unreachable repo or unavailable package
#     never aborts the rest;
#   - every tool is re-checked after the package manager reports success, so
#     a partial install (fzf's binary without its shell bindings, say) is
#     repaired on the next run instead of being skipped as present;
#   - without root or sudo the package-manager steps are skipped and the
#     upstream releases go to ~/bin (which bash/bashrc puts first on PATH);
#   - --dry-run prints every privileged and network command (prefixed "+",
#     release versions shown as <latest>) instead of running it, changes
#     nothing, and needs neither root nor sudo.
#
# Ghostty itself is not installed: a Linux host is set up as the SSH target of
# a Ghostty terminal.  ./setup.sh installs the xterm-ghostty terminfo so those
# sessions keep TERM=xterm-ghostty.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: setup/bootstrap-linux.sh [--dry-run] [--bin-dir DIR]

Installs the term-public shell stack on RHEL-family or Ubuntu/Debian hosts.
  --dry-run, -n   print the privileged and network commands instead of running them
  --bin-dir DIR   where upstream release binaries go
                  (default /usr/local/bin with root or sudo, else ~/bin)
  -h, --help      show this help
USAGE
}

DRY_RUN=0
BIN_DIR=""
while (( $# > 0 )); do
  case "$1" in
    --dry-run|-n) DRY_RUN=1 ;;
    --bin-dir)
      [[ $# -ge 2 ]] || { echo "bootstrap-linux: --bin-dir needs a directory" >&2; exit 2; }
      BIN_DIR="$2"; shift ;;
    --bin-dir=*) BIN_DIR="${1#--bin-dir=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "bootstrap-linux: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --- Host facts ---------------------------------------------------------------
# TERM_PUBLIC_OS_RELEASE lets the tests exercise each distribution's plan
# from any host.
OS_RELEASE="${TERM_PUBLIC_OS_RELEASE:-/etc/os-release}"
OS_ID="" OS_ID_LIKE="" OS_VERSION_ID="" OS_PRETTY=""
if [[ -r "$OS_RELEASE" ]]; then
  # os-release is shell-assignment syntax by specification.
  OS_ID="$(. "$OS_RELEASE" && printf '%s' "${ID:-}")"
  OS_ID_LIKE="$(. "$OS_RELEASE" && printf '%s' "${ID_LIKE:-}")"
  OS_VERSION_ID="$(. "$OS_RELEASE" && printf '%s' "${VERSION_ID:-}")"
  OS_PRETTY="$(. "$OS_RELEASE" && printf '%s' "${PRETTY_NAME:-${ID:-}}")"
fi

case " $OS_ID $OS_ID_LIKE " in
  *" rhel "*|*" fedora "*|*" centos "*|*" rocky "*|*" almalinux "*) FAMILY=dnf ;;
  *" ubuntu "*|*" debian "*) FAMILY=apt ;;
  *)
    echo "bootstrap-linux: unsupported distribution (ID=${OS_ID:-?} ID_LIKE=${OS_ID_LIKE:-?})." >&2
    echo "  Supported: the RHEL 9 family (rhel, rocky, almalinux, centos, fedora) and ubuntu/debian." >&2
    exit 1
    ;;
esac

ARCH="$(uname -m)"
case "$ARCH" in arm64) ARCH=aarch64 ;; esac   # macOS spelling, for dry runs
USER_NAME="${USER:-$(id -un)}"

# Privilege: root, sudo, or neither.  Neither is a supported mode — the
# package-manager steps are skipped and upstream releases go to ~/bin.
if (( EUID == 0 )); then
  SUDO="" PRIV_OK=1
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo" PRIV_OK=1
else
  SUDO="" PRIV_OK=0
fi

if (( PRIV_OK && ! DRY_RUN )); then
  case "$FAMILY" in
    dnf) command -v dnf >/dev/null 2>&1 || { echo "bootstrap-linux: dnf not found." >&2; exit 1; } ;;
    apt) command -v apt-get >/dev/null 2>&1 || { echo "bootstrap-linux: apt-get not found." >&2; exit 1; } ;;
  esac
fi

if [[ -z "$BIN_DIR" ]]; then
  if (( PRIV_OK )); then BIN_DIR=/usr/local/bin; else BIN_DIR="$HOME/bin"; fi
fi
# Binaries this run installs must be visible to its own presence checks
# (and to `fzf --version`), whether or not the caller's PATH has BIN_DIR yet.
export PATH="$BIN_DIR:$PATH"

# _priv CMD...
# Run a privileged command; under --dry-run print it instead.
_priv() {
  if (( DRY_RUN )); then
    printf '+ %s\n' "$*"
    return 0
  fi
  if [[ -n "$SUDO" ]]; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

# --- Package manager ----------------------------------------------------------
# Metadata refresh only.  Never `dnf upgrade` / `apt-get upgrade` here: this
# script installs a tool set, it does not maintain the host.  The caller
# treats a failure as advisory.
_pm_update() {
  (( PRIV_OK )) || return 0
  case "$FAMILY" in
    apt) _priv env DEBIAN_FRONTEND=noninteractive apt-get -qq update ;;
    dnf) : ;;  # dnf refreshes per-repo metadata on each install as needed
  esac
}

# _pm_install PKG — one package per call, so one failure stays one failure.
_pm_install() {
  if (( ! PRIV_OK )); then
    echo "  (no root or sudo: package manager skipped)"
    return 1
  fi
  case "$FAMILY" in
    dnf) _priv dnf -y -q install "$1" ;;
    apt) _priv env DEBIAN_FRONTEND=noninteractive \
           apt-get -qq -y -o Dpkg::Use-Pty=0 install --no-install-recommends "$1" ;;
  esac
}

# fzf, ripgrep, fd, bat and gh live in EPEL on the RHEL family.  Fedora
# carries them natively.
_ensure_epel() {
  [[ "$FAMILY" == dnf && "$OS_ID" != fedora ]] || return 0
  (( PRIV_OK )) || return 0
  if command -v rpm >/dev/null 2>&1 && rpm -q epel-release >/dev/null 2>&1; then
    return 0
  fi
  local major="${OS_VERSION_ID%%.*}"
  echo "==> EPEL"
  case "$OS_ID" in
    rhel) _pm_install "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${major:-9}.noarch.rpm" ;;
    *)    _pm_install epel-release ;;
  esac || echo "  warning: could not enable EPEL; the upstream release fallbacks cover what they can" >&2
}

# --- Upstream release fallbacks -----------------------------------------------
# Used only for a tool the package manager could not install or left
# incomplete.  Everything is fetched over HTTPS from the project's own GitHub
# release; where the project publishes a per-asset .sha256 (starship,
# ripgrep) the download is checked against it.  Under --dry-run each step
# prints the command it would run; release versions that would be resolved
# over the network appear as <latest>.

_fetch() {  # URL DEST
  if (( DRY_RUN )); then
    printf '+ curl -fsSL -o %s %s\n' "$2" "$1"
    return 0
  fi
  curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1"
}

# A missing checksum file is not an error (most projects publish none); a
# mismatch is.
_verify_if_published() {  # URL FILE
  local expected
  if (( DRY_RUN )); then
    printf '+ curl -fsSL -o %s.sha256 %s.sha256  (verify with sha256sum -c if published)\n' "$2" "$1"
    return 0
  fi
  if _fetch "$1.sha256" "$2.sha256" 2>/dev/null; then
    expected="$(awk 'NR == 1 { print $1 }' "$2.sha256")"
    printf '%s  %s\n' "$expected" "$2" | sha256sum -c --quiet - >/dev/null
  fi
}

# _latest_version OWNER/REPO — the tag GitHub redirects releases/latest to,
# without a leading v.  No API call, so no rate limit.
_latest_version() {
  local url
  if (( DRY_RUN )); then
    printf '%s\n' '<latest>'
    return 0
  fi
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' --retry 3 --connect-timeout 20 \
           "https://github.com/$1/releases/latest")" || return 1
  url="${url##*/}"
  printf '%s\n' "${url#v}"
}

_install_bin() {  # SRC NAME
  if (( DRY_RUN )); then
    printf '+ install -m 0755 %s %s\n' "$1" "$BIN_DIR/$2"
    return 0
  fi
  mkdir -p "$BIN_DIR" 2>/dev/null || true
  if [[ -d "$BIN_DIR" && -w "$BIN_DIR" ]]; then
    install -m 0755 "$1" "$BIN_DIR/$2"
  else
    _priv mkdir -p "$BIN_DIR" && _priv install -m 0755 "$1" "$BIN_DIR/$2"
  fi
}

# _release_tarball_bin URL BINARY
# Download a release tarball, verify it when a checksum is published, and
# install BINARY (found by name anywhere in the archive) into BIN_DIR.
_release_tarball_bin() {
  local url="$1" bin="$2" tmp found=""
  if (( DRY_RUN )); then
    _fetch "$url" "<tmp>/asset.tar.gz"
    _verify_if_published "$url" "<tmp>/asset.tar.gz"
    printf '+ tar -xzf <tmp>/asset.tar.gz -C <tmp>\n'
    _install_bin "<tmp>/$bin" "$bin"
    return 0
  fi
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/term-public-bootstrap.XXXXXX")"
  if _fetch "$url" "$tmp/asset.tar.gz" \
      && _verify_if_published "$url" "$tmp/asset.tar.gz" \
      && tar -xzf "$tmp/asset.tar.gz" -C "$tmp" \
      && found="$(find "$tmp" -type f -name "$bin" | head -n 1)" \
      && [[ -n "$found" ]] \
      && _install_bin "$found" "$bin"; then
    rm -rf "$tmp"
    return 0
  fi
  rm -rf "$tmp"
  return 1
}

# _release_file_bin URL BINARY — a bare-binary release asset (jq).
_release_file_bin() {
  local url="$1" bin="$2" tmp
  if (( DRY_RUN )); then
    _fetch "$url" "<tmp>/$bin"
    _verify_if_published "$url" "<tmp>/$bin"
    _install_bin "<tmp>/$bin" "$bin"
    return 0
  fi
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/term-public-bootstrap.XXXXXX")"
  if _fetch "$url" "$tmp/$bin" \
      && _verify_if_published "$url" "$tmp/$bin" \
      && _install_bin "$tmp/$bin" "$bin"; then
    rm -rf "$tmp"
    return 0
  fi
  rm -rf "$tmp"
  return 1
}

_no_release_for_arch() {
  echo "  no upstream release for $ARCH" >&2
  return 1
}

_manual_starship() {
  local t
  case "$ARCH" in
    x86_64) t=x86_64-unknown-linux-gnu ;;
    aarch64) t=aarch64-unknown-linux-musl ;;
    *) _no_release_for_arch; return 1 ;;
  esac
  _release_tarball_bin \
    "https://github.com/starship/starship/releases/latest/download/starship-${t}.tar.gz" starship
}

_manual_ripgrep() {
  local v
  case "$ARCH" in x86_64|aarch64) ;; *) _no_release_for_arch; return 1 ;; esac
  v="$(_latest_version BurntSushi/ripgrep)" || return 1
  _release_tarball_bin \
    "https://github.com/BurntSushi/ripgrep/releases/download/${v}/ripgrep-${v}-${ARCH}-unknown-linux-musl.tar.gz" rg
}

_manual_fd() {
  local v
  case "$ARCH" in x86_64|aarch64) ;; *) _no_release_for_arch; return 1 ;; esac
  v="$(_latest_version sharkdp/fd)" || return 1
  _release_tarball_bin \
    "https://github.com/sharkdp/fd/releases/download/v${v}/fd-v${v}-${ARCH}-unknown-linux-musl.tar.gz" fd
}

_manual_bat() {
  local v
  case "$ARCH" in x86_64|aarch64) ;; *) _no_release_for_arch; return 1 ;; esac
  v="$(_latest_version sharkdp/bat)" || return 1
  _release_tarball_bin \
    "https://github.com/sharkdp/bat/releases/download/v${v}/bat-v${v}-${ARCH}-unknown-linux-musl.tar.gz" bat
}

_manual_eza() {
  local t
  case "$ARCH" in
    x86_64) t=x86_64-unknown-linux-musl ;;
    aarch64) t=aarch64-unknown-linux-gnu ;;
    *) _no_release_for_arch; return 1 ;;
  esac
  _release_tarball_bin \
    "https://github.com/eza-community/eza/releases/latest/download/eza_${t}.tar.gz" eza
}

_manual_gh() {
  local v t
  case "$ARCH" in x86_64) t=amd64 ;; aarch64) t=arm64 ;; *) _no_release_for_arch; return 1 ;; esac
  v="$(_latest_version cli/cli)" || return 1
  _release_tarball_bin \
    "https://github.com/cli/cli/releases/download/v${v}/gh_${v}_linux_${t}.tar.gz" gh
}

_manual_jq() {
  local t
  case "$ARCH" in x86_64) t=amd64 ;; aarch64) t=arm64 ;; *) _no_release_for_arch; return 1 ;; esac
  _release_file_bin "https://github.com/jqlang/jq/releases/latest/download/jq-linux-${t}" jq
}

# fzf is two artifacts: the binary, and the shell bindings bash/bashrc
# sources for Ctrl-R / Ctrl-T / Alt-C.  Both distribution packages ship the
# bindings; a release tarball carries only the binary.  Each is checked and
# repaired independently, so a bare binary (from an earlier interrupted run,
# or installed by hand) gets its bindings on the next run instead of being
# skipped as present.

# The Linux locations bash/bashrc probes, in the same order.
FZF_BINDINGS_PATHS=(
  /usr/share/fzf/shell/key-bindings.bash
  /usr/share/doc/fzf/examples/key-bindings.bash
  "$HOME/.fzf/shell/key-bindings.bash"
)

_fzf_bindings_present() {
  local p
  for p in "${FZF_BINDINGS_PATHS[@]}"; do
    [[ -r "$p" ]] && return 0
  done
  return 1
}

_fzf_complete() {
  command -v fzf >/dev/null 2>&1 && _fzf_bindings_present
}

# Fetch the shell files at the tag matching the installed binary, at the
# path upstream's own installer uses (and bash/bashrc probes).
_fzf_shell_files() {
  local v=""
  if (( ! DRY_RUN )) && command -v fzf >/dev/null 2>&1; then
    v="$(fzf --version 2>/dev/null | awk '{ print $1 }')"
  fi
  [[ -n "$v" ]] || v="$(_latest_version junegunn/fzf)" || return 1
  (( DRY_RUN )) || mkdir -p "$HOME/.fzf/shell"
  _fetch "https://raw.githubusercontent.com/junegunn/fzf/v${v}/shell/key-bindings.bash" \
    "$HOME/.fzf/shell/key-bindings.bash" \
  && _fetch "https://raw.githubusercontent.com/junegunn/fzf/v${v}/shell/completion.bash" \
    "$HOME/.fzf/shell/completion.bash"
}

_manual_fzf() {
  local v t
  if (( DRY_RUN )) || ! command -v fzf >/dev/null 2>&1; then
    case "$ARCH" in x86_64) t=amd64 ;; aarch64) t=arm64 ;; *) _no_release_for_arch; return 1 ;; esac
    v="$(_latest_version junegunn/fzf)" || return 1
    _release_tarball_bin \
      "https://github.com/junegunn/fzf/releases/download/v${v}/fzf-${v}-linux_${t}.tar.gz" fzf || return 1
  fi
  if (( DRY_RUN )) || ! _fzf_bindings_present; then
    _fzf_shell_files
  fi
}

# --- Tool driver --------------------------------------------------------------
PRESENT=()
INSTALLED=()
MISSING=()

# _have SPEC — a command name, file:PATH, terminfo:NAME, or fn:FUNCTION.
_have() {
  case "$1" in
    file:*) [[ -e "${1#file:}" ]] ;;
    terminfo:*) command -v infocmp >/dev/null 2>&1 \
                  && TERMINFO= infocmp "${1#terminfo:}" >/dev/null 2>&1 ;;
    fn:*) "${1#fn:}" ;;
    *) command -v "$1" >/dev/null 2>&1 ;;
  esac
}

_present() {  # SPECS — any one present means the tool is complete
  local spec
  for spec in $1; do
    if _have "$spec"; then return 0; fi
  done
  return 1
}

# _tool NAME HAVE_SPECS DNF_PKG APT_PKG MANUAL_FN [UPSTREAM]
#   HAVE_SPECS  space-separated; any one present means the tool is complete
#   DNF_PKG / APT_PKG  "-" when that family does not package it
#   MANUAL_FN   "-" when there is no upstream release fallback
# Order is the contract: package manager first; the upstream release only
# when the package manager could not deliver, or reported success but the
# tool is still incomplete (the repair path).
_tool() {
  local name="$1" specs="$2" dnf_pkg="$3" apt_pkg="$4" manual="$5" upstream="${6:-}"
  local pkg=-
  if _present "$specs"; then
    PRESENT+=("$name")
    return 0
  fi
  echo "==> $name"
  case "$FAMILY" in
    dnf) pkg="$dnf_pkg" ;;
    apt) pkg="$apt_pkg" ;;
  esac
  if [[ "$pkg" != - ]]; then
    if _pm_install "$pkg"; then
      if (( DRY_RUN )); then
        INSTALLED+=("$name ($FAMILY: $pkg)")
        if [[ "$manual" != - ]]; then
          echo "  if $FAMILY cannot install it, the upstream release fallback (github.com/$upstream) would run:"
          "$manual" || true
        fi
        return 0
      fi
      if [[ "$manual" == - ]] || _present "$specs"; then
        INSTALLED+=("$name ($FAMILY: $pkg)")
        return 0
      fi
      echo "  $FAMILY reported $pkg installed but $name is still incomplete; repairing from the upstream release" >&2
    elif (( PRIV_OK )); then
      echo "  $FAMILY could not install $pkg" >&2
    fi
  fi
  if [[ "$manual" != - ]]; then
    if (( DRY_RUN )); then
      echo "  upstream release (github.com/$upstream) would run:"
      if "$manual"; then
        INSTALLED+=("$name (upstream release -> $BIN_DIR)")
        return 0
      fi
    else
      echo "  falling back to the upstream release (github.com/$upstream)"
      if "$manual" && _present "$specs"; then
        INSTALLED+=("$name (upstream release -> $BIN_DIR)")
        return 0
      fi
      echo "  upstream release install failed for $name" >&2
    fi
  fi
  MISSING+=("$name")
}

# Debian renames fd and bat; give them their upstream names in ~/bin (which
# bash/bashrc puts first on PATH) when nothing else provides them.
_alias_debian_names() {
  [[ "$FAMILY" == apt ]] || return 0
  local pair src dst
  for pair in fdfind:fd batcat:bat; do
    src="${pair%%:*}"
    dst="${pair##*:}"
    command -v "$src" >/dev/null 2>&1 || continue
    if command -v "$dst" >/dev/null 2>&1; then continue; fi
    if [[ -e "$HOME/bin/$dst" || -L "$HOME/bin/$dst" ]]; then continue; fi
    if (( DRY_RUN )); then
      echo "+ ln -s $(command -v "$src") $HOME/bin/$dst"
    else
      mkdir -p "$HOME/bin"
      ln -s "$(command -v "$src")" "$HOME/bin/$dst"
      echo "Linked $HOME/bin/$dst -> $src (Debian's name for it)"
    fi
  done
}

# --- Login shell --------------------------------------------------------------
_login_shell() {
  if command -v getent >/dev/null 2>&1; then
    getent passwd "$USER_NAME" | cut -d: -f7
  else
    printf '%s\n' "${SHELL:-}"
  fi
}

_ensure_bash_login_shell() {
  local current target=""
  current="$(_login_shell)"
  if [[ "$(basename -- "${current:-/none}")" == bash ]]; then
    echo "Login shell is already bash ($current)."
    return 0
  fi
  for target in /bin/bash /usr/bin/bash; do
    if [[ -x "$target" ]] && grep -qx "$target" /etc/shells 2>/dev/null; then
      break
    fi
    target=""
  done
  if [[ -z "$target" ]]; then
    echo "warning: no bash listed in /etc/shells; leaving the login shell as ${current:-unknown}" >&2
    return 0
  fi
  if ! command -v chsh >/dev/null 2>&1 && [[ "$FAMILY" == dnf ]]; then
    _pm_install util-linux-user || true  # RHEL ships chsh separately
  fi
  if ! command -v chsh >/dev/null 2>&1 && (( ! DRY_RUN )); then
    echo "warning: chsh not available; set the login shell to $target manually" >&2
    return 0
  fi
  echo "Changing login shell from ${current:-unknown} to $target (chsh may ask for your password)..."
  if (( DRY_RUN )); then
    echo "+ chsh -s $target"
  elif ! chsh -s "$target"; then
    echo "warning: chsh failed (directory-managed account?); set the login shell to $target manually" >&2
    return 0
  fi
  echo "Login shell is now bash. Your previous shell's config no longer runs:"
  echo "  - a prior ~/.bashrc / ~/.bash_profile is preserved as .bak and still"
  echo "    sourced by the term-public bash config after ./setup.sh"
  echo "  - zsh config (~/.zshenv, ~/.zshrc) is NOT read by bash; migrate any"
  echo "    needed exports to local/env.local and aliases to local/bashrc.local"
  echo "    (see README: Per-Machine Overlay)"
}

_report_bash() {
  local major
  if [[ ! -x /bin/bash ]]; then
    echo "bootstrap-linux: /bin/bash not found." >&2
    exit 1
  fi
  major="$(/bin/bash -c 'printf %s "${BASH_VERSINFO[0]}"')"
  echo "System bash: $(/bin/bash --version | head -n 1)"
  if (( major < 5 )); then
    echo "  warning: this baseline is developed against bash 5.x; ${major}.x mostly works but is untested." >&2
  fi
}

# --- Run ----------------------------------------------------------------------
if (( DRY_RUN )); then
  echo "term-public Linux bootstrap (dry run): ${OS_PRETTY:-$OS_ID} / $FAMILY / $ARCH"
  echo "Lines starting with '+' are the privileged or network commands a real run would execute;"
  echo "<latest> marks a release version resolved over the network at run time."
else
  echo "term-public Linux bootstrap: ${OS_PRETTY:-$OS_ID} / $FAMILY / $ARCH"
fi
if (( ! PRIV_OK )); then
  echo "No root or sudo: package-manager steps are skipped; upstream releases go to $BIN_DIR."
fi
_report_bash

_pm_update || echo "warning: package index refresh failed; continuing with the cached index and the upstream fallbacks" >&2
_ensure_epel

# NAME            HAVE                                            DNF             APT             MANUAL           UPSTREAM
_tool curl        "curl"                                          curl            curl            -
_tool tar         "tar"                                           tar             tar             -
_tool gzip        "gzip"                                          gzip            gzip            -
_tool ncurses     "tic"                                           ncurses         ncurses-bin     -
_tool tmux-terminfo "terminfo:tmux-256color"                      ncurses-term    ncurses-term    -
_tool bash-completion "file:/usr/share/bash-completion/bash_completion" bash-completion bash-completion -
_tool git         "git"                                           git             git             -
_tool python3     "python3"                                       python3         python3         -
_tool tmux        "tmux"                                          tmux            tmux            -
_tool jq          "jq"                                            jq              jq              _manual_jq       jqlang/jq
_tool fzf         "fn:_fzf_complete"                              fzf             fzf             _manual_fzf      junegunn/fzf
_tool ripgrep     "rg"                                            ripgrep         ripgrep         _manual_ripgrep  BurntSushi/ripgrep
_tool fd          "fd fdfind"                                     fd-find         fd-find         _manual_fd       sharkdp/fd
_tool bat         "bat batcat"                                    bat             bat             _manual_bat      sharkdp/bat
_tool eza         "eza"                                           eza             eza             _manual_eza      eza-community/eza
_tool gh          "gh"                                            gh              gh              _manual_gh       cli/cli
_tool starship    "starship"                                      starship        starship        _manual_starship starship/starship

_alias_debian_names
_ensure_bash_login_shell

echo
if (( DRY_RUN )); then
  echo "Dry run complete (${OS_PRETTY:-$OS_ID}, $FAMILY). Nothing was changed."
else
  echo "Bootstrap complete (${OS_PRETTY:-$OS_ID}, $FAMILY)."
fi
if (( ${#PRESENT[@]} > 0 )); then
  echo "  already present: ${PRESENT[*]}"
fi
if (( ${#INSTALLED[@]} > 0 )); then
  echo "  installed:"
  printf '    %s\n' "${INSTALLED[@]}"
fi
echo "Ghostty is not installed on Linux by this script: this host is the SSH target"
echo "of a Ghostty terminal, and ./setup.sh installs the xterm-ghostty terminfo."
echo "Next: ./setup.sh"
if (( ${#MISSING[@]} > 0 )); then
  if (( DRY_RUN )); then
    # A plan is not a failure: report what a real run would leave behind.
    echo "A real run would end with NOT installed: ${MISSING[*]}"
    exit 0
  fi
  echo "NOT installed: ${MISSING[*]}" >&2
  if (( PRIV_OK )); then
    echo "  ($FAMILY had no reachable package and no upstream release fallback applied;" >&2
  else
    echo "  (no root or sudo for the package manager and no upstream release fallback applied;" >&2
  fi
  echo "  install these by hand, then rerun this script — it skips what is present.)" >&2
  exit 1
fi
