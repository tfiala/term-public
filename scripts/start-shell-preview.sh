#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/start-shell-preview.sh
  scripts/start-shell-preview.sh --ghostty

Starts a fresh shell using this repo's zsh/p10k config without needing to
install it first. With `--ghostty`, launches a new Ghostty window on macOS.
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

launch_shell() {
  local wrapper_dir
  wrapper_dir="$(mktemp -d /tmp/term-public-zdotdir.XXXXXX)"

  cat > "${wrapper_dir}/.zshenv" <<EOF
export TERM_PUBLIC_P10K="${REPO_ROOT}/p10k.zsh"
export TERM_PUBLIC_ROOT="${REPO_ROOT}"
export ZDOTDIR="${wrapper_dir}"
EOF

  cat > "${wrapper_dir}/.zshrc" <<EOF
source "${REPO_ROOT}/zsh/zshrc"
EOF

  cd "$TARGET_CWD"

  env -i \
    HOME="$HOME" \
    USER="${USER:-$(id -un)}" \
    LOGNAME="${LOGNAME:-${USER:-$(id -un)}}" \
    SHELL="${SHELL:-/bin/zsh}" \
    TERM="${TERM:-xterm-256color}" \
    LANG="${LANG:-en_US.UTF-8}" \
    LC_ALL="${LC_ALL:-en_US.UTF-8}" \
    PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/bin" \
    ZDOTDIR="${wrapper_dir}" \
    TERM_PUBLIC_P10K="${REPO_ROOT}/p10k.zsh" \
    TERM_PUBLIC_ROOT="${REPO_ROOT}" \
    /bin/zsh -l
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
    set command of cfg to "/bin/zsh"
    set environment variables of cfg to {¬
      "TERM_PUBLIC_ROOT=" & repo_root, ¬
      "TERM_PUBLIC_P10K=" & repo_root & "/p10k.zsh"}
    set initial input of cfg to "exec " & quoted form of (repo_root & "/scripts/start-shell-preview.sh") & " --shell-only --cwd " & quoted form of target_cwd & "\n"

    new window with configuration cfg
  end tell
end run
APPLESCRIPT
else
  launch_shell
fi
