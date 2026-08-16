# Minimal Powerlevel10k config with hive-aware custom segments.

'builtin' 'local' '-a' 'p10k_config_opts'
[[ ! -o 'aliases'         ]] || p10k_config_opts+=('aliases')
[[ ! -o 'sh_glob'         ]] || p10k_config_opts+=('sh_glob')
[[ ! -o 'no_brace_expand' ]] || p10k_config_opts+=('no_brace_expand')
'builtin' 'setopt' 'no_aliases' 'no_sh_glob' 'brace_expand'

() {
  emulate -L zsh -o extended_glob

  unset -m '(POWERLEVEL9K_*|DEFAULT_USER)~POWERLEVEL9K_GITSTATUS_DIR'
  autoload -Uz is-at-least && is-at-least 5.1 || return

  typeset -g POWERLEVEL9K_LEFT_PROMPT_ELEMENTS=(
    hive_badge
    dir
    vcs
    newline
    prompt_char
  )

  typeset -g POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS=(
    hive_pr
    status
    command_execution_time
    background_jobs
    time
  )

  typeset -g POWERLEVEL9K_MODE=nerdfont-complete
  typeset -g POWERLEVEL9K_PROMPT_ADD_NEWLINE=true

  typeset -g POWERLEVEL9K_SHORTEN_STRATEGY=truncate_to_unique
  typeset -g POWERLEVEL9K_TIME_FORMAT='%D{%H:%M}'

  typeset -g POWERLEVEL9K_STATUS_OK=false
  typeset -g POWERLEVEL9K_COMMAND_EXECUTION_TIME_THRESHOLD=1
  typeset -g POWERLEVEL9K_VCS_BRANCH_ICON=$'\uF126 '
  typeset -g POWERLEVEL9K_VCS_UNTRACKED_ICON='?'
  typeset -g POWERLEVEL9K_VCS_DISABLE_GITSTATUS_FORMATTING=true
  typeset -g POWERLEVEL9K_VCS_CONTENT_EXPANSION='${$((my_git_formatter(1)))+${my_git_format}}'
  typeset -g POWERLEVEL9K_VCS_LOADING_CONTENT_EXPANSION='${$((my_git_formatter(0)))+${my_git_format}}'
  typeset -g POWERLEVEL9K_VCS_{STAGED,UNSTAGED,UNTRACKED,CONFLICTED,COMMITS_AHEAD,COMMITS_BEHIND}_MAX_NUM=-1

  typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_VIINS_CONTENT_EXPANSION='❯'
  typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_VIINS_CONTENT_EXPANSION='❯'
}

# --- day / night palette ------------------------------------------------------
# Ghostty's appearance-pair theme only remaps the 16 ANSI colors; the 256-color
# cube these segments use is fixed, so the prompt must pick mode-appropriate
# colors itself. The mode comes from the macOS appearance at startup;
# `term-theme` records flips in $XDG_CACHE_HOME/term-theme/mode and a precmd
# hook applies them at the next prompt (p10k hot reload picks up the changed
# variables — do not set POWERLEVEL9K_DISABLE_HOT_RELOAD here).

_term_public_p10k_state_file() {
  print -r -- "${XDG_CACHE_HOME:-$HOME/.cache}/term-theme/mode"
}

