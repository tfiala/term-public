#!/usr/bin/env bash
set -euo pipefail

echo '--- shell ---'
echo "SHELL=${SHELL:-<unset>}"
echo "BASH_VERSION=${BASH_VERSION:-<unset>}"
echo "TERM_PUBLIC_ROOT=${TERM_PUBLIC_ROOT:-<unset>}"
readlink ~/.bash_profile 2>/dev/null || echo "~/.bash_profile not symlink"
readlink ~/.bashrc 2>/dev/null || echo "~/.bashrc not symlink"
readlink ~/.inputrc 2>/dev/null || echo "~/.inputrc not symlink"
readlink "${XDG_CONFIG_HOME:-$HOME/.config}/starship.toml" 2>/dev/null \
  || echo "starship.toml not symlink"

echo '--- login shell registration ---'
for b in /opt/homebrew/bin/bash /usr/local/bin/bash; do
  if [[ -x "$b" ]]; then
    echo "$b: $("$b" --version | head -1)"
    grep -qx "$b" /etc/shells && echo "$b in /etc/shells" \
      || echo "$b NOT in /etc/shells (run setup/bootstrap-macos.sh)"
  fi
done
dscl . -read "/Users/$USER" UserShell 2>/dev/null || true

echo '--- prompt deps ---'
command -v bash
command -v starship || echo "starship missing (run setup/bootstrap-macos.sh)"
command -v git
command -v python3
command -v ghostty || true

echo '--- env ---'
bash -i -c 'echo "TERM_PUBLIC_ROOT=$TERM_PUBLIC_ROOT"; echo "PROMPT_COMMAND=${PROMPT_COMMAND:-<unset>}"' 2>/dev/null
