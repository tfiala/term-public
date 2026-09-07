# ADR-0003: Hive Window Turn Indicator — Who Holds the Baton in an Implementer/Reviewer Pair

**Kind:** proposal
**Status:** proposed
**Date:** 2026-09-06
**Revisit:** Agent CLIs converge on a standard "waiting for input" signal (a title convention, an OSC sequence, or a cross-vendor hook), which would replace the inference in this ADR with a fact; the PR platform lets the two sessions act under distinct identities, which would make the comment author authoritative for the role; or pairs routinely grow beyond two windows, at which point the one-glyph label stops fitting the shape of the work.
**Supersedes:** none
**Superseded-By:** none
**Related:** ADR-0002

## Context

Interactive agent work in a hive runs as **pairs of sessions**: one window
implements, an adjacent window reviews. The pair is usually `<repo>-N` and
`<repo>-N+1`, but not always. The agents are mixed — Claude Code on one side,
Codex on the other, or the same CLI on both — so nothing here may depend on
one vendor's hooks or log format.

The loop is a relay with the operator in the middle. The reviewer posts a
disposition on the PR and stops; the operator switches to the implementer's
window and types "address review feedback". The implementer pushes, posts
"addressed in `<sha>`", and stops; the operator switches back and types
"re-review". When both windows are quiet the operator has to remember which
one is waiting for the next prompt, and with several pairs open across
several hives that memory fails routinely.

The working heuristic is that **the last window to produce output is usually
not the one holding the baton**, because producing that output was the
handoff. It is right in the steady state and wrong in three cases that matter:

1. The last speaker ended with a question. It is still that window's turn.
2. The reviewer approved. The next action is the merge, and the operator
   types that in the implementer's window, so the baton crosses over on an
   approval just as it does on a rejection.
3. The pair is not alternating: a re-review after a rebase, two fix rounds in
   a row, or a third window joining.

Nothing in the status bar describes interactive sessions today. ADR-0002 put
a `●` in the window label for live `run-dsl` runs (via the `@hive_run_suffix`
window option) and moved terminal run states to the backtick+a popup; a
separate backtick+b popup shows CI. Interactive agents are invisible to all
three.

Measured on the two attached hive sessions on 2026-09-06, 14 panes:

- tmux `window_activity` gives per-window idle time with no agent
  cooperation: values ranged from 0 s to 53 h across the 14 panes.
- Both agents already set the pane title. Claude Code writes `✳ <task
  summary>` (`✳ PR #632 review`, `✳ Pull request #634 review`); Codex writes
  the workspace name and prefixes a braille spinner while it is working
  (`⠧ home-dc-6`). Six of the fourteen panes carried a Claude summary; two of
  the Codex panes were mid-spin.
- `pane_current_command` separates the two: Claude Code's process is named by
  its version (`2.1.263`), Codex's is `node`.
- Both agents keep per-session JSONL logs on disk (610 Claude session files
  under `~/.claude/projects/`, 5,937 Codex files under `~/.codex/sessions/`).
  They hold the richest state — the first prompt, the last reply, whether it
  ended in a question — but each has a private format, and scanning them on
  every relabel is the same cost class ADR-0002 removed from the label path.