_term_public_p10k_apply_palette() {
  local mode=$1 frame
  typeset -g _term_public_p10k_mode=$mode
  if [[ $mode == day ]]; then
    frame=245
    typeset -g POWERLEVEL9K_BACKGROUND=254
    typeset -g POWERLEVEL9K_DIR_FOREGROUND=24
    typeset -g POWERLEVEL9K_TIME_FOREGROUND=66
    typeset -g POWERLEVEL9K_VCS_CLEAN_FOREGROUND=28
    typeset -g POWERLEVEL9K_VCS_UNTRACKED_FOREGROUND=94
    typeset -g POWERLEVEL9K_VCS_MODIFIED_FOREGROUND=130
    typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_VIINS_FOREGROUND=28
    typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_VIINS_FOREGROUND=160
    typeset -g _term_public_git_meta='%242F'
    typeset -g _term_public_git_clean='%28F'
    typeset -g _term_public_git_modified='%130F'
    typeset -g _term_public_git_untracked='%25F'
    typeset -g _term_public_git_conflicted='%160F'
    typeset -g _term_public_git_loading='%246F'
    typeset -g _term_public_hive_fg=25
  else
    frame=240
    typeset -g POWERLEVEL9K_BACKGROUND=236
    typeset -g POWERLEVEL9K_DIR_FOREGROUND=31
    typeset -g POWERLEVEL9K_TIME_FOREGROUND=109
    typeset -g POWERLEVEL9K_VCS_CLEAN_FOREGROUND=76
    typeset -g POWERLEVEL9K_VCS_UNTRACKED_FOREGROUND=220
    typeset -g POWERLEVEL9K_VCS_MODIFIED_FOREGROUND=214
    typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_VIINS_FOREGROUND=76
    typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_VIINS_FOREGROUND=196
    typeset -g _term_public_git_meta='%246F'
    typeset -g _term_public_git_clean='%76F'
    typeset -g _term_public_git_modified='%178F'
    typeset -g _term_public_git_untracked='%39F'
    typeset -g _term_public_git_conflicted='%196F'
    typeset -g _term_public_git_loading='%244F'
    typeset -g _term_public_hive_fg=75
  fi
  typeset -g POWERLEVEL9K_MULTILINE_FIRST_PROMPT_PREFIX="%${frame}F╭─"
  typeset -g POWERLEVEL9K_MULTILINE_NEWLINE_PROMPT_PREFIX="%${frame}F├─"
  typeset -g POWERLEVEL9K_MULTILINE_LAST_PROMPT_PREFIX="%${frame}F╰─"
}

_term_public_p10k_detect_mode() {
  if (( $+commands[defaults] )); then
    if defaults read -g AppleInterfaceStyle >/dev/null 2>&1; then
      print -r -- night
    else
      print -r -- day
    fi
    return
  fi
  local f mode
  f=$(_term_public_p10k_state_file)
  if [[ -r $f ]]; then
    mode=$(<$f)
    if [[ $mode == (day|night) ]]; then
      print -r -- $mode
      return
    fi
  fi
  print -r -- night
}

# Only state-file writes newer than the last applied mode count, so a stale
# file never overrides the appearance detected at startup.
zmodload -F zsh/stat b:zstat 2>/dev/null
zmodload zsh/datetime 2>/dev/null
typeset -g _term_public_p10k_mode_mtime=${EPOCHSECONDS:-0}

_term_public_p10k_watch_mode() {
  (( $+builtins[zstat] )) || return 0
  local f mode
  local -a st
  f=$(_term_public_p10k_state_file)
  zstat -A st +mtime -- $f 2>/dev/null || return 0
  (( st[1] > _term_public_p10k_mode_mtime )) || return 0
  typeset -g _term_public_p10k_mode_mtime=$st[1]
  mode=$(<$f)
  [[ $mode == (day|night) && $mode != $_term_public_p10k_mode ]] || return 0
  _term_public_p10k_apply_palette $mode
}

_term_public_p10k_apply_palette "$(_term_public_p10k_detect_mode)"
autoload -Uz add-zsh-hook
add-zsh-hook precmd _term_public_p10k_watch_mode

