# OBS-0001: Muse architecture and quality evaluation

**Date:** 2026-09-22 (revised per review feedback)
**Source:** Muse Code powered by Meta Muse Spark (session `verdant-radiance`)
**Scope:** repo-wide assessment, no code changes

## Summary

Public terminal baseline for macOS + RHEL 9 / Ubuntu SSH targets
(Ghostty, bash 5 + Starship, tmux, Neovim launchers, `hive` workflow).
Narrow scope, explicitly excludes secrets and language runtimes.
The suite represents heavy investment for a dotfiles repo (1031 tests,
~1.5:1 test-to-prod ratio, real-bootstrap CI); effectiveness evidence
and residual gaps are assessed below rather than assumed from counts.

## Architecture

- **Linked configs vs. copied executables — a deliberate split.**
  `setup.sh:63-73` (`backup_and_copy_file`) explains it: executables
  installed into `~/bin` are *copies, never symlinks*, because other
  tooling also installs by copying (`infra/home-dc`
  `scripts/copy-scripts.py` fetches hive from this repo's main) and a
  symlink lets that write travel through into this checkout's
  `scripts/`, overwriting the canonical source (seen 2026-08-16, when
  a copy of main clobbered an in-review branch). The accepted cost is
  staleness until the next `setup.sh` run. Dotfiles stay symlinked via
  `backup_and_link_file`, with backup/conflict handling (conflict
  exits 3 before replacing links). Per-machine overrides live in the
  untracked `local/` overlay; existing customizations are never
  overwritten.
- **`hive.py`: single-file distribution is the constraint.**
  `scripts/hive.py` (4,838 lines; 154 top-level functions + 7 classes
  = 161 combined definitions: `_Colors`, `_Spinner`, `SyncAction`,
  `RemoteProfile`, `RepoStatus`, `RemoteCache`, `_TurnWindow`) is the
  functional core — status, pull, pr-check, issues, create, local,
  apiary, tmux. Its header marks this copy canonical; other repos
  vendor it by copying one standalone file. That is why the earlier
  "extract subsystems" suggestion is withdrawn as stated: splitting
  `tmux` / `pairs` / `pr-check` into modules would break the
  copy-one-file install/vendor contract unless the distribution
  mechanism changes with it. The real coupling to watch is internal:
  subcommands share discovery, status-classification, and
  tmux-window-label machinery, so a change to repo-state semantics
  ripples across status, pull, and window labels together.
- **Work preservation is the load-bearing invariant.** `pull`
  fast-forwards only; dirty, diverged, and held repos are skipped with
  a printed reason, never rewritten. Holds are explicit per-repo
  markers (`git config hive.hold "<reason>"`). Divergence is checked
  against freshly fetched refs so a failed fetch can never be misread
  as divergence against a stale remote-tracking ref
  (`scripts/hive.py` near `_is_diverged`).
- **External state is cached narrowly.** `RemoteCache` lives for one
  `pull` invocation only — no cross-run persistence to go stale.
  Tmux window-label and turn-indicator caches carry explicit TTLs
  (`_LABEL_CACHE_TTL = 300`, `_TURN_CACHE_TTL = 60`) with stale-entry
  clearing on write. The freshness risk therefore sits at the outer
  edges, not in the cache design: the installed `~/bin` copy going
  stale between `setup.sh` runs, and vendored copies in other repos
  drifting from this canonical source.
- Bootstrap split by OS: `setup/bootstrap-macos.sh` (37 lines, Homebrew
  bash + `chsh`) vs. `setup/bootstrap-linux.sh` (649 lines, apt/dnf,
  `--dry-run`, no-sudo handling).
- Thin configs elsewhere: `bash/bashrc` (282), `tmux/tmux.conf` (111),
  `starship/starship.toml` (91), `ghostty/config` (18). Bash-over-zsh
  choice is documented in README/ADRs.

## Quality — what the tests actually prove

Counts show investment, not effectiveness. The effectiveness cases:

- `tests/test_tmux_conf.py`: ~28 adversarial mutation shapes for the
  color guard (quoting variants, line continuations, `if-shell`,
  `bind` prefixes, near-miss option names like `mode-sty`,
  variable-indirection `$REVIEW_STYLE_OPTION`) must all still trip the
  hex-only check, while compliant truecolor lines must not false
  positive; required-style single-deletion mutations and duplicate
  assignments must fail. This is guard-against-regression testing,
  not just happy-path coverage.
- `tests/test_no_machine_specific_config.py::TestNoLeakEndToEnd`:
  spawns a real `pytest` subprocess with an injected canary sentinel
  and asserts the sentinel never appears in actual pytest output —
  because a sibling assertion's operand rendering could leak rejected
  content even when the scanner's own message is clean. It also pins
  third-party init tokens absent from tracked config.
- Failure-path coverage: `test_bootstrap_linux.py::TestRecovery`
  runs real-mode against a fake system (index-refresh failure must not
  abort; fzf re-attempted on rerun, not skipped); dry-run output must
  show concrete download/verify/install commands, not project names.
- `tests/test_adr_index.py` validates ADR field vocabulary, required
  and nonempty sections per kind, recent-binding `Infra Impact`
  presence, and index↔file consistency — in CI, not just locally.

## Quality — coverage limits (local vs. CI)

- Local (macOS): **1031 passed**. Push CI (Linux runners) reported
  **1022 passed, 9 skipped**: the skips are platform-conditional
  (`Ghostty app bundle not installed`, `no release target for
  $MACHINE`, terminfo-guard paths) — tests that only mean something
  on macOS. Consequence: green CI never exercises the macOS-only
  surface (Ghostty bundle paths, `/etc/shells` + `chsh`,
  Homebrew-bash pinning). Real macOS integration is unverified by
  automation; Linux bootstrap is the part CI proves for real
  (package installs, fallbacks, link, login shell, smoke).
- Line ratio (~11.8k test vs. ~7.5k prod) is reported as investment
  context only.

## Risks — verified and reprioritized

1. No static analysis in CI: no shellcheck/shfmt for ~1.2k lines of
   bash, no ruff/mypy for Python. The mutation and recovery tests
   above mitigate but do not replace it; a mistyped external name
   (container, key, env var) passes every test that shares the typo.
2. Higher-value review surfaces (evaluated as surfaces, not asserted
   defects): credential handling — `hive.py` carries no tokens or
   secrets (the only "token" hits are fence-parsing locals) and
   shells out to `gh`/`fj`; installed-copy and vendored-copy
   staleness (accepted cost, drifts silently); external contracts
   the tests pin by behavior (terminfo entries, Starship/fzf
   bindings, Forgejo CLI shapes); cache TTL adequacy under rapid
   window-hook firing.
3. ADR lint is manual for a documented reason: `adr-lint` ships via a
   private distribution unavailable to the public runners
   (`docs/decisions/README.md`). `test_adr_index.py` already enforces
   vocabulary, sections, and index consistency in CI; the residual
   gap is prose/style checks only. Not a CI gap to file.
4. Withdrawn: bytecode staleness. `*.pyc` and `tests/__pycache__/` are
   git-ignored and **0 cache files are tracked** (`git ls-files |
   grep -c "pycache|\.pyc"` → 0). Local `__pycache__/` dirs are
   runtime residue, not a vendoring risk.

## Evidence

- `pytest --collect-only -q` → 1031 collected; `pytest -q` → 1031 passed
- `wc -l` totals: prod ~7.5k, tests ~11.8k (see section line counts)
- `python3 -c` AST parse of `scripts/hive.py` → 154 functions,
  7 classes, 161 combined
- `rg -ni 'shellcheck|ruff|shfmt' .github setup.sh README.md` → no hits
- `git ls-files | grep -c "pycache|\.pyc"` → 0 (nothing tracked)
- `git diff --check` → clean
