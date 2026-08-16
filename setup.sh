#!/usr/bin/env bash

# backup_and_link_file
# $1 - source path
# $2 - destination path
backup_and_link_file() {
  mkdir -p "$(dirname "$2")"

  if [[ -e "$2" ]]; then
    if [[ -L "$2" ]]; then
      unlink "$2"
      ln -s "$1" "$2"
    elif [[ -d "$2" ]] || ! cmp -s "$1" "$2"; then
      rm -rf "$2.bak"
      mv "$2" "$2.bak"
      ln -s "$1" "$2"
    else
      # An identical regular file must still become a symlink, or it
      # silently freezes while the repo moves on (a copied ~/bin/hive sat
      # stale from May while scripts/hive.py advanced). No backup needed —
      # the content is identical.
      rm -f "$2"
      ln -s "$1" "$2"
    fi
  else
    ln -s "$1" "$2"
  fi
}

# remove_stale_link
# $1 - home dotfile that may be a symlink from the zsh era
# $2 - repo-relative suffix the old link pointed at
# Unlinks it (from any term-public checkout, including deleted files)
# and restores the pre-term-public backup if one exists.
remove_stale_link() {
  if [[ -L "$1" ]]; then
    case "$(readlink "$1")" in
      */"$2")
        unlink "$1"
        if [[ -e "$1.bak" ]]; then
          mv "$1.bak" "$1"
        fi
        ;;
    esac
  fi
}

ROOT_DIR="$(pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
LOCAL_DIR="$ROOT_DIR/local"

mkdir -p "$HOME/bin"
mkdir -p "$LOCAL_DIR/bin"

backup_and_link_file "$ROOT_DIR/ghostty" "$CONFIG_HOME/ghostty"
backup_and_link_file "$ROOT_DIR/bash/bash_profile" "$HOME/.bash_profile"
backup_and_link_file "$ROOT_DIR/bash/bashrc" "$HOME/.bashrc"
backup_and_link_file "$ROOT_DIR/bash/inputrc" "$HOME/.inputrc"
backup_and_link_file "$ROOT_DIR/starship/starship.toml" "$CONFIG_HOME/starship.toml"
backup_and_link_file "$ROOT_DIR/scripts/hive.py" "$HOME/bin/hive"
backup_and_link_file "$ROOT_DIR/scripts/hive-ci-popup.py" "$HOME/bin/hive-ci-popup"
backup_and_link_file "$ROOT_DIR/scripts/term-theme" "$HOME/bin/term-theme"
backup_and_link_file "$ROOT_DIR/tmux/tmux.conf" "$HOME/.tmux/tmux.conf"
backup_and_link_file "$ROOT_DIR/tmux/tmux.conf" "$HOME/.tmux.conf"

# Clean up links from the zsh era (removed in the bash cutover, #15).
remove_stale_link "$HOME/.zshenv" "zsh/zshenv"
remove_stale_link "$HOME/.zshrc" "zsh/zshrc"
remove_stale_link "$HOME/.p10k.zsh" "p10k.zsh"

if [[ ! -f "$LOCAL_DIR/env.local" ]]; then
  cat > "$LOCAL_DIR/env.local" <<'EOF'
# Per-machine environment overrides for term-public.
# Examples:
# export PATH="$HOME/.local/node/bin:$PATH"
# export K3S_DEV_ROOT="$HOME/src/k3s"
EOF
fi

if [[ ! -f "$LOCAL_DIR/bashrc.local" ]]; then
  cat > "$LOCAL_DIR/bashrc.local" <<'EOF'
# Per-machine bash customizations for term-public.
# Examples:
# alias kpods='kubectl get pods -A'
# source "$HOME/.config/some-tool/init.bash"
EOF
fi

# Install Ghostty terminfo into ~/.terminfo so shells and other programs
# can find xterm-ghostty without TERMINFO being set.
if command -v infocmp >/dev/null 2>&1 && command -v tic >/dev/null 2>&1; then
  if ! TERMINFO= infocmp xterm-ghostty >/dev/null 2>&1; then
    _ghostty_ti="/Applications/Ghostty.app/Contents/Resources/terminfo"
    if [[ -d "$_ghostty_ti" ]]; then
      TERMINFO="$_ghostty_ti" infocmp -x xterm-ghostty 2>/dev/null | TERMINFO= tic -x - 2>/dev/null \
        && echo "Installed xterm-ghostty terminfo to ~/.terminfo"
    fi
  fi
fi

echo "Linked config into place."
echo "Local machine overlay is available in $LOCAL_DIR"
