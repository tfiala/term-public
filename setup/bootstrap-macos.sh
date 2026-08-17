#!/usr/bin/env bash
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required." >&2
  exit 1
fi

# bash: macOS ships 3.2 (2007); the daily shell is Homebrew bash 5.x.
brew install bash bash-completion@2 starship
brew install git gh jq ripgrep fd fzf bat eza tmux
brew install --cask ghostty

BREW_BASH="$(brew --prefix)/bin/bash"
if [[ ! -x "$BREW_BASH" ]]; then
  echo "Expected Homebrew bash at $BREW_BASH; not found." >&2
  exit 1
fi

if ! grep -qx "$BREW_BASH" /etc/shells; then
  echo "Registering $BREW_BASH in /etc/shells (sudo)..."
  echo "$BREW_BASH" | sudo tee -a /etc/shells >/dev/null
fi

current_shell="$(dscl . -read "/Users/$USER" UserShell 2>/dev/null | awk '{print $2}')"
if [[ "$current_shell" != "$BREW_BASH" ]]; then
  echo "Changing login shell to $BREW_BASH..."
  chsh -s "$BREW_BASH"
  echo "Login shell is now bash. Your previous shell's config no longer runs:"
  echo "  - prior ~/.bashrc / ~/.bash_profile are preserved as .bak and still"
  echo "    sourced by the term-public bash config after ./setup.sh"
  echo "  - zsh config (~/.zshenv, ~/.zshrc) is NOT read by bash; migrate any"
  echo "    needed exports to local/env.local and aliases to local/bashrc.local"
  echo "    (see README: Per-Machine Overlay)"
fi

echo "Bootstrap complete."
