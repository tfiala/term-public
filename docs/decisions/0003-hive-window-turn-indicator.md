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

### What the generated tmux config schedules today

The window label is rewritten by `hive tmux label-window` from three hooks
only: `after-new-window`, `after-select-window`, and `after-select-pane`. A
whole-session relabel happens on config reload and on the manual backtick+R
binding. The one periodic producer is `status-right`, whose `#(hive tmux
status-context …)` tmux re-evaluates every `status-interval` (this repo sets
none, so tmux's default of 15 s applies) while a client is attached. Nothing
observes a PR comment being posted: an agent that posts a handoff and goes
quiet triggers none of the relabel hooks.

### What the available sources actually expose

Measured on the two attached hive sessions on 2026-09-06, 14 panes:

- tmux `window_activity` is the time of the last output to the window. It
  gives a per-window *quiet-since* with no agent cooperation; values ranged
  from 0 s to 53 h across the 14 panes. It says nothing about *who* is
  producing output or why.
- Both agents already set the pane title. Claude Code writes `✳ <task
  summary>` (`✳ PR #632 review`, `✳ Pull request #634 review`); Codex writes
  the workspace name and prefixes a braille spinner while it is working
  (`⠧ home-dc-6`). Six of the fourteen panes carried a Claude summary; two of
  the Codex panes were mid-spin. Only Codex supplies a working spinner in the
  title, so the title is a summary source, not a liveness source.
- `pane_current_command` separates the two CLIs: Claude Code's process is
  named by its version (`2.1.263`), Codex's is `node`. It identifies the
  program; it does not say whether the program is emitting output.
- Once both checkouts track the same PR branch, `git branch --show-current`
  and the upstream are identical in both windows, and the PR record is the
  same object for both. None of these carry push or checkout provenance.
  The branch **reflog** does. Measured on this repo's own PR: the checkout
  that created the branch has `branch: Created from HEAD` as the oldest
  reflog entry for the branch ref; a fresh clone after `gh pr checkout` has
  `branch: Created from origin/<branch>` and a tracking upstream. The two
  shapes are distinguishable with one local command and no network.
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
agent-agnostic, with per-agent enrichment strictly additive. Every rule below
is stated over facts the version-1 sources expose; anything they do not
expose resolves to *unknown*, and unknown never renders a glyph.

### Pairing

Two windows in the same hive whose checkouts are on the same branch, with
that branch the head of an open or recently closed PR, form a pair. A window
with no partner is *unpaired* and still gets a turn state of its own.

### Roles

The role of a window is resolved in this order; the first source that yields
a value wins.

1. **Declared.** `hive tmux role implementer|reviewer` stores
   `@hive_turn_role` on the window. It is cleared automatically when the
   window's checkout leaves the PR branch (the put-back) or when the branch's
   PR is merged or closed, so a declaration cannot outlive the pairing it
   describes. A future enricher may set it from the operator's first prompt;
   the option is the contract, not the enricher.
2. **Derived from the branch reflog** in the window's checkout, read on each
   refresh with `git reflog show --format=%gs refs/heads/<branch>` and taking
   the oldest entry:
   - `branch: Created from HEAD` or `branch: Created from <default-branch>`
     → implementer (the branch was born here);
   - `branch: Created from <remote>/<branch>`, or an entry recording a fetch
     of the PR head → reviewer (the branch was fetched here);
   - anything else, or no reflog → unknown.
3. **Unknown.** Adjacency (`N`, `N+1`) is **not** a source. It was proposed as
   a tie-break and removed: with no provenance in the other inputs it would
   have been the actual determinant, silently.

The popup shows each window's role and which source produced it, so a wrong
derivation is visible on the same screen as the glyph it drove.

### Liveness

The refresh producer samples `window_activity` for every hive window on each
tick. A window is **active** when its last output is younger than 30 s (two
ticks at the 15 s status-interval), otherwise **quiet**, with
*quiet-since* equal to `window_activity`. This is the only liveness input in
version 1: both agents repaint the pane while they work (Claude Code's
progress line; Codex's title spinner is itself pane output), so output
silence means the same thing for both, and no per-vendor signal is consulted.
`pane_current_command` is used only to name the agent in the popup. A hung
agent that keeps repainting reads as active; the popup shows "active for N
min" so that case is at least visible.

### Baton rule

Inputs per pair: the PR state; the classification of the **latest** PR
comment; the question flag per window; the liveness state per window. The
rule is evaluated top to bottom and the **first match is the result**. Every
input combination reaches a row.

| # | Condition | Result |
|---|---|---|
| 1 | PR merged or closed | both windows: `↩` (put this checkout back). This is the next action there and outranks everything below. |
| 2 | An enricher reports a window's last output ended in a question | that window: `?`. Sits above the comment rules because the question is newer than any handoff it followed; without an enricher the flag is simply absent and this row never matches. |
| 3 | latest comment is a disposition: *approve* | implementer: `✓` (the merge is typed there) |
| 4 | latest comment is a disposition: *changes requested* or *no new delta* | implementer: `▶` |
| 5 | latest comment is a completion reply ("addressed in") | reviewer: `▶` |
| 6 | latest comment is none of the recognized kinds | **unknown**: no glyph on either window; popup row reads "unrecognized handoff" with the comment's first line and age |
| 7 | no PR, or a PR with no comments; at least one window active | nobody: no glyph |
| 8 | no PR, or a PR with no comments; both windows quiet | the window with the **earlier** quiet-since: `▶` |

Two cross-cutting rules complete it:

- **Target role has no window.** When a row names a role (rows 3–5) and the
  pair has no window in that role — the window is unpaired, the partner was
  closed, or the role resolved to unknown — the result is unknown: no glyph,
  and the popup names the missing role ("next: reviewer — no window").
- **Lookup failure.** A PR lookup that fails or exceeds its 3 s timeout
  yields unknown for that pair on that tick: no glyph, and the popup reads
  "unknown (lookup failed N s ago)". The previous tick's glyph is cleared,
  not kept; a stale `▶` is a wrong pointer. This is the rule ADR-0002 drew
  for run state: a failed observation is not a verdict.

The comment classifier reads the first non-empty line of the latest comment,
case-insensitively: `approve`/`approved`/`lgtm` at the start → approve;
`changes requested` → changes requested; `no new delta` → no new delta;
`addressed in` → completion; anything else → unrecognized. The vocabulary is
the review discipline's, and the two must change together; the fixtures in
the tests are the coupling point.

### Surfaces

Cheapest first, and they stack:

- **Window label glyph** through a new `@hive_turn_suffix` option composed
  into `_WINDOW_STATUS_FORMAT` beside `@hive_run_suffix`: `▶` the next prompt
  goes here; `✓` approved, waiting on the merge; `?` asked a question; `↩`
  PR done, put this checkout back; nothing otherwise. One character, same
  budget as ADR-0002.
- **Pair line in status-right** from the producer's stdout: `5⇄6 #634 ▶6`.
- **Pairs popup** on a new backtick binding (`hive tmux pairs`): one row per
  pair with the windows and their roles (and role source), PR number and
  title, the last handoff (which window, which kind, how long ago), the next
  window, and the verb to type there (`re-review`, `address review
  feedback`, `merge pr`, `put back`). Unpaired and unknown windows get a row
  each, with the pane title as the summary and the reason no glyph is shown.

### Refresh producer

A new `hive tmux turn-refresh <session>` runs from the existing periodic
slot: the `#(…)` in `status-right`, which tmux re-evaluates every
`status-interval` for each attached client. One process per tick per
session, and it does the whole session's work:

1. list every hive window with `pane_current_path` and `window_activity`;
2. resolve pairs by branch and roles by declaration or reflog (local, cheap);
3. fetch PR state and the latest comment **once per distinct branch**, from a
   per-branch cache with a 60 s TTL and a 3 s lookup timeout, through the
   existing `gh`/`fj` paths;
4. evaluate the baton rule for every pair and unpaired window;
5. write `@hive_turn_suffix`, `@hive_turn_role`, and `@hive_turn_target` on
   **every** window with `tmux set-option -w`, whether or not it is selected;
6. print the pair line for status-right.

Because step 5 touches every window on every tick, both windows of a pair
change together, and a handoff posted while the operator is in another
window is picked up without any window-selection event. Worst-case staleness
after a comment is one status-interval plus the cache TTL: 75 s. The manual
backtick+R refresh also busts the PR cache, for the operator who wants the
answer now. `#(…)` runs only while a client is attached, which is exactly
when the label can be seen.

`label-window` stays what it is; it reads the options the producer wrote and
does no PR work of its own, so the hook path keeps ADR-0002's cost profile.

### Data sources and enrichment

Version 1 uses only tmux formats (`window_activity`, `pane_title`,
`pane_current_command`, `pane_current_path`), `git` in the checkout (branch,
upstream, the branch reflog), the declared-role option, and the cached PR
lookup. No hooks, no log parsing. Version 2 adds optional enrichers — a
Claude Code `Stop` hook, a Codex log reader — that may set the question flag
and improve the summary line. An enricher that is absent, stale, or failing
degrades to version 1 behavior; it never produces a glyph on its own, and it
never sets a role except through `@hive_turn_role`.

## Rationale

The PR is the only handoff record both agents already write, in a shape the
review discipline already fixes, so it is the only source that is
agent-agnostic *and* changes exactly when the baton moves. Building on it
first means the indicator is right for any agent that follows the workflow,
including one that does not exist yet.

Roles come from the branch reflog because it is the one place the checkout
itself records how the branch got there, it needs no network and no agent
cooperation, and it is already written by the commands the workflow uses. A
declared override sits above it because the reflog can expire or be absent,
and because an unusual pairing shape should be sayable in one command rather
than mis-derived. Adjacency is gone because a hint that decides in every
undeclared case is not a hint.

The question override sits above the comment rules because when it exists
it is strictly newer information than the handoff it follows. Putting it
below them, as the first draft did, made it unreachable whenever a PR
comment existed — which is every case after the first handoff.

The rule table has a row for "latest comment is something else" and for
"the role this row names has no window" because those are the cases that
would otherwise fall through to a guess. A glyph is a pointer at the
operator's next keystrokes; a pointer built from a guess is worse than no
pointer, so those rows resolve to unknown and say why in the popup.

The producer rides the `status-right` slot because it is the only periodic
thing the generated config already runs, and because a producer that updates
every window on every tick is the simplest way to guarantee both sides of a
pair change together. A cache TTL by itself schedules nothing; the tick is
what turns a TTL into a refresh.

The last-to-speak heuristic is kept, but confined to the case where nothing
better exists (rows 7–8), and stated over `window_activity` so it is the same
computation for both agents. Its failure modes are precisely the states the
PR encodes (an approval) or an enricher can see (a question), so the ordering
is what turns the heuristic from "usually right" into "right unless the
world is silent".

Approval points at the implementer for the same reason everything else does:
the glyph marks where the operator's next keystrokes go, not who acted last.
Putting `✓` on the reviewer would be a report; putting it on the implementer
is a pointer.

The label carries only actionable state, following ADR-0002. `▶`, `✓`, `?`,
and `↩` each name a keystroke; a "working" glyph for interactive sessions
would not, and the spinner Codex already paints into the title covers it for
free.

Enrichment is additive because the mixed-vendor constraint is not going away.
A design whose correctness depended on a Claude hook would be wrong in every
Codex window, and a hook that fires in one window of a pair but not the other
is worse than none: it would make one side look authoritative.

## Consequences

- `hive tmux` gains three window options (`@hive_turn_suffix`,
  `@hive_turn_role`, `@hive_turn_target`), a second suffix in the status
  format, the `role`, `turn-refresh`, and `pairs` subcommands, a popup
  binding, and a cache-busting step in the backtick+R refresh.
- `tests/test_hive_tmux.py` grows a table-driven test over the baton rule
  with one fixture per row, plus fixtures for: an unrecognized latest
  comment; a row whose target role has no window; a lookup failure and a
  lookup timeout; a declared role overriding a derived one; each reflog
  shape and the no-reflog case; and a PR-state transition (completion →
  disposition) delivered through a fake lookup with **no window-selection
  event**, asserting that one tick moves `@hive_turn_suffix` on both windows.
- The status-right slot gains a network dependency, bounded by the 60 s
  cache, the 3 s timeout, and one lookup per distinct PR branch per TTL
  rather than per window or per tick. A pair shares a cache entry.
- Reading a branch reflog per hive window per tick is a local `git` call;
  it is in the cost class ADR-0002 accepted for the label path, and it is
  measured before the change ships, the way that ADR measured its scan.
- Reflogs expire (`gc.reflogExpire`, 90 days by default) and are absent in
  some clone shapes, so a long-lived branch can lose its derived role. The
  result is *unknown*, shown as such, and one `hive tmux role` command fixes
  it; it is never a silently wrong role.
- The reflog shapes above were measured for a locally created branch and for
  `gh pr checkout`. `fj pr checkout` also creates a tracking branch, but its
  exact reflog line has not been captured; it goes into the fixture set
  before the derived rule is trusted on a Forgejo hive, and until then those
  windows resolve to unknown rather than to a guess.
- Row 2 does not fire until an enricher exists, so in version 1 a question
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
- Reflog provenance was measured on this repository's own PR branch: the
  originating checkout reports `branch: Created from HEAD`; a fresh clone
  after `gh pr checkout` reports `branch: Created from origin/<branch>` with
  `branch.<name>.remote`/`.merge` set to the tracking upstream.
- The scheduling facts in **Context** were read from the tmux config
  generator in `scripts/hive.py`: the three `set-hook … label-window` lines,
  the `status-right` `#(hive tmux status-context …)` slot, the reload and
  backtick+R `refresh-labels` paths, and the absence of any
  `status-interval` setting in the tracked tmux config.
- Session log counts were read from the two agents' state directories on the
  same day: 610 and 5,937 files.
- The three failure cases of the last-to-speak heuristic are drawn from the
  operator's own account of losing the baton, and the approval case was
  confirmed by the operator: the merge is typed in the implementer's window.
- The comment structure relied on in rows 3–5 is the review discipline the
  agents run under (disposition comments and "addressed in `<sha>`" replies as
  the only two kinds of cross-session message), which is why content
  classification is viable at all.

## Open Questions

- Should `▶` be suppressed on the currently active window? The glyph exists
  to be seen from elsewhere; on the window the operator is already typing in
  it is noise, but hiding it makes the label flicker on every switch.
- Is one PR lookup per branch per minute acceptable for GitHub-hosted hives,
  where the API is rate-limited per account, or should GitHub repos fall back
  to rows 7–8 until a hook-written file exists?
- Is 30 s the right active/quiet threshold? Two ticks absorbs a single slow
  redraw, but an agent that pauses between tool calls for longer than that
  will flicker to quiet and back. The number is a constant in one place and
  the fixtures pin it; it may need to move after a week of use.
- Where does version 2 enricher state live, and who clears it when a window
  is reused for a different PR? The `@hive_turn_role` lifecycle above is the
  model; the question is whether the question flag follows it.

## Revisit Triggers

- An agent CLI ships a stable "waiting for input" signal usable from tmux, or
  the CLIs agree on one. Row 2 becomes a fact instead of an enrichment.
- The PR platform supports distinct identities per session, making the
  comment author sufficient for the role.
- Multi-reviewer pairs become routine, and the pair model needs to be a set.
- PR lookups in the status-right slot prove too slow or rate-limited in
  practice, which would push the state into a hook-written file after all.

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
- **Adjacency as the role tie-break.** Rejected after review: the other
  version-1 inputs carry no provenance, so the "tie-break" would decide every
  undeclared case. The reflog carries provenance; adjacency carries a habit.
- **Event-driven refresh from the relabel hooks.** Rejected as sufficient:
  the hooks fire on window creation and selection, not on PR activity, so
  the glyph would go stale at exactly the moment it matters — after the agent
  posts and goes quiet.
- **Keep the last known glyph across a lookup failure.** Rejected: a stale
  `▶` is a wrong pointer, and the popup can carry the "unknown since" detail
  that the label cannot.
- **A separate dashboard.** Rejected: the window label is already the
  glanceable surface, and a dashboard is one more window to lose the baton
  in.
- **A PR label set by the agent at each handoff (`needs-review`,
  `needs-fix`).** Not adopted for version 1 because it depends on every agent
  following a new convention, but it is the natural hardening once the
  content classifier has shown which cases it misreads: the same comment that
  the classifier keys on could set the label.
