# ADR-0003: Hive Window Turn Indicator — Who Holds the Baton in an Implementer/Reviewer Pair

**Kind:** decision
**Status:** implemented
**Date:** 2026-09-06
**Revisit:** Agent CLIs converge on a standard "waiting for input" signal (a title convention, an OSC sequence, or a cross-vendor hook), which would replace the inference in this ADR with a fact; the PR platform lets the two sessions act under distinct identities, which would make the comment author authoritative for the role; the review discipline adopts the `Handoff:` marker below, which would let the first-line grammar shrink to a fallback; or pairs routinely grow beyond two windows, at which point the one-glyph label stops fitting the shape of the work.
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
none, so tmux's default of 15 s applies) while a client is attached. Each
evaluation is a **new process**: nothing survives from one tick to the next
except what is written to a tmux option or to disk. Nothing observes a PR
comment being posted: an agent that posts a handoff and goes quiet triggers
none of the relabel hooks.

### What identity and storage the hive already models

A hive is multi-repository by construction, and branch names collide across
repositories as a matter of course: two unrelated `fix/foo` branches in two
repos of one hive are ordinary. `hive` already treats identity accordingly:
`_normalize_origin_url` strips `.git`, trailing slashes, and userinfo so two
spellings of one remote dedupe to one cache key; `_label_cache_key` hashes
the workspace path so same-named workspaces in different hives do not share
a label cache; and `_default_branch` reads `refs/remotes/origin/HEAD` to
know which branch is the repo's default. Anything this ADR keys must be at
least that specific.

Storage is a different matter. `_TMUX_DIR` is `/tmp/hive-tmux`, created
with `mkdir` defaults: the directory is mode `0755` and its files `0644`,
readable by every local user. That is fine for generated tmux config and
label caches, which hold nothing private. It is not fine for PR titles and
comment text from private repositories, which is what this ADR needs to
cache. The one place `hive` already writes user-private files is the popup
path, which uses `tempfile.mkstemp` (mode `0600`).

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
- `pane_current_command` separates the two live CLIs from a plain shell in
  the measured panes, but it is only a basename: Claude Code presents its
  version (`2.1.263`), Codex presents `node`, and a bare window presents
  `bash`. `node` is not an agent identity. The process inventory on the pane
  TTY carries the structural distinction: the live Codex pane has a Node
  entry script whose resolved path ends in
  `/node_modules/@openai/codex/bin/codex.js` and a descendant native `codex`
  binary under the same package; the live Claude pane has a `claude` process.
  An ordinary Node server has neither agent signature. The inventory
  identifies the program but still does not say whether it is emitting output.
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
- The PR is the one record both sides write to. Both sessions post as the
  same user, so the author says nothing about the role; the content carries
  the handoff, but not in a single fixed phrase. Measured on this
  repository's own PR comments (87 comments across PRs #7–#38, read on
  2026-09-06): a first-line grammar of `approve`/`lgtm`, `changes
  requested`, `no new delta`, and `addressed in` recognized 65. The real
  forms include `Review disposition: LGTM at exact head …`, `Approved / LGTM
  on exact rebased head …`, `## Re-review: <sha> — approved`, `Changes still
  requested on exact head …`, `Reviewed locally. No blocking findings.`,
  `Both blockers addressed in …`, `Fixed in …`, `Pushed …`, and `Rebased
  onto current main …; new head …`. A loose grammar that accepted `approve`
  anywhere covered 78 of the 87 — and also accepted `Cannot approve this
  yet`. The grammar in this ADR is anchored and guarded instead; its numbers
  are in **Evidence**.

Operator vocabulary is stable enough to classify: a review starts with
"pull, checkout pr #X, review"; an implementation starts with "implement
issue #Y" or is freeform.

## Decision

Add a **turn indicator** for interactive agent windows, built PR-first and
agent-agnostic, with per-agent enrichment strictly additive. Every rule below
is stated over facts the version-1 sources expose; anything they do not
expose resolves to *unknown*, and unknown never renders a glyph.

### Identity, eligibility, and pair shape

The **pair key** of a window is `(remote, branch)`: the checkout's origin
URL passed through `_normalize_origin_url`, and the current branch. Windows
with equal branch names on different remotes never share a key, a group, or
any cached state.

A window is **eligible** only if all three hold: its pane's current path is
inside a git checkout with an origin; its branch is not that remote's
default branch as read by `_default_branch`; and the process inventory for
its `pane_tty` contains a verified agent signature. The producer reads that
inventory with `ps`, restricted to the session's pane TTYs and rooted at each
`pane_pid`, and walks the parent/child relationships rather than trusting the
foreground basename:

- Claude Code is a descendant process whose command basename is `claude`.
- Codex is either a Node process whose entry-script path resolves to a path
  ending in `/node_modules/@openai/codex/bin/codex.js`, or its descendant
  native `codex` binary under that same package. A bare `node` basename never
  qualifies.

