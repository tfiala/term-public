#!/usr/bin/env bash
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required." >&2
  exit 1
fi

brew install git gh jq ripgrep fd fzf bat eza dtach
brew install --cask ghostty

if [[ ! -d "$HOME/.oh-my-zsh" ]]; then
  git clone https://github.com/ohmyzsh/ohmyzsh.git "$HOME/.oh-my-zsh"
fi

ZSH_CUSTOM="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}"
if [[ ! -d "$ZSH_CUSTOM/themes/powerlevel10k" ]]; then
  git clone https://github.com/romkatv/powerlevel10k.git \
    "$ZSH_CUSTOM/themes/powerlevel10k"
fi

echo "Bootstrap complete."
