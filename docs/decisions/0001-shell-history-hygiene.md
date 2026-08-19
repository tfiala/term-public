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
   matching entries from `~/.bash_history`. This ADR fixes its required
   postconditions and leaves the algorithm to the implementation:

   - **Backup.** A copy of the pre-removal file exists before the destination
     is modified, created `0600` regardless of umask, since history can
     contain private commands.
   - **Privacy throughout.** The `0600` boundary covers *every* artifact that
     holds history content, not just the backup: any temporary or replacement
     file is created `0600` from the outset rather than chmod-ed afterwards
     (which leaves a readable window), ownership is preserved, and the final
     destination mode is no more permissive than the mode it had before,
     capped at `0600`. An atomic replace that publishes a fresh inode created
     under the default umask would silently relax `~/.bash_history` from
     `0600` to `0644` — the operation would satisfy every other postcondition
     while exposing the private commands this contract exists to protect.
     Diagnostics are value-safe for the same reason: counts and bounded
     reasons only, never the matched command text.
   - **Selectivity.** Every non-matching entry survives byte-identically,
     including its `#epoch` line where it has one — and its *absence* where it
     does not. The file is not guaranteed to be all timestamped pairs:
     `scripts/import-zsh-history.py` emits an entry with no `#epoch` line when
     the source entry carried no timestamp, so untimestamped entries **may**
     be present and must be preserved as-is.
   - **Structural integrity.** The result contains no `#epoch` line whose
     command was removed, and no orphaned continuation line from a multi-line
     entry.
   - **No silent loss.** Entries appended by a live shell between the read and
     the replacement are either carried into the result or the run aborts
     non-zero leaving the destination unchanged. Dropping them silently is a
     defect, not a tolerable race.
   - **No self-reintroduction.** The text just removed must not be back in
     the history at the next prompt sync. This is not hypothetical: the
     helper's own command line contains the pattern, and `PROMPT_COMMAND`'s
     `history -a` appends it after the rewrite has already happened, so an
     ordinary `histrm <pattern>` ends with the file containing exactly one
     matching line — its own invocation — leaving the bad spelling on
     Ctrl-R. The documented usage is therefore a leading-space invocation
     (` histrm …`), which `HISTCONTROL=ignoreboth` keeps out of history
     entirely; a shell-function implementation that deletes its own entry
     satisfies the postcondition equally. The scope is exactly the
     invocation — bash tracks its write offset, so entries flushed before
     the rewrite are not re-appended.
   - **Atomic publication.** No reader ever observes a partially written
     file.
   - **No-op safety.** A pattern that matches nothing leaves the destination
     byte-identical.

   The "no silent loss" postcondition is the load-bearing one, and it is why
   the concurrency fix from #24 does not transfer. `import-zsh-history.py` is
   *additive*, so `O_APPEND` is sufficient there: a concurrent `history -a`
   interleaves harmlessly. Removal is *subtractive* — appending a filtered
   copy would leave the original bytes in place, so the file must be rewritten,
   which reintroduces exactly the lost-update window `O_APPEND` avoided.
   Because bash's `history -a` takes no lock, a removal cannot close that
   window from its own side; it can only detect the change and retry or refuse,
   or require that no other shell is writing. Whichever the implementation
   picks, it must state the residual window rather than claim there is none —
   a shell holding the old descriptor across an atomic replace can still write
   to the replaced inode.

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

**Removal composes with the #24 sync design — in both directions.** Because
every shell reloads the merged file at each prompt (`history -c; history -r`),
one `histrm` propagates to every live tmux pane at its next prompt with no
restart and no per-pane action. The same prompt hook is also what makes the
no-self-reintroduction postcondition necessary rather than pedantic: `history -a`
runs on that same next prompt and would otherwise publish the invocation — and
therefore the pattern text — to every one of those panes. The sync is not a
free win to be assumed; it propagates whatever the file says, including a
mistake.

## Consequences

- Nothing is ever removed from history without the user asking, so no correct
  command is lost to a heuristic. Up-arrow repair of a failed command keeps
  working.
