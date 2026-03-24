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
  typeset -g POWERLEVEL9K_MULTILINE_FIRST_PROMPT_PREFIX='%240F╭─'
  typeset -g POWERLEVEL9K_MULTILINE_NEWLINE_PROMPT_PREFIX='%240F├─'
  typeset -g POWERLEVEL9K_MULTILINE_LAST_PROMPT_PREFIX='%240F╰─'
  typeset -g POWERLEVEL9K_BACKGROUND=236

  typeset -g POWERLEVEL9K_DIR_FOREGROUND=31
  typeset -g POWERLEVEL9K_SHORTEN_STRATEGY=truncate_to_unique
  typeset -g POWERLEVEL9K_TIME_FORMAT='%D{%H:%M}'
  typeset -g POWERLEVEL9K_TIME_FOREGROUND=109

  typeset -g POWERLEVEL9K_STATUS_OK=false
  typeset -g POWERLEVEL9K_COMMAND_EXECUTION_TIME_THRESHOLD=1
  typeset -g POWERLEVEL9K_VCS_CLEAN_FOREGROUND=76
  typeset -g POWERLEVEL9K_VCS_UNTRACKED_FOREGROUND=220
  typeset -g POWERLEVEL9K_VCS_MODIFIED_FOREGROUND=214

  typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_VIINS_FOREGROUND=76
  typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_VIINS_FOREGROUND=196
  typeset -g POWERLEVEL9K_PROMPT_CHAR_OK_VIINS_CONTENT_EXPANSION='❯'
  typeset -g POWERLEVEL9K_PROMPT_CHAR_ERROR_VIINS_CONTENT_EXPANSION='❯'
}

function prompt_hive_badge() {
  [[ -n "$HIVE_NAME" ]] || return
  p10k segment -f "${HIVE_COLOR_256:-75}" -t "[${HIVE_NAME}-${HIVE_NUMBER}]"
}

function instant_prompt_hive_badge() {
  prompt_hive_badge
}

function prompt_hive_pr() {
  [[ -n "$HIVE_NAME" ]] || return
  local cache="/tmp/hive-dtach/${HIVE_NAME}-${HIVE_NUMBER}.pr"
  local pr=""
  [[ -f "$cache" ]] && pr=$(<"$cache")
  [[ -n "$pr" ]] || return
  p10k segment -f 75 -t "#${pr}"
}

function instant_prompt_hive_pr() {
  prompt_hive_pr
}

typeset -g POWERLEVEL9K_CONFIG_FILE=${${(%):-%x}:a}

(( ${#p10k_config_opts} )) && setopt ${p10k_config_opts[@]}
