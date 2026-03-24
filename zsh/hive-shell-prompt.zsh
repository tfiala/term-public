# hive-shell-prompt.zsh
#
# Sourced only inside `hive shell` sessions.

[[ -z "$HIVE_NAME" ]] && return

rm -f "${XDG_CACHE_HOME:-$HOME/.cache}"/p10k-instant-prompt-*.zsh.zwc 2>/dev/null

_hive_last_branch=""
_hive_pr_cache="/tmp/hive-dtach/${HIVE_NAME}-${HIVE_NUMBER}.pr"

_hive_git_branch() {
  git rev-parse --abbrev-ref HEAD 2>/dev/null
}

_hive_fetch_pr() {
  local branch="$1"
  local pr_json num

  command -v fj >/dev/null 2>&1 || return

  pr_json=$(fj pr list --json --state open --head "$branch" 2>/dev/null)
  if [[ -n "$pr_json" ]]; then
    num=$(printf '%s' "$pr_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if data:
    print(data[0].get("number", ""))
' 2>/dev/null)
    [[ -n "$num" ]] && printf '%s' "$num" > "$_hive_pr_cache" || : > "$_hive_pr_cache"
  else
    : > "$_hive_pr_cache"
  fi
}

_hive_lookup_pr() {
  local branch="$1"
  [[ "$branch" == "$_hive_last_branch" ]] && return

  _hive_last_branch="$branch"
  : > "$_hive_pr_cache"

  local default_branch
  default_branch=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null)
  default_branch="${default_branch##refs/remotes/origin/}"
  [[ -z "$default_branch" ]] && default_branch="main"
  [[ "$branch" == "$default_branch" ]] && return

  _hive_fetch_pr "$branch" &!
}

_hive_bg_fetch() {
  local toplevel fetch_key fetch_marker now last_fetch
  toplevel=$(git rev-parse --show-toplevel 2>/dev/null) || return
  mkdir -p /tmp/hive-dtach
  fetch_key=$(printf '%s' "$toplevel" | tr '/' '_')
  fetch_marker="/tmp/hive-dtach/.fetch${fetch_key}"

  now=$(date +%s)
  last_fetch=0
  [[ -f "$fetch_marker" ]] && last_fetch=$(<"$fetch_marker" 2>/dev/null)

  if (( now - last_fetch > 120 )); then
    printf '%s' "$now" > "$fetch_marker"
    git -C "$toplevel" fetch --quiet >/dev/null 2>&1 &!
  fi
}

_hive_precmd() {
  local branch title pr_num=""
  branch=$(_hive_git_branch)
  if [[ -n "$branch" ]]; then
    _hive_bg_fetch
    _hive_lookup_pr "$branch"
    [[ -f "$_hive_pr_cache" ]] && pr_num=$(<"$_hive_pr_cache")
    title="${HIVE_NAME}-${HIVE_NUMBER} | ${branch}"
    [[ -n "$pr_num" ]] && title="${title} #${pr_num}"
  else
    title="${HIVE_NAME}-${HIVE_NUMBER}"
  fi
  printf '\e]0;%s\a' "$title"
}

_hive_zshexit() {
  local sidecar="/tmp/hive-dtach/${HIVE_NAME}-${HIVE_NUMBER}.json"
  [[ -f "$sidecar" ]] && rm -f "$sidecar"
  [[ -f "$_hive_pr_cache" ]] && rm -f "$_hive_pr_cache"
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _hive_precmd
add-zsh-hook zshexit _hive_zshexit

bindkey -s '^[g' 'hive status --compact\n'
bindkey -s '^[G' 'hive pull --compact\n'

_hive_last_branch=$(_hive_git_branch)
