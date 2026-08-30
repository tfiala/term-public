# ADR-0002: Hive Window Run Indicator — Live Runs Only, Rolled Up Over the Workspace Subtree

**Kind:** decision
**Status:** accepted
**Date:** 2026-08-30
**Revisit:** The sidecar directory gains an index or a retention policy that makes a full scan cheap, which would make a richer per-workspace summary affordable in the label; run-dsl starts writing a terminal record that distinguishes "deliberately stopped" from "died unexpectedly", which would make an interrupted-state glyph actionable; or the status bar gains room for more than a few characters per window.
**Supersedes:** none
**Superseded-By:** none
**Related:** #33

## Context

`hive tmux` renames each tmux window to reflect its workspace and appends a
single-character run-dsl status suffix, driven by `_RUN_LABEL_INDICATOR`:
`●` running, `✗` failed, `…` interrupted, and nothing for succeeded. The state
came from `_workspace_run_state`, which selected the most recent sidecar under
`~/.local/state/acc-runs/` whose `work_dir` was **exactly equal** to the
workspace path.

In practice the indicator stopped describing anything. Two windows of the
`hf-suite` hive had displayed a fixed `✗` and `…` for months across full
terminal restarts, because the glyph is recomputed from disk rather than held
in session state. Measured on the live sidecar directory at the time of this
ADR:

- 20 hive workspaces were carrying a stuck glyph; the newest was 29 days old
  and the oldest 176 days.
- 101 of 179 `hf-suite` runs (56%) had a `work_dir` **below** the workspace
  root, almost all inside `.local/<repo>` clones. Exact-match discarded every
  one of them. `suite-8` had never had a run at its workspace root at all, so
  its indicator could not light up under any circumstance.
- The scan read all 14,607 manifests per window, on every relabel — roughly
  408 ms per window switch, 3.26 s for an eight-window refresh.

Three faults compounded:

1. **No age bound.** `failed` and `interrupted` are terminal, so once set they
   never changed. The indicator degraded from signal into permanent decoration.
2. **Blind to `.local`.** The unit the label described (one exact `work_dir`)
   was not the unit the user works in (a workspace and its clones).
3. **`interrupted` conflated "died" with "we stopped looking."** A missing
   `status.json` *or* a stale heartbeat both produce `…`, so every Ctrl-C,
   crash, and reboot left a permanent mark indistinguishable from a real
   failure. This is the same defect class as folding a failed observation into
   a definite verdict in a wait loop.

## Decision

The window label carries **only live runs**, rolled up over the workspace
subtree:

- `●` when exactly one run is live at or under the workspace, `●N` for N
  concurrent runs, and nothing at all when none are.
- "At or under" is a path-ancestor test, so a run in
  `suite-5/.local/corpus` lights up `suite-5`.
- `✗` and `…` are removed from the label entirely.

Terminal states move to the backtick+a popup (`_tmux_runs`), which gains one
row per `work_dir` rather than per workspace, each named by its subpath.

## Rationale

Live state is the only state that belongs in a status bar. It is backed by a
heartbeat with a 300-second TTL, so it **self-clears by construction** — no
staleness cutoff to tune, and no way for it to go wrong the way the terminal
states did. It also answers the question the status bar is actually for: is an
agent working in there right now.

Terminal states are real information, but they are not glanceable-in-one-
character information. A single glyph cannot say *which* of a workspace's
clones failed or why, and a workspace routinely has several. The popup can say
both, and it is where you would go to act on a failure anyway. Adding a
staleness cutoff to the label was considered and rejected: it would have kept a
signal that still could not name its own subject.

The subtree rollup follows from the same reasoning. The window represents a
workspace, and `.local/<repo>` work is that workspace's work. Rolling up is
what makes the indicator describe the thing the window is named after.

## Consequences

- Windows are quiet by default and only mark active work. The 20 stuck glyphs
  clear on the first relabel after deployment.
- Restricting the label to live runs makes the hot path cheap: a live run
  rewrites `runtime.json` on every heartbeat, so its mtime is never older than
  its own `last_heartbeat_at`, and any sidecar past the TTL is skipped on one
  `stat()` without opening either JSON file. With `os.scandir` replacing
  `iterdir`, a window switch went from ~408 ms to ~32 ms — 12.7x — and a full
  eight-window refresh from 3.26 s to 0.26 s.
- A failure is no longer visible without opening the popup. This is intended:
  the previous behavior did not reliably show failures either (it missed 56% of
  runs), it just looked like it did.
- `_workspace_run_state` is removed. `_subtree_run_states` supersedes it for
  the popup, and `_workspace_live_runs` serves the label.
- The underlying sidecar accumulation (14,607 directories, largely fixtures
  from `acc`'s own test suite) is untouched and remains an `acc` concern.

## Infra Impact

None.

## Evidence

- Live `hf-suite-0` window names carried the glyphs directly
  (`suite-5 ✗`, `suite-6 …`); `automatic-rename` is off, so these came from
  `hive`'s own `tmux rename-window`, not from tmux flags.
- The two sidecars responsible were 100 days (`success: false`) and 126 days
  (no `status.json`) old.
- End-to-end check after the change: a live sidecar with
  `work_dir = suite-5/.local/corpus` produced window name `suite-5 ●` and
  `@hive_run_suffix = ●`, and both cleared when the sidecar was removed. The
  pre-change code could not have shown this at all.
- Timings above measured on the live sidecar directory (14,607 entries).

## Revisit Triggers

- A retention policy or index for `~/.local/state/acc-runs/` lands, making a
  full scan cheap enough for a richer label.
- run-dsl records a terminal reason that separates a deliberate stop from an
  unexpected death, making an interrupted glyph actionable.
- Concurrent runs per workspace grow common enough that a bare count stops
  being useful and the popup becomes the primary surface.

## Alternatives Considered

- **Keep all four states, add a ~24h staleness cutoff and the subtree
  rollup.** Rejected: it retains the ambiguous `interrupted` category and the
  one-glyph-for-N-runs problem, and requires tuning a cutoff that the live-only
  design does not need.
- **Remove the indicator entirely.** Rejected: it discards `●`, the one part
  that tracks reality and cannot go stale.
- **Roll up without a count** (`●` regardless of how many). Rejected: the count
  is one character and distinguishes one agent from five, which is exactly the
  case where a glance is worth something.
