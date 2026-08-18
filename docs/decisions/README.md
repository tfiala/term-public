# Architecture Decisions

This directory stores Architecture Decision Records (ADRs) for `term-public`.

The convention is adopted from `infra/home-dc` and validated with `adr-lint`,
which comes from `infra/acc` and is installed to `~/bin` by that repo's
`copy-scripts`.

## Format

```markdown
# ADR-NNNN: Title

**Kind:** decision | proposal | umbrella | informational | assessment | analysis | record
**Status:** proposed | accepted | implemented | validated | rejected | superseded | deprecated | deferred
**Date:** YYYY-MM-DD
**Revisit:** Conditions that should trigger re-evaluation
**Supersedes:** none
**Superseded-By:** none
**Related:** ADR-NNNN, #N

## Context

## Decision

## Rationale

## Consequences

## Infra Impact

## Evidence

## Revisit Triggers

## Alternatives Considered
```

Notes:

- Required sections vary by `Kind`; the list above is for `decision`. Run
  `adr-lint` rather than guessing.
- `## Infra Impact` is required for binding kinds (`decision`, `proposal`,
  `umbrella`) dated on or after 2026-05-04. `None.` is an accepted body, and is
  the usual answer in this repo — `term-public` is local shell/terminal config
  with no cluster surface.
- Status is lowercase and carries no progress prose; put rollout state in the
  body, not in the `Status:` line.
- Keep sensitive values out of ADRs. Reference file paths instead of copying
  secrets, and cite history/log *counts* rather than pasting command history.

## Linting

`adr-lint` is not wired into this repo's CI; run it locally before opening a PR:

```bash
adr-lint --mode strict --check-index docs/decisions
```

Zero errors and zero warnings is the bar.

## Current Decisions

| ADR | Title | Status | Revisit |
|-----|-------|--------|---------|
| [0001](0001-shell-history-hygiene.md) | Shell History Hygiene — Deliberate Removal, Not Exit-Status Culling | accepted | A pre-execution validity signal becomes available; targeted removal proves too manual in practice; bash or the line editor gains a native forget-this-entry primitive; or the interactive shell moves off bash |
