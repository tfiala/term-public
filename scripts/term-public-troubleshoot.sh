#!/usr/bin/env bash
set -euo pipefail

echo '--- shell ---'
echo "SHELL=${SHELL:-<unset>}"
echo "ZDOTDIR=${ZDOTDIR:-<unset>}"
echo "TERM_PUBLIC_ROOT=${TERM_PUBLIC_ROOT:-<unset>}"
readlink ~/.zshrc 2>/dev/null || echo "~/.zshrc not symlink"
readlink ~/.p10k.zsh 2>/dev/null || echo "~/.p10k.zsh not symlink"

echo '--- files ---'
ls -ld ~/.oh-my-zsh ~/.oh-my-zsh/custom/themes/powerlevel10k 2>/dev/null || true
ls -ld ~/.p10k.zsh ~/.zshrc 2>/dev/null || true

echo '--- prompt deps ---'
command -v zsh
command -v git
command -v python3
command -v ghostty || true

echo '--- env ---'
zsh -i -c 'echo ZSH=$ZSH; echo ZSH_THEME=$ZSH_THEME; typeset -p POWERLEVEL9K_MODE 2>/dev/null || echo no-p10k'