- Pollution is not prevented automatically. Wrong entries persist until someone
  removes them or avoids recording them with a leading space. This is an
  accepted trade: the repo prefers a manual, precise cleanup over an automatic,
  imprecise one.
- `histrm` becomes a piece of software with real correctness obligations —
  possibly-untimestamped entries, multi-line entries, concurrent appends from
  live shells, file modes on every artifact, and a backup before mutation. It
  should be built and tested to the standard `scripts/import-zsh-history.py`
  was, not written as a one-line `sed`. A naive `grep -v` rewrite fails at
  least three of the postconditions above (orphaned `#epoch` lines, a
  `0644` replacement inode, and self-reintroduction), which is why they are
  written down rather than left to taste.
- The usage is part of the contract, not a tip: `histrm` is invoked with a
  leading space unless it is implemented to delete its own entry. A helper
  that is correct internally and invoked normally still leaves the pattern in
  history, so documentation that omits the space documents a broken workflow.
- Removal is strictly harder than the import was, and the ADR does not pretend
  otherwise: the importer could sidestep concurrency with `O_APPEND`, while a
  rewrite cannot. Whoever implements `histrm` inherits a real design decision
  (detect-and-retry versus required quiescence) rather than a settled recipe.
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
- Format ambiguity, measured 2026-08-19 (counts only): the live
  `~/.bash_history` had 928 lines and 429 `#epoch` markers. Uniform pairs
  would be 858, so 70 lines are unaccounted for — but bash's format cannot
  say whether those are continuation lines of multi-line entries,
  untimestamped entries, or both, and the file had no untimestamped entry
  before its first marker. That is precisely why the contract is written for
  what the format *permits* rather than what one file happens to hold: the
  distinction is not recoverable by inspection.
- Self-reintroduction, probed 2026-08-19 in a synthetic interactive bash using
  this repo's `bash/bashrc`, with a naive `grep -v` removal helper on `PATH`:
  seeding one matching entry and running `histrm <marker>` normally left the
  file with exactly one matching line — the recorded `histrm <marker>`
  invocation. Invoking it with a leading space left zero. A separate run
  confirmed the scope: an entry flushed before the rewrite was *not*
  re-appended, so only the invocation is at risk. The same run also showed the
  naive helper orphaning a `#epoch` line whose command it removed —
  independent evidence that the structural-integrity postcondition is load-
  bearing rather than decorative.
- `~/.bash_history` mode observed `0600` on 2026-08-19, which is the mode the
  privacy postcondition requires a rewrite to preserve rather than relax.
- `bash/bashrc` — `HISTCONTROL=ignoreboth` and the `_term_public_history_sync`
  `PROMPT_COMMAND` (the hook point a cull would have used, and the mechanism
  that would re-add a `histrm` invocation), added in #24.
- `bash/bashrc` — the Up/Down `history-search-backward`/`-forward` bindings and
  the fzf keybinding source block, added in #29, which are what make a polluted
  history actively visible.
- Starship's bash init preserves `$?` as `STARSHIP_CMD_STATUS` and re-runs the
  caller's chain via `STARSHIP_PROMPT_COMMAND` (observed live:
  `STARSHIP_PROMPT_COMMAND=_term_public_history_sync`), establishing that
  exit-status culling was technically available and is being declined on merit.
- `scripts/import-zsh-history.py` and the #24 review — the prior art for the
  concurrency and file-format hazards `histrm` inherits. Two specifics the
  removal contract above depends on: `render_bash_history` emits a `#epoch`
  line only `if timestamp is not None`, so an untimestamped entry is written
  bare and the file **can** be a mix rather than uniform pairs — a capability
  of the writer, which is what the conservative contract needs; it is not a
  claim about what any particular file contains today (see the next bullet);
  and the destination write is
  `O_APPEND` with the comment "Append via `O_APPEND`, never read-modify-replace:
  a `history -a` from a live shell between the snapshot above and this write
  must survive" — an option available to an additive import and not to a
  subtractive removal.

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
