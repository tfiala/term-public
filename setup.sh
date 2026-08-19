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
      # silently freezes while the repo moves on. No backup needed —
      # the content is identical.
      rm -f "$2"
      ln -s "$1" "$2"
    fi
  else
    ln -s "$1" "$2"
  fi
}

# backup_and_copy_file
# $1 - source path
# $2 - destination path
# Executables installed into ~/bin are copies, never symlinks: other
# tooling also installs them by copying (infra/home-dc
# scripts/copy-scripts.py fetches hive from this repo's main), and a
# symlink lets that write travel through into this checkout's scripts/,
# overwriting the canonical source (seen 2026-08-16, when a copy of main
# clobbered an in-review branch). The cost is that a copy goes stale
# until the next setup.sh run — accepted for ~/bin; dotfiles stay
# symlinked via backup_and_link_file above.
backup_and_copy_file() {
  mkdir -p "$(dirname "$2")"

  if [[ -L "$2" ]]; then
    if [[ -e "$2" ]] && ! [[ "$2" -ef "$1" ]]; then
      # Live link to something else — preserve it; the link itself
      # moves, so the backup keeps pointing at the user's real file.
      rm -rf "$2.bak"
      mv "$2" "$2.bak"
    else
      # A link resolving to our own source (the legacy install shape,
      # in any spelling) preserves nothing worth backing up, and a
      # dangling link preserves nothing at all. Remove either.
      unlink "$2"
    fi
  elif [[ -e "$2" ]]; then
    if [[ "$1" -ef "$2" ]]; then
      # A hard link to the source is another write-through alias — and
      # backing it up would keep the shared inode alive under a new
      # name. Remove it and copy fresh; the content is our own.
      rm -f "$2"
    elif [[ ! -d "$2" ]] && cmp -s "$1" "$2"; then
      # Same content already installed — still normalize the executable
      # bit, which a bare copy may have lost.
      chmod u+x "$2"
      return 0
    else
      rm -rf "$2.bak"
      mv "$2" "$2.bak"
    fi
  fi
  cp "$1" "$2"
  chmod u+x "$2"
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

# _remember_linked_checkout
# $1 - installed symlink that setup.sh is about to replace
# $2 - repo-relative suffix of that link's target
# Records a different checkout only when its normalized git origin matches
# this checkout. Relative links are resolved from the link's directory.
_remember_linked_checkout() {
  local link="$1" suffix="$2" target root this_id target_id existing
  [[ -L "$link" ]] || return 0
  target="$(readlink "$link")"
  case "$target" in
    */"$suffix") ;;
    *) return 0 ;;
  esac
  case "$target" in
    /*) ;;
    *) target="$(dirname "$link")/$target" ;;
  esac
  root="${target%/"$suffix"}"
  root="$(cd "$root" 2>/dev/null && pwd -P)" || return 0
  [[ "$root" != "$ROOT_DIR" ]] || return 0
  this_id="$(_repo_identity "$ROOT_DIR")"
  target_id="$(_repo_identity "$root")"
  [[ -n "$this_id" && "$target_id" == "$this_id" ]] || return 0
  # The fallback keeps an empty indexed array safe under macOS bash 3.2
  # with nounset enabled.
  for existing in "${OLD_CHECKOUT_ROOTS[@]-}"; do
    [[ -n "$existing" ]] || continue
    [[ "$existing" != "$root" ]] || return 0
  done
  OLD_CHECKOUT_ROOTS[${#OLD_CHECKOUT_ROOTS[@]}]="$root"
}

# _overlay_source_is_pristine
# $1 - source checkout, $2 - repo-relative overlay path
# A source is pristine when it matches that checkout's template. Checkouts
# from before local/*.template existed fall back to this checkout's template.
_overlay_source_is_pristine() {
  local old_root="$1" relative="$2" source_template new_template
  source_template="$old_root/$relative.template"
  new_template="$ROOT_DIR/$relative.template"
  if [[ -f "$source_template" ]]; then
    cmp -s "$old_root/$relative" "$source_template"
  elif [[ -f "$new_template" ]]; then
    cmp -s "$old_root/$relative" "$new_template"
  else
    return 1
  fi
}

# _overlay_destination_is_pristine
# $1 - repo-relative overlay path
# Missing destinations and regular files equal to the current template are
# safe migration targets. Symlinks are always user-owned destinations.
_overlay_destination_is_pristine() {
  local relative="$1" destination template
  destination="$ROOT_DIR/$relative"
  template="$ROOT_DIR/$relative.template"
  if [[ ! -e "$destination" && ! -L "$destination" ]]; then
    return 0
  fi
  [[ ! -L "$destination" && -f "$template" && -f "$destination" ]] \
    && cmp -s "$destination" "$template"
}

# _migrate_overlay_file
# $1 - source checkout, $2 - repo-relative overlay path
# Copies a customized source only over an absent/pristine destination. A
# different customized destination is preserved and reported without exposing
# either file's contents.
_migrate_overlay_file() {
  local old_root="$1" relative="$2" source destination
  source="$old_root/$relative"
  destination="$ROOT_DIR/$relative"
  [[ -f "$source" || -L "$source" ]] || return 0
  _overlay_source_is_pristine "$old_root" "$relative" && return 0
  if [[ -e "$destination" || -L "$destination" ]] \
      && cmp -s "$source" "$destination"; then
    return 0
  fi
  if _overlay_destination_is_pristine "$relative"; then
    mkdir -p "$(dirname "$destination")" || return 1
    rm -f "$destination" || return 1
    cp -pP "$source" "$destination" || return 1
    echo "Migrated machine-local overlay: $relative (from $old_root)"
  else
    OVERLAY_CONFLICT_COUNT=$((OVERLAY_CONFLICT_COUNT + 1))
    echo "WARNING: machine-local overlay differs in both checkouts: $relative" >&2
    echo "  Preserved $destination" >&2
    echo "  Review the prior copy at $source and merge it manually." >&2
  fi
}

# migrate_checkout_overlay
# $1 - verified prior checkout root
# Migrates Ghostty's overlay plus all non-template files under local/.
migrate_checkout_overlay() {
  local old_root="$1" source relative manifest
  _migrate_overlay_file "$old_root" "ghostty/local.config"
  [[ -d "$old_root/local" ]] || return 0
  manifest="$(mktemp "${TMPDIR:-/tmp}/term-public-overlay.XXXXXX")"
  if ! find "$old_root/local" \( -type f -o -type l \) -print0 > "$manifest"; then
    rm -f "$manifest"
    echo "ERROR: could not enumerate the prior machine-local overlay at $old_root/local" >&2
    return 1
  fi
  while IFS= read -r -d '' source; do
    relative="local/${source#"$old_root/local/"}"
    case "$relative" in
      *.template) continue ;;
    esac
    if ! _migrate_overlay_file "$old_root" "$relative"; then
      rm -f "$manifest"
      return 1
    fi
  done < "$manifest"
  rm -f "$manifest"
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

ROOT_DIR="$(pwd -P)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
LOCAL_DIR="$ROOT_DIR/local"
OLD_CHECKOUT_ROOTS=()
OVERLAY_CONFLICT_COUNT=0
OVERLAY_CONFLICT_EXIT_STATUS=3

mkdir -p "$HOME/bin"
mkdir -p "$LOCAL_DIR/bin"

# One definition drives both prior-checkout discovery and link installation,
# keeping each external path binding exact.
_REPO_LINK_RELATIVES=(
  "ghostty"
  "bash/bash_profile"
  "bash/bashrc"
  "bash/inputrc"
  "starship/starship.toml"
  "tmux/tmux.conf"
  "tmux/tmux.conf"
)
_REPO_LINK_DESTINATIONS=(
  "$CONFIG_HOME/ghostty"
  "$HOME/.bash_profile"
  "$HOME/.bashrc"
  "$HOME/.inputrc"
  "$CONFIG_HOME/starship.toml"
  "$HOME/.tmux/tmux.conf"
  "$HOME/.tmux.conf"
)

_link_index=0
while (( _link_index < ${#_REPO_LINK_RELATIVES[@]} )); do
  _remember_linked_checkout \
    "${_REPO_LINK_DESTINATIONS[$_link_index]}" \
    "${_REPO_LINK_RELATIVES[$_link_index]}"
  _link_index=$((_link_index + 1))
done

for _old_checkout in "${OLD_CHECKOUT_ROOTS[@]-}"; do
  [[ -n "$_old_checkout" ]] || continue
  migrate_checkout_overlay "$_old_checkout"
done

if (( OVERLAY_CONFLICT_COUNT > 0 )); then
  echo "WARNING: $OVERLAY_CONFLICT_COUNT machine-local overlay conflict(s) require manual review." >&2
  echo "  Installed links were not changed; rerun setup.sh after reconciling the files above." >&2
  exit "$OVERLAY_CONFLICT_EXIT_STATUS"
fi

if [[ ! -f "$LOCAL_DIR/env.local" ]]; then
  cp "$LOCAL_DIR/env.local.template" "$LOCAL_DIR/env.local"
fi

if [[ ! -f "$LOCAL_DIR/bashrc.local" ]]; then
  cp "$LOCAL_DIR/bashrc.local.template" "$LOCAL_DIR/bashrc.local"
fi

_link_index=0
while (( _link_index < ${#_REPO_LINK_RELATIVES[@]} )); do
  backup_and_link_file \
    "$ROOT_DIR/${_REPO_LINK_RELATIVES[$_link_index]}" \
    "${_REPO_LINK_DESTINATIONS[$_link_index]}"
  _link_index=$((_link_index + 1))
done

backup_and_copy_file "$ROOT_DIR/scripts/hive.py" "$HOME/bin/hive"
backup_and_copy_file "$ROOT_DIR/scripts/hive-ci-popup.py" "$HOME/bin/hive-ci-popup"
backup_and_copy_file "$ROOT_DIR/scripts/term-theme" "$HOME/bin/term-theme"

# Clean up links from the zsh era (removed in the bash cutover, #15).
remove_stale_link "$HOME/.zshenv" "zsh/zshenv"
remove_stale_link "$HOME/.zshrc" "zsh/zshrc"
remove_stale_link "$HOME/.p10k.zsh" "p10k.zsh"

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
