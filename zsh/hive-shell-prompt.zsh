# hive-shell-prompt.zsh — Hive dev shell prompt overlay.
#
# Sourced automatically when HIVE_NAME is set (by `hive shell`).
# Works alongside Powerlevel10k: does NOT set PROMPT/RPROMPT directly.
# Instead, p10k custom segments (hive_badge, hive_pr) in ~/.p10k.zsh
# read HIVE_* env vars and the PR cache file this script maintains.
#
# Provides: Ghostty tab title, PR number caching, background git fetch,
# keyboard shortcuts, and session cleanup on exit.
#
# Required environment variables (set by hive shell before launching dtach):
#   HIVE_NAME       — short hive name (e.g., "infra")
#   HIVE_NUMBER     — workspace number (e.g., "3")
#   HIVE_ROOT       — hive root path
#   HIVE_WORKSPACE  — workspace directory name
#   HIVE_COLOR_RGB  — prompt color as "R;G;B" (e.g., "97;150;255")
#   HIVE_COLOR_256  — prompt color as 256-color code (e.g., "75")

# Bail if not in a hive shell
[[ -z "$HIVE_NAME" ]] && return

# Clear stale p10k instant prompt cache — a non-hive shell may have written
# a cache that bakes in empty hive segments, causing them to be suppressed.
rm -f "${XDG_CACHE_HOME:-$HOME/.cache}"/p10k-instant-prompt-*.zsh.zwc 2>/dev/null

# --- PR number caching -------------------------------------------------------

# Cache state for PR lookup
_hive_last_branch=""
_hive_pr_cache="/tmp/hive-dtach/${HIVE_NAME}-${HIVE_NUMBER}.pr"

_hive_git_branch() {
    git rev-parse --abbrev-ref HEAD 2>/dev/null
}

_hive_fetch_pr() {
    # Query Forgejo for PR number and write to cache file.
    # Called directly (blocking) or in a subshell (async).
    local branch="$1"
    local pr_json num
    pr_json=$(fj pr list --json --state open --head "$branch" 2>/dev/null)
    if [[ -n "$pr_json" ]]; then
        num=$(printf '%s' "$pr_json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data:
    print(data[0].get('number', ''))
" 2>/dev/null)
        if [[ -n "$num" ]]; then
            printf '%s' "$num" > "$_hive_pr_cache"
        else
            : > "$_hive_pr_cache"
        fi
    else
        : > "$_hive_pr_cache"
    fi
}

_hive_lookup_pr() {
    # Look up PR number for current branch, write to cache file.
    # The p10k hive_pr segment reads from this cache file.
    local branch="$1"
    [[ "$branch" == "$_hive_last_branch" ]] && return

    # Branch changed — invalidate immediately so stale PR# is never shown
    _hive_last_branch="$branch"
    : > "$_hive_pr_cache"

    # Only query for non-default branches
    local default_branch
    default_branch=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null)
    default_branch="${default_branch##refs/remotes/origin/}"
    [[ -z "$default_branch" ]] && default_branch="main"
    [[ "$branch" == "$default_branch" ]] && return

    # Async lookup — cache will be available on next prompt render
    _hive_fetch_pr "$branch" &!
}

# --- Background git fetch ----------------------------------------------------

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

# --- precmd hook --------------------------------------------------------------

_hive_precmd() {
    local branch
    branch=$(_hive_git_branch)
    if [[ -n "$branch" ]]; then
        _hive_bg_fetch
        _hive_lookup_pr "$branch"

        # Ghostty tab title via OSC — include PR# if available
        local _pr_num=""
        [[ -f "$_hive_pr_cache" ]] && _pr_num=$(<"$_hive_pr_cache")
        local _title="${HIVE_NAME}-${HIVE_NUMBER} | ${branch}"
        [[ -n "$_pr_num" ]] && _title="${_title} #${_pr_num}"
        printf '\e]0;%s\a' "$_title"
    else
        printf '\e]0;%s\a' "${HIVE_NAME}-${HIVE_NUMBER}"
    fi
}

autoload -Uz add-zsh-hook
add-zsh-hook precmd _hive_precmd

# --- zshexit hook (cleanup sidecar) -------------------------------------------

_hive_zshexit() {
    local sidecar="/tmp/hive-dtach/${HIVE_NAME}-${HIVE_NUMBER}.json"
    [[ -f "$sidecar" ]] && rm -f "$sidecar"
    [[ -f "$_hive_pr_cache" ]] && rm -f "$_hive_pr_cache"
}

add-zsh-hook zshexit _hive_zshexit

# --- Keyboard shortcuts -------------------------------------------------------

# CI popup (Alt+B)
bindkey -s '^[b' 'hive-tmux-ci-popup\n'

# Hive status (Alt+G)
bindkey -s '^[g' 'hive status --compact\n'

# Hive pull (Alt+Shift+G)
bindkey -s '^[G' 'hive pull --compact\n'

# --- Initialize branch tracking ----------------------------------------------
# Set _hive_last_branch so the first precmd doesn't see a "branch change"
# and clear the PR cache that hive.py pre-populated before launching dtach.
_hive_last_branch=$(_hive_git_branch)
