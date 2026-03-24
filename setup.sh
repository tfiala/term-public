#!/usr/bin/env zsh

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
    fi
  else
    ln -s "$1" "$2"
  fi
}

ROOT_DIR="$(pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
LOCAL_DIR="$ROOT_DIR/local"

mkdir -p "$HOME/bin"
mkdir -p "$LOCAL_DIR/bin"

backup_and_link_file "$ROOT_DIR/ghostty" "$CONFIG_HOME/ghostty"
backup_and_link_file "$ROOT_DIR/zsh/zshrc" "$HOME/.zshrc"
backup_and_link_file "$ROOT_DIR/p10k.zsh" "$HOME/.p10k.zsh"
backup_and_link_file "$ROOT_DIR/scripts/hive.py" "$HOME/bin/hive"
backup_and_link_file "$ROOT_DIR/zsh/hive-shell-prompt.zsh" "$HOME/bin/hive-shell-prompt.zsh"

if [[ ! -f "$LOCAL_DIR/env.local" ]]; then
  cat > "$LOCAL_DIR/env.local" <<'EOF'
# Per-machine environment overrides for term-public.
# Examples:
# export PATH="$HOME/.local/node/bin:$PATH"
# export K3S_DEV_ROOT="$HOME/src/k3s"
EOF
fi

if [[ ! -f "$LOCAL_DIR/zshrc.local" ]]; then
  cat > "$LOCAL_DIR/zshrc.local" <<'EOF'
# Per-machine zsh customizations for term-public.
# Examples:
# alias kpods='kubectl get pods -A'
# source "$HOME/.config/some-tool/init.zsh"
EOF
fi

echo "Linked config into place."
echo "Local machine overlay is available in $LOCAL_DIR"