Only the resulting boolean and agent kind survive the check; command lines
are never printed, logged, or cached. A failed or unparseable process snapshot
also fails closed and clears any prior eligibility output. A window put back
on `main`, a window that is just a shell, and a window running an ordinary
Node workload are ineligible: they take no part in any group, get no glyph,
and appear in the popup only under "not participating". Unknown agent
installations fail closed to ineligible until their structural signature has
a fixture.

A **group** is the set of eligible windows in one hive with equal pair keys.
The baton table below applies to a group according to its **shape**, and
only one shape is a pair:

| Shape | Definition | What the table does |
|---|---|---|
| pair | exactly two windows whose resolved roles are exactly one implementer and one reviewer | every row applies |
| singleton | one window | rows 1–2 apply; rows 3–5 apply only when the window's own role equals the row's target, else unknown ("next: reviewer — no window"); rows 7–8 resolve to **no glyph**, popup "unpaired, quiet since …" |
| malformed | two windows with a duplicate role or any unknown role; or three or more windows | rows 1–2 apply per window; every other row resolves to **unknown**, and the popup names the shape ("implementer + implementer", "3 windows on <key>", "roles unknown") |

The "third window joining" case named in **Context** is therefore not a
pair; it is a malformed group, shown as such, until a window is put back.
Declarations can repair duplicate or unknown roles, but cannot repair a
three-window cardinality.

### PR resolution and terminal state

