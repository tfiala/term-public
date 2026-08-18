# ADR-0001: Shell History Hygiene — Deliberate Removal, Not Exit-Status Culling

**Kind:** decision
**Status:** accepted
**Date:** 2026-08-18
**Revisit:** A pre-execution validity signal becomes available that separates a malformed invocation from a correct command that reported a non-zero result; targeted removal proves too manual and pollution recurs faster than it is cleaned; bash or the line editor gains a native forget-this-entry primitive; or the interactive shell moves off bash (ble.sh or another shell) and the hook points change.
**Supersedes:** none
**Superseded-By:** none
**Related:** #24, #29

## Context

Two recent changes made history a first-class part of this repo's shell. #24
restored the zsh-era history *data* behavior after the bash cutover in #19:
timestamps, `HISTCONTROL=ignoreboth`, `histverify`, and a per-prompt
`history -a; history -c; history -r` sync so concurrent tmux panes share one
live file. #29 restored *recall* on top of it: Up/Down readline prefix search
and fzf's Ctrl-R, Ctrl-T, and Alt-C.

Better recall raises the value of history quality. An entry that was previously
buried is now actively surfaced: prefix search puts every past `codex …`
invocation one keystroke away, and Ctrl-R fuzzy-matches fragments across the
whole file. A wrong variant sitting next to the right one is no longer inert.

The motivating case is exactly that. `~/.bash_history` contains both
`codex --dangerously-skip-approvals-and-sandbox`, which is not a real flag, and
`codex --dangerously-bypass-approvals-and-sandbox`, which is. The wrong spelling
was typed, failed, and persisted.

The obvious remedy is to stop recording commands that exit non-zero. It is
mechanically available: `PROMPT_COMMAND` runs after each command with `$?` in
hand, and Starship's bash integration explicitly captures the status first
(`STARSHIP_CMD_STATUS`) and re-exposes the caller's `PROMPT_COMMAND` through
`STARSHIP_PROMPT_COMMAND`, so a filter could read the status and drop the entry
before `history -a` writes it. Nothing about #24's design blocks it.

This ADR records why we are not doing that, and what we do instead.

## Decision

**Do not filter shell history by exit status.** No `PROMPT_COMMAND` hook culls,
suppresses, or rewrites history entries based on `$?`, at any threshold —
neither "all non-zero" nor a narrowed subset such as 127.

History hygiene is handled by two deliberate controls instead:

1. **Targeted removal after the fact.** A `histrm <pattern>` helper removes
   matching entries from `~/.bash_history`, backing the file up first and
   treating the timestamped format as `#epoch` / command *pairs* so a removal
   cannot orphan a timestamp or split a multi-line entry. It must append-merge
   rather than snapshot-replace, since live shells write to the same file
   concurrently — the same defect the #24 review caught in
   `scripts/import-zsh-history.py`.
2. **Pre-hoc opt-out.** `HISTCONTROL=ignoreboth` (already set in `bash/bashrc`)
   means a leading space keeps a command out of history entirely. That is the
   supported way to try a flag you believe is a guess.

Implementing `histrm` is authorized by this decision but is not a precondition
for it; the prohibition on exit-status culling stands on its own.

## Rationale

**No exit code separates "wrong command" from "correct command that reported
something."** Measured on this machine: a bad flag to `codex` exits 2, a bad
flag to `git` exits 129, and a missing command exits 127 — but `grep` with no
match also exits 1, `diff` on differing files exits 1, a test runner reporting
genuine failures exits 1, and an interrupted command exits 130. The failing
codes are drawn from the same small set as the successful-but-non-zero ones.
Any threshold that removes the first group removes the second, and the second
group is full of commands worth recalling. Culling would silently delete
history the user wants in order to delete history the user does not.

**The narrow version does not even solve the motivating case.** Restricting the
cull to 127 (command not found) is the only variant with few false positives,
and it is useless here: `codex` exists, so the wrong flag exits 2, not 127. A
127-only rule would leave the exact entry that prompted this decision in place
while still interfering elsewhere.

**Culling fights repair.** The removal decision lands at the next prompt, which
is precisely the moment the user presses Up to recall the failed line and fix
it. Deleting `gti status` at the instant you would have pressed Up to correct
`gti` to `git` makes the shell worse at the one workflow the typo creates. #29
made Up-arrow prefix search the primary repair path, so this cost is now higher
than it was before.

**The harm is persistence, not existence.** A wrong command being in history for
the next few seconds is harmless and often useful. The problem is a wrong
variant living in the file indefinitely, polluting prefix search and Ctrl-R.
That is a removal problem, not a recording problem, and removal is where the fix
belongs.

**Precision beats policy at this volume.** In the current 1,168-entry file the
wrong `codex` spelling appears twice against 136 correct ones. A standing
automatic policy would have to guess correctly on every one of the 1,168 to
clean up two. A pattern the user chooses, run when they notice, cannot guess
wrong.

