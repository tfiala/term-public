#!/usr/bin/env bash
# Post-bootstrap smoke test for a Linux host.  Run after
# setup/bootstrap-linux.sh and ./setup.sh; exits non-zero on the first broken
# contract.  CI runs it on Ubuntu and inside an AlmaLinux 9 container; on a
# real host it is the quickest "did it take?" check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

for c in starship tmux fzf rg jq git python3 eza gh; do
  command -v "$c" >/dev/null 2>&1 || fail "$c not on PATH"
done
pass "core commands present"

# Debian names them fdfind/batcat; bootstrap-linux.sh links the upstream
# names into ~/bin, which bash/bashrc puts first on PATH.
for pair in "fd fdfind" "bat batcat"; do
  # shellcheck disable=SC2086
  set -- $pair
  command -v "$1" >/dev/null 2>&1 || command -v "$2" >/dev/null 2>&1 \
    || fail "$1 missing (neither $1 nor $2 on PATH)"
done
pass "fd and bat present"

# A login shell through the linked files reaches the prompt with the repo
# root resolved and without the missing-starship complaint.
out="$(TERM=xterm-256color bash -lic 'echo "ROOT=$TERM_PUBLIC_ROOT"; echo READY' 2>&1)" \
  || fail "login shell failed: $out"
[[ "$out" == *"ROOT=$ROOT"* ]] || fail "TERM_PUBLIC_ROOT not resolved: $out"
[[ "$out" == *READY* ]] || fail "login shell did not reach the end of its config: $out"
[[ "$out" != *"missing starship"* ]] || fail "bashrc reports starship missing"
pass "interactive login shell starts through the linked config"

# fzf's Ctrl-R binding was sourced from one of the paths bash/bashrc lists.
TERM=xterm-256color bash -ic 'declare -F __fzf_history__' >/dev/null 2>&1 \
  || fail "fzf key bindings not loaded (no __fzf_history__ after bashrc)"
pass "fzf key bindings loaded"

STARSHIP_CONFIG="$ROOT/starship/starship.toml" starship prompt >/dev/null \
  || fail "starship prompt failed with the repo config"
pass "starship prompt renders"

TERMINFO= infocmp xterm-ghostty >/dev/null 2>&1 \
  || fail "xterm-ghostty terminfo not installed (setup.sh compiles ghostty/xterm-ghostty.terminfo)"
pass "xterm-ghostty terminfo resolves"

shell="$(tmux -L tp-smoke -f "$ROOT/tmux/tmux.conf" start-server \; show-options -gv default-shell 2>&1)" \
  || fail "tmux could not load tmux/tmux.conf: $shell"
tmux -L tp-smoke kill-server 2>/dev/null || true
case "$shell" in
  /usr/bin/bash|/usr/local/bin/bash) pass "tmux default-shell is $shell" ;;
  *) fail "tmux default-shell is '$shell', expected the system bash 5" ;;
esac

echo "smoke test passed"