- The PR is the one record both sides write to, and the review discipline the
  agents follow makes those comments structured: a disposition (approve,
  changes requested, no new delta) or a completion reply ("addressed in
  `<sha>`"). Both sessions post as the same user, so the author says nothing
  about the role; the content says everything about the handoff.

Operator vocabulary is stable enough to classify: a review starts with
"pull, checkout pr #X, review"; an implementation starts with "implement
issue #Y" or is freeform.

## Proposed Decision

Add a **turn indicator** for interactive agent windows, built PR-first and
agent-agnostic, with per-agent enrichment strictly additive.

**Pairing and roles.** Two windows whose checkouts track the same PR head
branch are a pair. The window that pushed the branch and opened the PR is the
implementer; the window that checked the PR out is the reviewer. Adjacency
(`N`, `N+1`, lower index implements) is a tie-break hint only, never the
determination. A window with no partner still gets a turn state of its own.

**Baton rule**, first match wins:

1. PR merged or closed: no baton. Both windows are flagged for put-back (the
   wrap-up obligation), which is the next action there.
2. Last PR comment is a disposition: *changes requested* or *no new delta*
   points at the implementer; *approve* also points at the implementer,
   because that is where the merge is typed.
3. Last PR comment is a completion reply ("addressed in"): points at the
   reviewer.
4. A window whose last output ended in a question holds the baton regardless
   of 2–3. This needs an agent-specific enricher; without one the case is
   simply not detected, and rules 2–3 stand.
5. No PR yet, or no comments: the window that went quiet **earlier** holds the
   baton (the last-to-speak heuristic, by `window_activity`). If either window
   is still producing output, nobody does.

**Surfaces**, cheapest first, and they stack:

- **Window label glyph** through a new `@hive_turn_suffix` option composed
  into `_WINDOW_STATUS_FORMAT` beside `@hive_run_suffix`: `▶` the next prompt
  goes here; `✓` approved, waiting on the merge (shown on the implementer);
  `?` asked a question; nothing otherwise. One character, same budget as
  ADR-0002.
- **Pair line in status-right** through `status-context`: `5⇄6 #634 ▶6`.
- **Pairs popup** on a new backtick binding (`hive tmux pairs`): one row per
  pair with the windows, PR number and title, the last handoff (which window,
  which kind, how long ago), the next window, and the verb to type there
  (`re-review`, `address review feedback`, `merge pr`, `put back`). Unpaired
  windows get a row with their pane title as the summary.

**Data sources.** Version 1 uses only tmux formats (`window_activity`,
`pane_title`, `pane_current_command`, `pane_current_path`), `git` in the
checkout (branch, upstream), and the PR API through the existing `gh`/`fj`
paths, cached per branch for at most 60 s the way the Claude status line
already caches PR lookups. No hooks, no log parsing. Version 2 adds optional
enrichers — a Claude Code `Stop` hook, a Codex log reader — for rule 4 and for
a better summary line. An enricher that is absent, stale, or failing degrades
to version 1 behavior; it never produces a glyph on its own.

**Freshness and failure.** The glyph is recomputed on the existing relabel
cadence. A failed PR lookup renders no glyph and is counted in the popup as
"unknown", never folded into "no baton". This is the same rule ADR-0002 drew
for run state: a failed observation is not a verdict.

## Rationale

The PR is the only handoff record both agents already write, in a shape the
review discipline already fixes, so it is the only source that is
agent-agnostic *and* changes exactly when the baton moves. Building on it
first means the indicator is right for any agent that follows the workflow,
including one that does not exist yet.

The last-to-speak heuristic is kept, but demoted to the case where nothing
better exists. Its failure modes are precisely the states the PR encodes (an
approval) or an enricher can see (a question), so the ordering is what turns
the heuristic from "usually right" into "right unless the world is silent".

Approval points at the implementer for the same reason everything else does:
the glyph marks where the operator's next keystrokes go, not who acted last.
Putting `✓` on the reviewer would be a report; putting it on the implementer
is a pointer.

The label carries only actionable state, following ADR-0002. `▶`, `✓`, and
`?` each name a keystroke; a "working" glyph for interactive sessions would
not, and the spinner Codex already paints into the title covers it for free.

Enrichment is additive because the mixed-vendor constraint is not going away.
A design whose correctness depended on a Claude hook would be wrong in every
Codex window, and a hook that fires in one window of a pair but not the other
is worse than none: it would make one side look authoritative.

## Consequences

- `hive tmux` gains a second window option, a second suffix in the status
  format, a `pairs` subcommand, and a popup binding. `tests/test_hive_tmux.py`
  grows a table-driven test over the baton rule with fixtures for each rule
  and for the failure path.
- The relabel path gains a network dependency, bounded by the 60 s cache and
  by one call per distinct PR branch rather than per window. A pair shares a
  cache entry.
- Role inference in version 1 rests on the push/checkout distinction and the
  adjacency hint. A pair created by hand in an unusual shape can be
  misclassified; the popup shows the inferred roles so a wrong one is visible
  rather than silent.
- Rule 4 does not fire until an enricher exists, so in version 1 a question
  left by the last speaker still reads as a handoff. This is the current
  behavior, so it is a known gap rather than a regression.
- Comment classification is by content. If the review discipline changes its
  wording, the classifier and the discipline must change together; the
  popup's "last handoff" column makes a misread visible on the same screen.

## Infra Impact

None.

## Evidence

- Pane titles, current commands, and idle times were read from the two live
  hive sessions on 2026-09-06 (14 panes, two hives, both agents present).
- Session log counts were read from the two agents' state directories on the
  same day: 610 and 5,937 files.
- The three failure cases of the last-to-speak heuristic are drawn from the
  operator's own account of losing the baton, and the approval case was
  confirmed by the operator: the merge is typed in the implementer's window.
- The comment structure relied on in rules 2–3 is the review discipline the
  agents run under (disposition comments and "addressed in `<sha>`" replies as
  the only two kinds of cross-session message), which is why content
  classification is viable at all.

## Open Questions

- Should a *no new delta* disposition point at the implementer, or at nobody?
  It usually means the reviewer is waiting for a push that has not happened,
  which is the implementer's move, but it can also mean the operator sent a
  re-review too early, which is the operator's.
- Should `▶` be suppressed on the currently active window? The glyph exists
  to be seen from elsewhere; on the window the operator is already typing in
  it is noise, but hiding it makes the label flicker on every switch.
- Is the PR lookup acceptable in the relabel path for GitHub-hosted hives,
  where the API is rate-limited per account, or should GitHub repos fall back
  to rule 5 until a hook-written file exists?
- Should roles be declared rather than inferred — a `hive tmux role reviewer`
  at the start of a review, or a recognized first prompt — and if declared,
  where does the declaration live so both windows and the popup agree?
- Where does version 2 enricher state live, and who clears it when a window
  is reused for a different PR?

## Revisit Triggers

- An agent CLI ships a stable "waiting for input" signal usable from tmux, or
  the CLIs agree on one. Rule 4 becomes a fact instead of an enrichment.
- The PR platform supports distinct identities per session, making the
  comment author sufficient for the role.
- Multi-reviewer pairs become routine, and the pair model needs to be a set.
- PR lookups in the relabel path prove too slow or rate-limited in practice,
  which would push the state into a hook-written file after all.

## Alternatives Considered

- **Hook-only design (a Claude Code `Stop` hook writes the state).** Rejected
  as the primary source: it is blind in every Codex window, and a pair with
  one hooked side would present a false asymmetry.
- **Transcript-only design (read both agents' session logs).** Rejected as
  the primary source: two private formats, a scan cost per relabel of the
  kind ADR-0002 just removed, and still nothing that says which side the
  baton is on once a PR exists.
- **Last-to-speak only.** Rejected as the sole rule: it is wrong on
  questions and on approvals, which are exactly the moments the operator
  loses track.
- **A separate dashboard.** Rejected: the window label is already the
  glanceable surface, and a dashboard is one more window to lose the baton
  in.
- **A PR label set by the agent at each handoff (`needs-review`,
  `needs-fix`).** Not adopted for version 1 because it depends on every agent
  following a new convention, but it is the natural hardening once the
  content classifier has shown which cases it misreads: the same comment that
  the classifier keys on could set the label.