**Removal composes with the #24 sync design.** Because every shell reloads the
merged file at each prompt (`history -c; history -r`), one `histrm` propagates
to every live tmux pane at its next prompt with no restart and no per-pane
action.

## Consequences

- Nothing is ever removed from history without the user asking, so no correct
  command is lost to a heuristic. Up-arrow repair of a failed command keeps
  working.
- Pollution is not prevented automatically. Wrong entries persist until someone
  removes them or avoids recording them with a leading space. This is an
  accepted trade: the repo prefers a manual, precise cleanup over an automatic,
  imprecise one.
- `histrm` becomes a piece of software with real correctness obligations —
  timestamp/command pairing, multi-line entries, concurrent appends from live
  shells, and a backup before mutation. It should be built and tested to the
  standard `scripts/import-zsh-history.py` was, not written as a one-line `sed`.
- Removal is destructive to a file that has no other copy. The backup-first
  requirement is load-bearing, not decoration.
- The `PROMPT_COMMAND` chain stays as #24 left it. Nothing new runs per prompt,
  so prompt latency is unchanged.

## Infra Impact

None. `term-public` configures a local interactive shell; this decision touches
`bash/bashrc`, `~/.bash_history`, and a future `scripts/` helper. No cluster
resource, RBAC, NetworkPolicy, registry image, or secret is involved.

## Evidence

- Exit codes measured on this machine (macOS, Homebrew bash 5.x), 2026-08-18:
  `codex --<invalid-flag>` → 2; `git --<invalid-flag>` → 129; missing command →
  127; `grep` with no match → 1; `diff` on differing input → 1. The failing and
  the correct-but-non-zero cases overlap.
- `~/.bash_history` on 2026-08-18: 1,168 command lines; 2 occurrences of the
  invalid `codex --dangerously-skip-approvals-…` spelling versus 136 of the
  valid `--dangerously-bypass-approvals-…` one. (Counts only — history contents
  are not reproduced here.)
- `bash/bashrc` — `HISTCONTROL=ignoreboth` and the `_term_public_history_sync`
  `PROMPT_COMMAND` (the hook point a cull would have used), added in #24.
- `bash/bashrc` — the Up/Down `history-search-backward`/`-forward` bindings and
  the fzf keybinding source block, added in #29, which are what make a polluted
  history actively visible.
- Starship's bash init preserves `$?` as `STARSHIP_CMD_STATUS` and re-runs the
  caller's chain via `STARSHIP_PROMPT_COMMAND` (observed live:
  `STARSHIP_PROMPT_COMMAND=_term_public_history_sync`), establishing that
  exit-status culling was technically available and is being declined on merit.
- `scripts/import-zsh-history.py` and the #24 review — the prior art for the
  concurrency and file-format hazards `histrm` inherits.

## Revisit Triggers

- A signal becomes available that distinguishes a malformed invocation from a
  correct command reporting a non-zero result *before* the entry is written —
  for example a shell that validates flags against a completion spec. Exit
  status is not that signal; a different one would reopen this.
- Targeted removal proves too manual: pollution accumulates faster than it is
  cleaned, or the user stops running `histrm` and the wrong variants win prefix
  search anyway.
- bash or the line editor gains a native primitive for forgetting the previous
  entry, making a narrowly-scoped, user-triggered version of culling cheap and
  unambiguous.
- The interactive shell changes — ble.sh adopted (ADR-worthy in its own right,
  and it replaces readline), or a move off bash — since both the hook points and
  the recall behavior this decision is balanced against would change.

## Alternatives Considered

**Cull every non-zero exit.** Rejected. It cannot distinguish a bad flag from
`grep` finding nothing, a test run reporting real failures, or a command the
user interrupted, and it deletes the entry at the exact moment Up-arrow repair
needs it.

**Cull only exit 127 (command not found).** Rejected. It is the only
low-false-positive threshold, but it does not catch the motivating case — a real
binary with a wrong flag exits 2 — while still deleting `gti status` at the
moment the user would press Up to fix it.

**`HISTCONTROL=erasedups`.** Rejected as a solution to this problem. It
deduplicates identical lines; the wrong and right `codex` spellings are
different strings, so both survive. It also reorders history in ways that
interact badly with the per-prompt reload.

**Hand-edit `~/.bash_history` in an editor.** Rejected as the standing answer.
It works once, but it races live shells appending through `history -a`, has no
backup discipline, and makes it easy to orphan a `#epoch` line from its command.
Those are exactly the hazards `histrm` exists to encapsulate.

**Adopt ble.sh for ghost-text suggestions instead.** Out of scope here. It
addresses recall ergonomics, not history contents, and was already weighed and
deferred in #29; a polluted history would still surface wrong entries as ghost
text.
