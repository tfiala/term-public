# OBS-0001: Muse architecture and quality evaluation

**Date:** 2026-09-22
**Source:** Muse Code powered by Meta Muse Spark (session `verdant-radiance`)
**Scope:** repo-wide assessment, no code changes

## Summary

Public terminal baseline for macOS + RHEL 9 / Ubuntu SSH targets
(Ghostty, bash 5 + Starship, tmux, Neovim launchers, `hive` workflow).
Narrow scope, explicitly excludes secrets and language runtimes.
Overall quality is well above typical dotfiles: fully green suite,
CI that exercises the real bootstrap, documented decisions.

## Architecture

- Config-linking model: `setup.sh` (484 lines) symlinks `bash/`,
  `ghostty/`, `tmux/`, `starship/` into `$HOME`; per-machine overrides
  live in untracked `local/` overlay with backup/conflict handling
  (conflict exits 3 before replacing links).
- `scripts/hive.py` (4,838 lines, 161 top-level defs, 7 classes:
  `_Colors`, `_Spinner`, `SyncAction`, `RemoteProfile`, `RepoStatus`,
  `RemoteCache`, `_TurnWindow`) is the functional core — status, pull,
  pr-check, issues, create, local, apiary, tmux subcommands. Header marks
  this copy canonical; other repos vendor from here.
- Bootstrap split by OS: `setup/bootstrap-macos.sh` (37 lines, Homebrew
  bash + `chsh`) vs. `setup/bootstrap-linux.sh` (649 lines, apt/dnf,
  `--dry-run`, no-sudo handling).
- Thin configs elsewhere: `bash/bashrc` (282), `tmux/tmux.conf` (111),
  `starship/starship.toml` (91), `ghostty/config` (18). Bash-over-zsh
  choice is documented in README/ADRs.

## Quality — strengths

- `pytest -q`: **1031 passed in ~19s** (verified 2026-09-22).
- ~11.8k lines of test vs. ~7.5k prod (~1.5:1). Per-component files:
  `test_hive.py` (2,982), `test_hive_tmux.py` (2,637), plus bootstrap,
  cutover, terminfo, ssh-agent, installer-pollution guards
  (`test_no_machine_specific_config.py` encodes a real Docker Desktop
  incident).
- CI (`.github/workflows/ci.yml`): unit tests plus real bootstrap +
  link + smoke on Ubuntu native and AlmaLinux 9 container.
- `docs/decisions/` ADRs with lint convention and index test
  (`test_adr_index.py`); 30KB README with layout/scope/install notes.

## Quality — risks

1. `hive.py` is a monolith (4.8k lines, 160+ functions). Hard to
   split-review, merge hotspot. `tmux` / `pairs` / `pr-check`
   subsystems are natural extraction candidates.
2. No static analysis in CI: no shellcheck/shfmt for ~1.2k lines of
   bash, no ruff/mypy for Python. Correctness rests on tests alone.
3. `adr-lint` is documented but manual-only, not wired into CI.
4. Stale `__pycache__/` dirs present in `scripts/` and `tests/`
   working tree; confirm `.gitignore` covers them so vendored copies
   stay clean.

## Evidence

- `pytest --collect-only -q` → 1031 collected; `pytest -q` → 1031 passed
- `wc -l` totals: prod ~7.5k, tests ~11.8k (see section line counts)
- `git log --oneline -15`: active history, hive/setup/bootstrap fixes
- `grep -ri shellcheck|ruff|shfmt .github/ setup.sh README.md` → no hits
