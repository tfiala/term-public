#!/usr/bin/env bash
set -euo pipefail

# backup_and_link_file
# $1 - source path
# $2 - destination path
# Destination symlinks are classified: already-correct links are left
# alone, dangling links are replaced (ln -s onto one fails and there is
# nothing to preserve), and live links to anything else are preserved as
# "$2.bak" — the link itself moves, so the backup keeps pointing into
# the user's other dotfiles repo and stays sourceable.
backup_and_link_file() {
  mkdir -p "$(dirname "$2")"

  if [[ -L "$2" ]]; then
    if [[ "$(readlink "$2")" == "$1" ]]; then
      return 0
    elif [[ ! -e "$2" ]]; then
      unlink "$2"
      ln -s "$1" "$2"
    else
      rm -rf "$2.bak"
      mv "$2" "$2.bak"
      ln -s "$1" "$2"
    fi
  elif [[ -e "$2" ]]; then
    if [[ -d "$2" ]] || ! cmp -s "$1" "$2"; then
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

# _repo_identity
# $1 - path to a checkout root
# Prints the checkout's git origin normalized to host/owner/repo, or
# nothing when there is no repo or no origin.  Normalization covers the
# scp (git@host:o/r), ssh://, and https:// forms, with or without .git.
_repo_identity() {
  local url
  url="$(git -C "$1" remote get-url origin 2>/dev/null || true)"
  [[ -n "$url" ]] || return 0
  url="${url%/}"
  url="${url%.git}"
  url="${url#ssh://}"
  url="${url#git://}"
  url="${url#https://}"
  url="${url#http://}"
  url="${url#*@}"
  url="${url/://}"
  printf '%s\n' "$url"
}

# remove_stale_link
# $1 - home dotfile that may be a symlink from the zsh era
# $2 - repo-relative suffix the old link pointed at
# Only removes a link whose target checkout has the same normalized git
# origin as this checkout — repository identity, not a path shape any
# dotfiles repo could contain.  Anything else — foreign repos, dangling
# links to a deleted checkout, or no provable origin on either side —
# is left in place with an actionable warning.
remove_stale_link() {
  local link="$1" suffix="$2" target root this_id target_id
  [[ -L "$link" ]] || return 0
  target="$(readlink "$link")"
  case "$target" in
    */"$suffix") ;;
    *) return 0 ;;
  esac
  # A relative symlink target resolves from the link's directory, not
  # from this process's cwd — resolving from cwd would let identity be
  # read off whatever happens to sit at the cwd-relative path.
  case "$target" in
    /*) ;;
    *) target="$(dirname "$link")/$target" ;;
  esac
  root="${target%/"$suffix"}"
  this_id="$(_repo_identity "$ROOT_DIR")"
  target_id="$(_repo_identity "$root")"
  if [[ -n "$this_id" && "$target_id" == "$this_id" ]]; then
    unlink "$link"
    if [[ -e "$link.bak" ]]; then
      mv "$link.bak" "$link"
    fi
  else
    echo "warning: $link -> $target looks like a zsh-era term-public link," >&2
    echo "  but its checkout's git origin could not be verified as this repository." >&2
    echo "  Leaving it in place; remove it manually if it belonged to term-public." >&2
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
      if TERMINFO="$_ghostty_ti" infocmp -x xterm-ghostty 2>/dev/null \
          | TERMINFO= tic -x - 2>/dev/null; then
        echo "Installed xterm-ghostty terminfo to ~/.terminfo"
      fi
    fi
  fi
fi

# bash cannot safely source zsh syntax, so leftover zsh config (including
# .bak files restored above) no longer runs after the chsh to bash.
# Point at the sanctioned overlay instead of silently dropping it.
_zsh_leftovers=""
for _f in "$HOME/.zshenv" "$HOME/.zshrc" "$HOME/.zshenv.bak" "$HOME/.zshrc.bak"; do
  if [[ -f "$_f" ]]; then
    _zsh_leftovers="$_zsh_leftovers $_f"
  fi
done
if [[ -n "$_zsh_leftovers" ]]; then
  echo "NOTE: zsh-era config remains at:$_zsh_leftovers"
  echo "      bash does not read these files. Migrate needed exports into"
  echo "      $LOCAL_DIR/env.local and aliases/functions into"
  echo "      $LOCAL_DIR/bashrc.local, then delete them."
fi

# Offer the one-shot history import while zsh history is still around and
# the import has not already run (the importer leaves the .zsh-import.bak
# backup behind as its done-marker).
if [[ -f "$HOME/.zsh_history" && ! -f "$HOME/.bash_history.zsh-import.bak" ]]; then
  echo "NOTE: zsh command history found at $HOME/.zsh_history."
  echo "      Import it into bash history with:"
  echo "      $ROOT_DIR/scripts/import-zsh-history.py"
fi

echo "Linked config into place."
echo "Local machine overlay is available in $LOCAL_DIR"