A PR is an **attribute** of a group, not a precondition for it. Open PR state
is group-wide even when one checkout has not pulled the latest head. Without
an open PR, terminal matching is defined only when every member has the same
local HEAD; different local HEADs resolve to **unknown** ("mixed local
HEADs") rather than pretending the group has one revision.

Resolution proceeds in this order:

1. When all members share one HEAD, an immutable **merged receipt** for
   `(pair key, HEAD)` may answer merged without a network lookup. A merged PR
   cannot reopen.
2. Otherwise a mutable lookup entry younger than 60 s may answer open,
   closed, none, or a structural unknown. The entry is bound to the pair key
   and the set of local HEADs it was evaluated against.
3. Otherwise query the remote for at most two open PRs whose head ref is the
   branch. Exactly one is the group's open PR; two results means two or more
   and is unknown ("multiple open PRs"). If none exists and the group has one
   shared local HEAD, inspect one count-bounded page (at most 30) of the most
   recently updated closed or
   merged PRs with that head ref and match that HEAD to the PR head SHA. A
   merged match writes an immutable receipt; a closed-unmerged match is only
   a mutable 60 s cache entry. With no match, a short page proves none; a full
   page is unknown ("terminal history truncated") because an older match may
   exist.
4. A failed lookup, a producer-deadline cutoff, or mixed local HEADs after no
   open PR resolves to unknown. No stale mutable entry is a verdict.

The distinction is deliberate: merged is immutable, while a closed PR may be
reopened with the same head SHA. A successful refresh that sees a reopen
replaces the cached closed observation with open; a failed refresh after the
closed entry expires yields unknown, not `↩`. The manual backtick+R refresh
busts every mutable PR entry but never an immutable merged receipt.

Merged receipts do not expire and are invalidated only when the pair key or
HEAD changes. The `↩` state for a merged checkout therefore persists until
put-back without allowing a mutable closed observation to become permanent.
Pre-PR groups (a fresh resolution of none) are ordinary — a reviewer may
fetch a branch before the PR is opened — and they are exactly the pairs rows
7–8 of the baton table exist for.

### Roles

Roles are computed from two inputs and written to two outputs. **No output
is ever read back as an input**; the self-latching that a shared option
would cause is the reason for the split.

Inputs:

1. **Declared:** `@hive_turn_role_declared`, set only by `hive tmux role
   implementer|reviewer`, which writes **the role and the pair key it was
   declared for** as one value (`reviewer <remote>#<branch>`). On every tick
   the producer compares the stored key with the window's current key; a
   mismatch means the declaration belongs to a branch this window has left,
   so it is ignored and cleared. The producer also clears it when the
   group's PR resolves to merged or closed. Because the binding is stored in
   the option itself, a fresh producer process — which is every producer
   process — can tell "declared for this pair" from "stale declaration
   carried onto a new branch" without any memory of the previous tick.
2. **Derived from the branch reflog** in the window's checkout, read fresh
   on every tick with `git reflog show --format=%gs refs/heads/<branch>` and
   taking the oldest entry:
   - `branch: Created from HEAD` or `branch: Created from <default-branch>`
     → implementer (the branch was born here);
   - `branch: Created from <remote>/<branch>`, or an entry recording a fetch
     of the PR head → reviewer (the branch was fetched here);
   - anything else, or no reflog → unknown.

Outputs, rewritten on every tick: `@hive_turn_role` (the effective role:
`implementer`, `reviewer`, or `unknown`) and `@hive_turn_role_source`
(`declared`, `reflog`, or `none`). A valid declaration wins; otherwise the
derived value is used; otherwise unknown. Because the derived value is never
stored as an input, it persists exactly as long as its evidence does: if the
reflog expires or becomes unreadable, the next tick reports unknown. A
declared role persists until its binding fails or its lifecycle clears it,
and the popup can always name the source truthfully.

Adjacency (`N`, `N+1`) is **not** a source. It was proposed as a tie-break
and removed: with no provenance in the other inputs it would have been the
actual determinant, silently.

### Liveness

The refresh producer samples `window_activity` for every hive window on each
tick. A window is **active** when its last output is younger than 30 s (two
ticks at the 15 s status-interval), otherwise **quiet**, with
*quiet-since* equal to `window_activity`. This is the only liveness input in
version 1: both agents repaint the pane while they work (Claude Code's
progress line; Codex's title spinner is itself pane output), so output
silence means the same thing for both, and no per-vendor signal is consulted.
The process-tree result determines eligibility; `pane_current_command` is
display-only in the popup. A hung agent that keeps repainting reads as active;
the popup shows "active for N min" so that case is at least visible.

### Baton rule

Inputs per group: its shape; the resolved PR (open, immutable merged receipt,
fresh closed observation, none, or unknown); the classification of the
**latest** PR comment; the question flag per window; the liveness state per
window. The rule is evaluated top to bottom and the **first match is the
result**, subject to the shape table above. Every input combination reaches
a row.

| # | Condition | Result |
|---|---|---|
| 1 | PR resolved as merged or closed, with one shared local HEAD matching the PR head | each window: `↩` (put this checkout back). This is the next action there and outranks everything below. |
| 2 | An enricher reports a window's last output ended in a question | that window: `?`. Sits above the comment rules because the question is newer than any handoff it followed; without an enricher the flag is simply absent and this row never matches. |
| 3 | latest comment is a disposition: *approve* | implementer: `✓` (the merge is typed there) |
| 4 | latest comment is a disposition: *changes requested* or *no new delta* | implementer: `▶` |
| 5 | latest comment is a completion reply | reviewer: `▶` |
| 6 | latest comment is none of the recognized kinds | **unknown**: no glyph on either window; popup row reads "unrecognized handoff" with the comment's first line and age |
| 7 | PR none, or PR with no comments; at least one window active | nobody: no glyph |
| 8 | PR none, or PR with no comments; both windows quiet | the window with the **earlier** quiet-since: `▶` |

Two cross-cutting rules complete it:

- **Target role has no window.** When a row names a role (rows 3–5) and the
  group has no window in that role, the result is unknown: no glyph, and the
  popup names the missing role.
- **Lookup failure.** A PR lookup that fails, exceeds its own timeout, or is
  cut off by the producer deadline yields unknown for that group on that
  tick: no glyph, and the popup reads "unknown (lookup failed N s ago)". The
  previous tick's glyph is cleared, not kept; a stale `▶` is a wrong pointer.
  An immutable merged receipt is not a lookup and is unaffected; an expired
  closed observation is. This is the rule ADR-0002 drew for run state: a
  failed observation is not a verdict.

### Comment classifier

The classifier consumes the latest PR comment and returns exactly one of
*approve*, *changes requested*, *no new delta*, *completion*, or
*unrecognized*. It reads at most the first non-empty line and the last three
non-empty lines, never the body. It is **positively anchored** — every
recognized form must start the line or follow a review heading — and
**guarded**: a negation anywhere on the first line, or more than one family
of keyword anywhere on it, refuses the whole line. The refusal is the safety
property; `✓` is the one glyph that directs a merge, and it may not be
earned by a sentence that merely contains the word.

1. **One exact marker, when present, wins.** An unquoted, unadorned complete
   line of the form `Handoff: approve`, `Handoff: changes-requested`,
   `Handoff: no-new-delta`, or `Handoff: addressed` among the last three
   non-empty lines is the classification. Two or more marker lines are
   unrecognized rather than resolved by order; quoted, bulleted, and fenced
   examples are not markers. This is the durable, machine-readable convention
   the review discipline may adopt; version 1 does not depend on it.
2. **Normalize the first non-empty line:** strip heading markers, emphasis,
   and backticks; lowercase.
3. **Negation guard.** If the line contains any of `not`, `cannot`, `can't`,
   `isn't`, `aren't`, `won't`, `don't`, `never`, `withheld`, `blocked`, or
   `pending` as a word, the result is unrecognized. No further matching.
4. **Anchored forms.** In what follows `<sha>` is 7–40 hex digits, `<end>`
   is end of line or one of space `. , ; : ! ) /`, and a *review heading*
   is: up to three words, then `review` or `re-review`, optionally `at|on|of
   <sha>`, optionally `disposition`, then any non-alphanumerics, optionally
   `<sha>` and more non-alphanumerics.
   - *approve:* an optional review heading followed by one verdict clause:
     `approve`, `approved`, `lgtm`, or `approved / lgtm`. After the verdict,
     the only accepted tails are (a) punctuation and end of line; (b)
     `at|on`, optional `exact`, optional `rebased`, optional `head`, `<sha>`,
     optional `against base <sha>`, then punctuation and end of line; or (c)
     the exact suffix `, confirming the standing approval at this head.`.
     Separately, the complete lines `reviewed locally. no blocking
     findings|issues.`, `reviewed the pr diff in <path>. no blocking
     findings|issues.`, and `no blocking findings|issues.` are accepted;
     `<path>` is one non-whitespace path token. Any other prefix or tail is
     unrecognized even when it contains an approval phrase.
   - *changes requested:* `changes` then at most two words then
     `requested`, at line start or after a review heading.
   - *no new delta:* that phrase at line start or after a review heading.
   - *completion:* up to six words then `addressed in <sha><end>`;
     `addressed` then up to six words then `in <sha><end>`; `fixed in
     <sha><end>`; `pushed <sha><end>` at line start; `and pushed
     <sha><end>`; `new head <sha>` followed by end, `, ; . )`, or `ci`; or
     `addressed … in the latest push` within 80 characters.
5. **Family guard.** Count the keyword families present anywhere on the
   line: approval words (`approve(d)`, `lgtm`, `no blocking
   findings|issues`), `changes … requested`, `no new delta`, and the
   completion phrases (`addressed in`, `fixed in`, `pushed`). A completion
   form is accepted only if no disposition family appears; a disposition
   form is accepted only if it is the sole family present and no completion
   phrase appears. Anything else is unrecognized. Ambiguity is never
   resolved by precedence.

The guards are what refuse `Cannot approve this yet`, `Approval withheld
pending the failing test`, `Not ready for approval`, `Approved with changes
requested on <sha>`, `Pushed back on <sha>`, `LGTM-adjacent but blocked`,
`Rebased; the new head <sha> is not ready`, and `No blocking findings?
Several, actually.`. The finite approval grammar additionally refuses
`Approved if CI passes`, `LGTM except for the failing migration`, `Approve
after addressing the blocker`, `Review: approved once the required test
lands`, `Approved provided that the migration is fixed`, `LGTM assuming CI
turns green`, and `Reviewed conditionally; no blocking findings.`. Every one
is unrecognized; none produces a glyph. The anchors keep
`Changes still requested on exact head <sha>` a disposition despite its
commit reference, and `Both blockers addressed in <sha>` a completion. The
cost of the guards on the real corpus is two comments, itemized in
**Evidence**; both are refusals, which is the safe direction.

### Surfaces

Cheapest first, and they stack:

- **Window label glyph** through a new `@hive_turn_suffix` option composed
  into `_WINDOW_STATUS_FORMAT` beside `@hive_run_suffix`: `▶` the next prompt
  goes here; `✓` approved, waiting on the merge; `?` asked a question; `↩`
  PR done, put this checkout back; nothing otherwise. One character, same
  budget as ADR-0002.
- **Pair line in status-right** from the producer's stdout: `5⇄6 #634 ▶6`.
- **Pairs popup** on a new backtick binding (`hive tmux pairs`): one row per
  group with the windows and their roles (and role source), the shape when
  it is not a pair, PR number and title, the last handoff (which window,
  which kind, how long ago), the next window, and the verb to type there
  (`re-review`, `address review feedback`, `merge pr`, `put back`).
  Singleton, malformed, ineligible, and unknown windows get a row each, with
  the pane title as the summary and the reason no glyph is shown.

### Refresh producer

A new `hive tmux turn-refresh <session>` runs from the existing periodic
slot: the `#(…)` in `status-right`, which tmux re-evaluates every
`status-interval` for each attached client. One process per tick per
session, bounded in time, and it does the whole session's work:

1. list every hive window with `pane_current_path`, `pane_pid`, `pane_tty`,
   `pane_current_command`, and `window_activity`; take one in-memory process
   snapshot restricted to the session's pane TTYs, walk each tree from its
   `pane_pid`, retain only agent kind/presence, and drop ineligible windows;
2. compute each window's pair key and, from the checkout, its HEAD and the
   branch reflog (local, cheap); group windows by key and classify shape;
3. resolve PR state **once per distinct pair key** using the algorithm above:
   an exact immutable merged receipt first, then a fresh mutable entry, then
   `gh`/`fj` lookups. Remote lookups run **in parallel** across keys with at
   most 8 in flight, a 3 s timeout each, and a **whole-producer deadline of
   6 s** from process start. When the deadline passes, the producer stops
   waiting: every key without a result is unknown on this tick, no expired
   mutable value becomes a verdict, and the process proceeds to step 6
   regardless. N simultaneous outages therefore cost one deadline, not N
   timeouts, and the 75 s freshness bound holds for every key that did
   answer;
4. apply the declared-role binding and lifecycle (ignore and clear a
   declaration whose stored key differs from the window's key; clear on
   merged/closed);
5. evaluate roles, liveness, and the baton rule for every group;
6. write `@hive_turn_suffix`, `@hive_turn_role`, `@hive_turn_role_source`,
   and `@hive_turn_target` on **every** eligible window with `tmux
   set-option -w`, whether or not it is selected, and clear them on windows
   that became ineligible;
7. print the pair line for status-right.

Because step 6 touches every window on every tick, both windows of a pair
change together, and a handoff posted while the operator is in another
window is picked up without any window-selection event. Worst-case staleness
after a comment or reopen is one status-interval plus the mutable cache TTL:
75 s. The manual backtick+R refresh also busts every mutable PR entry (never
an immutable merged receipt), for the operator who wants the answer now.
`#(…)` runs only while a client is attached, which is exactly when the label
can be seen.

`label-window` stays what it is; it reads the options the producer wrote and
does no PR work of its own, so the hook path keeps ADR-0002's cost profile.

### Private cache

The producer's mutable cache and merged receipts live under
`${XDG_STATE_HOME:-$HOME/.local/state}/hive/turn/`, **not** under
`/tmp/hive-tmux`. The directory is created with mode `0700` and re-`chmod`ed
to `0700` on every producer start, so a permissive umask cannot widen it.
Each entry is one file per pair-key hash, created with `O_CREAT|O_EXCL` at
mode `0600`, `fchmod`ed to `0600`, written in full, and moved into place
with an atomic rename, the way run-dsl sidecars are written. An entry holds
only the bounded fields the popup and the rule need: pair key, PR number,
state, head SHA or HEAD set, latest comment id, its classification, its first
line truncated to 80 characters, the PR title truncated to 80 characters,
timestamps, and an immutable merged receipt when present. The raw comment
body and pane process arguments are never written to disk.

### Data sources and enrichment

Version 1 uses only tmux formats (`window_activity`, `pane_title`,
`pane_current_command`, `pane_current_path`, `pane_pid`, `pane_tty`), the
in-memory process inventory for those TTYs, `git` in the checkout (origin URL,
default branch, branch, HEAD, the branch reflog), the declared-role input
option, and the cached PR lookup. No hooks, no log parsing. Version 2 adds
optional enrichers — a Claude Code `Stop` hook, a Codex log reader — that may
set the question flag and improve the summary line. An enricher that is
absent, stale, or failing degrades to version 1 behavior; it never produces a
glyph on its own, and it may set a role only by writing
`@hive_turn_role_declared` with the same key binding the operator's command
writes.

## Rationale

The PR is the only handoff record both agents already write, so it is the
only source that is agent-agnostic *and* changes exactly when the baton
moves. Building on it first means the indicator is right for any agent that
follows the workflow, including one that does not exist yet. The corpus
measurement is what makes that claim honest: the grammar is sized to the
comments that exist, and its residue is named.

Identity is `(remote, branch)` because that is the least specific key that
cannot collide inside a hive, and because `hive` already normalizes remotes
for exactly this reason. Eligibility exists because key equality is
necessary for a pair but not sufficient: two idle windows on `main` share a
key and are not a pair, and a shell or arbitrary Node process is not an
agent. Process ancestry and arguments carry the package identity that the
generic `node` basename does not. The shape table exists because the baton
table assumes one implementer and one reviewer, and a group that is not that
shape must say so rather than pick two of its members. Making the PR an
attribute rather than a precondition is what lets the no-PR rows describe a
real pair instead of an empty set.

Merged receipts exist because a time bound on a lookup is a time bound on
the answer: the first draft's seven-day window turned a finished checkout
into an apparent new baton on day eight with no branch, HEAD, or operator
event. Merged is immutable, so a receipt bound to `(pair key, HEAD)` changes
only when the checkout changes. Closed is not immutable: the platform can
reopen a PR without changing its head SHA, so a cached closed observation
must be revalidated and a failed revalidation must become unknown. Requiring
one shared HEAD for terminal group state prevents one stale checkout from
lending its closed/merged state to a different local revision.

Roles come from the branch reflog because it is the one place the checkout
itself records how the branch got there, it needs no network and no agent
cooperation, and it is already written by the commands the workflow uses. A
declared input sits above it because the reflog can expire or be absent, and
because an unusual pairing shape should be sayable in one command rather
than mis-derived. The declaration carries its pair key because the producer
is a new process every tick and has no other way to know what the
declaration was for. The input and the outputs are different options because
a single option that is both would turn one derived tick into a permanent
declaration. Adjacency is gone because a hint that decides in every
undeclared case is not a hint.

The classifier is anchored and guarded rather than permissive because the
78-of-87 grammar that preceded it was compatible with the corpus and unsafe
outside it: it would have shown `✓` for "cannot approve this yet". Anchors
say where a verdict may stand; the negation and family guards say when a line
is not allowed to be a verdict at all; and a finite approval-tail grammar
prevents a syntactically positive word from authorizing a conditional merge.
These all err toward refusal, which costs a glyph and never directs a merge.
The accepted tails cover the corpus's exact-head forms; novel prose uses the
machine marker or resolves to unrecognized instead of growing a blacklist.

The producer has a deadline because a per-lookup timeout bounds one lookup
and nothing else: ten keys and one outage would have run the slot past its
own interval. Parallel lookups plus a whole-process deadline bound the tick
no matter how many keys fail, and every key that fails is reported unknown
rather than left with last tick's glyph. The cache is private because the
data is: PR titles and comment lines from private repositories do not belong
in a world-readable `/tmp`, and the existing convention there was written
for files that hold nothing.

The question override sits above the comment rules because when it exists
it is strictly newer information than the handoff it follows. The producer
rides the `status-right` slot because it is the only periodic thing the
generated config already runs. The last-to-speak heuristic is kept, but
confined to rows 7–8 and stated over `window_activity` so it is the same
computation for both agents. Approval points at the implementer because the
glyph marks where the operator's next keystrokes go, not who acted last.
The label carries only actionable state, following ADR-0002. Enrichment is
additive because the mixed-vendor constraint is not going away.

## Consequences

- `hive tmux` gains one input option (`@hive_turn_role_declared`, carrying
  role and pair key), four output options (`@hive_turn_suffix`,
  `@hive_turn_role`, `@hive_turn_role_source`, `@hive_turn_target`), a
  second suffix in the status format, the `role`, `turn-refresh`, and
  `pairs` subcommands, a popup binding, a cache-busting step in the
  backtick+R refresh, and a private state directory.
- `tests/test_hive_tmux.py` grows table-driven tests over:
  - the baton rule, one fixture per row and per shape (pair, singleton,
    duplicate-role, all-unknown, three windows), plus an unrecognized latest
    comment, a row whose target role has no window, a lookup failure, a
    lookup timeout, and a deadline cut-off;
  - **eligibility:** two idle windows on the default branch, a shell window,
    and an ordinary Node workload on a feature branch, none of which may form
    a group or receive a glyph; live-shaped Claude and Codex process trees,
    including a symlinked Codex entry script and its resolved package path,
    must qualify; agent start and exit on the same pane must add and remove
    eligibility on successive ticks, while a child tool process must not hide
    the qualifying agent ancestor; a failed or malformed process snapshot
    must clear eligibility and any prior glyph;
  - **successive ticks for roles, across a producer-process restart:**
    derive a role from the reflog, remove the reflog evidence, run the
    producer again as a fresh process, and require `unknown`/`none`;
    declare a role, remove the reflog evidence, run again, and require the
    declaration to stand; change the branch, run again, and require the
    declaration ignored and cleared because its stored key no longer
    matches. In-memory state between calls is not permitted to pass these;
  - **identity and terminal state:** two windows with equal branch names on
    different remotes must neither group nor share a cache entry; two
    windows on the same remote and branch with no PR must pair and reach
    rows 7–8; an open PR must remain group-wide when the two local HEADs
    differ, while the same mixed-HEAD group with no open PR must resolve to
    unknown; two or more open PRs on one head ref must resolve to unknown; a
    full nonmatching terminal page must resolve to unknown while a short
    nonmatching page resolves to none; a closed-unmerged PR may show `↩` only
    while its mutable entry is fresh, must become open when the same PR and
    SHA are reopened, and must become unknown when its entry expires and
    refresh fails; a merged receipt must keep showing `↩` through arbitrary
    clock advance and lookup failure; either terminal state must stop matching
    when the branch advances;
  - **the classifier:** every recognized first-line form in the corpus as a
    positive fixture; the corpus residue as negatives; all mutation negatives
    named above plus `Approved pending CI` and `Review at <sha> — approved,
    but changes requested on docs`; `Changes still requested on exact head
    <sha>` as a SHA-bearing disposition; and the `Handoff:` marker overriding
    a contradicting first line, while quoted and conflicting markers do not;
  - **the producer boundary:** N keys that all time out must yield N unknown
    results within one deadline, with no window left carrying the previous
    tick's glyph; the state directory must be `0700` and its files `0600`
    under a `0000` umask and under a `0022` umask; an entry must never contain
    the comment body or pane process arguments;
  - a PR-state transition (completion → disposition) delivered through a
    fake lookup with **no window-selection event**, asserting that one tick
    moves `@hive_turn_suffix` on both windows.
- The status-right slot gains a network dependency, bounded by the 60 s
  cache, the per-lookup timeout, the parallel limit, the producer deadline,
  and one lookup per distinct pair key per TTL rather than per window or per
  tick. A pair shares a cache entry; two same-named branches on different
  remotes do not.
- Reading a pane process tree plus the branch reflog, HEAD, and default branch
  per window per tick is local work; it is in the cost class ADR-0002 accepted
  for the label path, and it is measured before the change ships, the way that
  ADR measured its scan.
- Reflogs expire (`gc.reflogExpire`, 90 days by default) and are absent in
  some clone shapes, so a long-lived branch can lose its derived role. The
  result is *unknown*, shown as such, and one `hive tmux role` command fixes
  it; it is never a silently wrong role, and never a latched one.
- The reflog shapes above were measured for a locally created branch and for
  `gh pr checkout`. `fj pr checkout` also creates a tracking branch, but its
  exact reflog line has not been captured; it goes into the fixture set
  before the derived rule is trusted on a Forgejo hive, and until then those
  windows resolve to unknown rather than to a guess.
- Row 2 does not fire until an enricher exists, so in version 1 a question
  left by the last speaker still reads as a handoff. This is the current
  behavior, so it is a known gap rather than a regression.
- Comment classification is by a bounded, anchored grammar seeded from the
  corpus. If the review discipline changes its wording, the fixtures fail
  first; the `Handoff:` marker is the path to making the grammar a fallback.
- The guards refuse two real comments in the corpus: one pre-discipline
  approval phrased with a negation ("I don't see any blocking issues") and
  one completion whose first line mentions "approved-review nits". Both are
  on closed PRs; both would have shown no glyph, not a wrong one.

## Infra Impact

None.

## Evidence

- Pane titles, current commands, idle times, and TTY process trees were read
  from the two live hive sessions on 2026-09-06 (14 panes, two hives, both
  agents present). The live Claude panes have a `claude` process. The live
  Codex panes have `node <...>/bin/codex`, whose script resolves to
  `<...>/node_modules/@openai/codex/bin/codex.js`, and a descendant native
  `codex` binary under that package. Their generic tmux foreground values do
  not establish those identities.
- Reflog provenance was measured on this repository's own PR branch: the
  originating checkout reports `branch: Created from HEAD`; a fresh clone
  after `gh pr checkout` reports `branch: Created from origin/<branch>` with
  `branch.<name>.remote`/`.merge` set to the tracking upstream.
- The comment corpus is this repository's 87 PR comments across PRs #7–#38,
  read through the API on 2026-09-06. The initial four-phrase grammar
  recognized 65. A loose grammar (approval words anywhere, completion verb
  within four words of a SHA) recognized 78 but accepted every mutation
  negative listed in the classifier section. The anchored, guarded grammar
  in this ADR recognizes **75** (29 completion, 21 approve, 25 changes
  requested, 0 no new delta), with 0 ambiguous and 12 unrecognized: 6
  header-only comments from PRs #7, #10, and #11 (written before the
  discipline fixed its wording), 4 comments that are not handoffs (a
  metadata-only PR-body refresh, a post-merge note, two progress notes), and
  the 2 guard refusals itemized in **Consequences**. All 17 mutation
  negatives are unrecognized; all 18 positive fixtures, including a marker
  case, are recognized.
- The installed GitHub CLI exposes `gh pr reopen`; a closed-unmerged PR is
  therefore mutable without changing its head SHA and cannot support an
  immutable receipt.
- `scripts/hive.py` implements the version-1 producer, structural agent
  identity, role lifecycle, conservative GitHub/Forgejo PR adapter, private
  cache, baton table, four tmux outputs, status line, role command, and pairs
  popup. `tests/test_hive_tmux.py` carries the table, mutation, lifecycle,
  permission, timeout, and tmux-parser regressions described above.
- A read-only live measurement on 2026-09-06 identified all four supported
  agent windows in the current `term-public` session from one TTY-restricted
  process snapshot in 8.4 ms. Process arguments were reduced in memory to an
  agent kind and were not printed or stored.
- The identity and storage facts in **Context** were read from
  `scripts/hive.py`: `_normalize_origin_url` (remote dedup),
  `_label_cache_key` (workspace path hash), `_default_branch` (reads
  `refs/remotes/origin/HEAD`), `_TMUX_DIR = Path('/tmp/hive-tmux')` with
  default-mode `mkdir` calls, and the popup's `tempfile.mkstemp`.
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

## Open Questions

- Should `▶` be suppressed on the currently active window? The glyph exists
  to be seen from elsewhere; on the window the operator is already typing in
  it is noise, but hiding it makes the label flicker on every switch.
- Is one PR lookup per pair key per minute acceptable for GitHub-hosted
  hives, where the API is rate-limited per account, or should GitHub repos
  fall back to rows 7–8 until a hook-written file exists?
- Is 30 s the right active/quiet threshold? Two ticks absorbs a single slow
  redraw, but an agent that pauses between tool calls for longer than that
  will flicker to quiet and back. The number is a constant in one place and
  the fixtures pin it; it may need to move after a week of use.
- Is a 6 s producer deadline right against a 15 s interval? It leaves nine
  seconds for tmux calls and label work; a hive with many keys on a slow
  network may want the interval raised instead of the deadline.
- Should the negation guard admit a positive sentence-final form such as
  `no blocking findings.` when it follows a negation earlier on the line?
  Today it refuses; the corpus cost is one pre-discipline comment.
- Should the review discipline adopt the `Handoff:` marker now, so the
  grammar is a fallback from day one, or after the grammar has shown which
  real comments it misreads?
- Where does version 2 enricher state live, and who clears it when a window
  is reused for a different PR? The `@hive_turn_role_declared` binding is
  the model; the question is whether the question flag carries a key too.

## Revisit Triggers

- An agent CLI ships a stable "waiting for input" signal usable from tmux, or
  the CLIs agree on one. Row 2 becomes a fact instead of an enrichment.
- The PR platform supports distinct identities per session, making the
  comment author sufficient for the role.
- The review discipline adopts the `Handoff:` marker, at which point the
  first-line grammar can be demoted to a fallback and its fixtures frozen.
- Multi-reviewer groups become routine, and the pair shape needs to become
  a set with a rule of its own.
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
- **One `@hive_turn_role` option as both the declaration and the effective
  role.** Rejected after review: the first derived tick would be read back
  as a declaration on the next, the role would never return to unknown when
  its evidence expired, and the popup could not name its source.
- **A declaration without its pair key.** Rejected after review: every
  producer tick is a new process, so a bare `reviewer` cannot be told apart
  from a `reviewer` declared for a branch the window has since left.
- **Pair and cache by bare branch name.** Rejected after review: a hive is
  multi-repository and branch names collide across repositories.
  `(remote, branch)` is the key `hive` already uses for everything else it
  dedupes.
- **Every equal-key group is a pair.** Rejected after review: it would pair
  two idle windows on `main`, and it leaves three windows, duplicate roles,
  and unknown roles undefined. Eligibility and the shape table replace it.
- **Pairing only on an open PR.** Rejected: it made the no-PR rows
  unreachable for a two-window pair, and a reviewer can fetch a branch
  before the PR exists.
- **A seven-day bound on resolving a terminal PR.** Rejected after review: it
  turned an untouched, merged checkout into an apparent new baton on day
  eight. Immutable merged receipts bound to `(pair key, HEAD)` replace that
  bound; closed-unmerged observations stay mutable and are revalidated.
- **An immutable receipt for a closed-unmerged PR.** Rejected after review: a
  closed PR can reopen with the same head SHA, so the receipt would continue
  to show `↩` after the baton became live again.
- **One terminal verdict for a group with mixed local HEADs.** Rejected after
  review: without an open PR, the two checkouts do not name one revision to
  match against a closed or merged PR. The explicit unknown state exposes the
  disagreement instead of lending one checkout's terminal state to the other.
- **`pane_current_command` as agent identity.** Rejected after review: it is a
  foreground basename, and Codex presents the same `node` basename as an
  ordinary Node workload. A TTY-rooted process tree plus structural package
  signatures replaces it.
- **A broad substring classifier over the whole comment body.** Rejected:
  "fixed" inside a findings list would turn a rejection into a completion,
  and the corpus shows the verdict already lives on the first line.
- **A permissive first-line grammar with a negation or conditional
  blacklist.** Rejected after review: it covered 78 of 87 and accepted both
  negative and novel conditional approval prose. Positive anchors, the
  negation and family guards, and a finite set of complete approval tails
  replace it, at a measured cost of two refusals in the corpus.
- **Require a machine-readable marker from day one.** Rejected as a
  precondition: it would make the indicator blind to every comment already
  in the corpus and to any agent that has not adopted the convention. The
  marker is offered as an override that wins when present.
- **Sequential lookups with a per-lookup timeout only.** Rejected after
  review: N failures cost N timeouts and can overrun the status interval,
  leaving later windows stale. Parallel lookups under a whole-producer
  deadline replace it.
- **Cache under `/tmp/hive-tmux` like the label cache.** Rejected after
  review: that directory is `0755`/`0644` by convention and would expose
  private-repository titles and comment lines to other local users. A
  `0700`/`0600` state directory with atomic writes and bounded fields
  replaces it.
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
