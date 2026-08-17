#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/start-shell-preview.sh
  scripts/start-shell-preview.sh --ghostty

Starts a fresh shell using this repo's bash/starship config without
needing to install it first. With `--ghostty`, launches a new Ghostty
window on macOS.
EOF
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="shell"
TARGET_CWD="$PWD"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ghostty)
      MODE="ghostty"
      shift
      ;;
    --shell-only)
      MODE="shell-only"
      shift
      ;;
    --cwd)
      TARGET_CWD="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Prefer Homebrew bash 5.x; /bin/bash 3.2 is a last resort for preview only.
pick_bash() {
  local b
  for b in /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash; do
    if [[ -x "$b" ]]; then
      echo "$b"
      return
    fi
  done
}

launch_shell() {
  local bash_bin rcfile
  bash_bin="$(pick_bash)"
  rcfile="$(mktemp /tmp/term-public-bashrc.XXXXXX)"

  cat > "$rcfile" <<EOF
export TERM_PUBLIC_ROOT="${REPO_ROOT}"
export STARSHIP_CONFIG="${REPO_ROOT}/starship/starship.toml"
source "${REPO_ROOT}/bash/bashrc"
EOF

  cd "$TARGET_CWD"

  env -i \
    HOME="$HOME" \
    USER="${USER:-$(id -un)}" \
    LOGNAME="${LOGNAME:-${USER:-$(id -un)}}" \
    SHELL="$bash_bin" \
    TERM="${TERM:-xterm-256color}" \
    LANG="${LANG:-en_US.UTF-8}" \
    LC_ALL="${LC_ALL:-en_US.UTF-8}" \
    PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin" \
    "$bash_bin" --rcfile "$rcfile" -i
}

if [[ "$MODE" == "shell-only" ]]; then
  launch_shell
elif [[ "$MODE" == "ghostty" ]]; then
  exec osascript - "$REPO_ROOT" "$TARGET_CWD" <<'APPLESCRIPT'
on run argv
  set repo_root to item 1 of argv
  set target_cwd to item 2 of argv

  tell application "Ghostty"
    activate

    set cfg to new surface configuration
    set initial working directory of cfg to target_cwd
    set command of cfg to "/bin/bash"
    set environment variables of cfg to {¬
      "TERM_PUBLIC_ROOT=" & repo_root}
    set initial input of cfg to "exec " & quoted form of (repo_root & "/scripts/start-shell-preview.sh") & " --shell-only --cwd " & quoted form of target_cwd & "\n"

    new window with configuration cfg
  end tell
end run
APPLESCRIPT
else
  launch_shell
fi