function my_git_formatter() {
  emulate -L zsh

  if [[ -n $P9K_CONTENT ]]; then
    typeset -g my_git_format=$P9K_CONTENT
    return
  fi

  if (( $1 )); then
    local meta=$_term_public_git_meta
    local clean=$_term_public_git_clean
    local modified=$_term_public_git_modified
    local untracked=$_term_public_git_untracked
    local conflicted=$_term_public_git_conflicted
  else
    local meta=$_term_public_git_loading
    local clean=$_term_public_git_loading
    local modified=$_term_public_git_loading
    local untracked=$_term_public_git_loading
    local conflicted=$_term_public_git_loading
  fi

  local res

  if [[ -n $VCS_STATUS_LOCAL_BRANCH ]]; then
    local branch=${(V)VCS_STATUS_LOCAL_BRANCH}
    (( $#branch > 32 )) && branch[13,-13]="…"
    res+="${clean}${(g::)POWERLEVEL9K_VCS_BRANCH_ICON}${branch//\%/%%}"
  fi

  if [[ -n $VCS_STATUS_TAG && -z $VCS_STATUS_LOCAL_BRANCH ]]; then
    local tag=${(V)VCS_STATUS_TAG}
    (( $#tag > 32 )) && tag[13,-13]="…"
    res+="${meta}#${clean}${tag//\%/%%}"
  fi

  [[ -z $VCS_STATUS_LOCAL_BRANCH && -z $VCS_STATUS_TAG ]] &&
    res+="${meta}@${clean}${VCS_STATUS_COMMIT[1,8]}"

  if [[ -n ${VCS_STATUS_REMOTE_BRANCH:#$VCS_STATUS_LOCAL_BRANCH} ]]; then
    res+="${meta}:${clean}${(V)VCS_STATUS_REMOTE_BRANCH//\%/%%}"
  fi

  if [[ $VCS_STATUS_COMMIT_SUMMARY == (|*[^[:alnum:]])(wip|WIP)(|[^[:alnum:]]*) ]]; then
    res+=" ${modified}wip"
  fi

  (( VCS_STATUS_COMMITS_BEHIND )) && res+=" ${clean}⇣${VCS_STATUS_COMMITS_BEHIND}"
  (( VCS_STATUS_COMMITS_AHEAD && !VCS_STATUS_COMMITS_BEHIND )) && res+=" "
  (( VCS_STATUS_COMMITS_AHEAD  )) && res+="${clean}⇡${VCS_STATUS_COMMITS_AHEAD}"
  (( VCS_STATUS_PUSH_COMMITS_BEHIND )) && res+=" ${clean}⇠${VCS_STATUS_PUSH_COMMITS_BEHIND}"
  (( VCS_STATUS_PUSH_COMMITS_AHEAD && !VCS_STATUS_PUSH_COMMITS_BEHIND )) && res+=" "
  (( VCS_STATUS_PUSH_COMMITS_AHEAD  )) && res+="${clean}⇢${VCS_STATUS_PUSH_COMMITS_AHEAD}"
  (( VCS_STATUS_STASHES        )) && res+=" ${clean}*${VCS_STATUS_STASHES}"
  [[ -n $VCS_STATUS_ACTION     ]] && res+=" ${conflicted}${VCS_STATUS_ACTION}"
  (( VCS_STATUS_NUM_CONFLICTED )) && res+=" ${conflicted}~${VCS_STATUS_NUM_CONFLICTED}"
  (( VCS_STATUS_NUM_STAGED     )) && res+=" ${modified}+${VCS_STATUS_NUM_STAGED}"
  (( VCS_STATUS_NUM_UNSTAGED   )) && res+=" ${modified}!${VCS_STATUS_NUM_UNSTAGED}"
  (( VCS_STATUS_NUM_UNTRACKED  )) && res+=" ${untracked}${(g::)POWERLEVEL9K_VCS_UNTRACKED_ICON}${VCS_STATUS_NUM_UNTRACKED}"
  (( VCS_STATUS_HAS_UNSTAGED == -1 )) && res+=" ${modified}─"

  typeset -g my_git_format=$res
}
functions -M my_git_formatter 2>/dev/null

function prompt_hive_badge() {
  [[ -n "$HIVE_NAME" ]] || return
  p10k segment -f "${HIVE_COLOR_256:-${_term_public_hive_fg:-75}}" -t "[${HIVE_NAME}-${HIVE_NUMBER}]"
}

function instant_prompt_hive_badge() {
  prompt_hive_badge
}

function prompt_hive_pr() {
  [[ -n "$HIVE_NAME" ]] || return
  local cache="/tmp/hive-tmux/${HIVE_NAME}-${HIVE_NUMBER}.pr"
  local pr=""
  [[ -f "$cache" ]] && pr=$(<"$cache")
  [[ -n "$pr" ]] || return
  p10k segment -f "${_term_public_hive_fg:-75}" -t "#${pr}"
}

function instant_prompt_hive_pr() {
  prompt_hive_pr
}

typeset -g POWERLEVEL9K_CONFIG_FILE=${${(%):-%x}:a}

(( ${#p10k_config_opts} )) && setopt ${p10k_config_opts[@]}
