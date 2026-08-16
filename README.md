# term-public

Public terminal baseline for macOS with:

- Ghostty
- `zsh` + `oh-my-zsh` + `powerlevel10k`
- a `hive` workflow for multi-checkout hives and `tmux`-backed dev sessions

## Scope

This repo is intentionally narrower than a personal dotfiles repo. It keeps:

- terminal and shell baseline
- `hive` / `apiary` / `tmux` workflow
- a small bootstrap flow

It avoids:

- personal tokens and machine-local secrets
- vendor- or employer-specific paths
- language runtime clutter

## Layout

- `ghostty/` terminal config and theme
- `zsh/` shell config
- `tmux/` base tmux config (carries the Claude-CLI-safe settings)
- `scripts/hive.py` hive/apiary/tmux entrypoint
- `setup.sh` symlink installer
- `setup/bootstrap-macos.sh` package/bootstrap helper
- `local/` untracked per-machine overlay created by `setup.sh`

## Install

1. Run `setup/bootstrap-macos.sh` to install baseline dependencies.
2. Run `./setup.sh` from the repo root to link config files into place.
3. Restart Ghostty and open a new shell.

## Per-Machine Overlay

`setup.sh` creates an untracked `local/` directory in the repo for machine-specific additions.

- `local/env.local` for environment and PATH changes
- `local/zshrc.local` for aliases, functions, and extra shell setup
- `local/bin/` for private helper scripts
- `ghostty/local.config` for machine-specific Ghostty overrides

Template files in `local/` may be committed as examples using the normal
`<real-file>.template` convention. The real file stays untracked. For example:

- `ghostty/local.config.template` is committed
- `ghostty/local.config` is machine-local and ignored

This is the place for things like Node path tweaks, k3s helper scripts, or workstation-only tooling that should not be committed back to the public repo.

## Day / night mode

`ghostty/config` uses an appearance-pair theme: `palenight` in dark mode and
`GitHub Light High Contrast` in light mode — a near-white, high-contrast
theme that stays readable under bright ambient light and glare. Ghostty
follows the macOS system appearance and restyles all live windows instantly,
including everything inside tmux sessions.

Switch modes with:

```bash
term-theme          # toggle day <-> night
term-theme day      # high-ambient-light mode (light background)
term-theme night    # low-light mode (dark background)
term-theme status   # print the current mode
```

The script flips the macOS system appearance, so the whole OS follows — in a
bright room that is usually what you want. For automatic switching at
sunset/sunrise, set System Settings → Appearance → Auto. The first run may
prompt for automation access to System Events.

Ghostty restyles everything it draws instantly, but programs that pick their
own colors need help — the appearance-pair theme only remaps the 16 ANSI
colors, not the 256-color cube or truecolor values most TUIs use. `term-theme`
records the mode in `~/.cache/term-theme/mode` and nudges each consumer:

- **zsh prompt (p10k)** — `p10k.zsh` carries day/night palettes; new shells
  detect the appearance at startup, and running shells pick up a flip at
  their next prompt via the mode file.
- **hive tmux** — `scripts/hive.py` carries per-hive day palettes; sessions
  are styled for the current mode at creation, and `term-theme` restyles
  live sessions via `hive tmux restyle`. Existing shells keep the
  `HIVE_COLOR_*` env they started with until a new pane/window.
- **Claude Code** — pins its theme in `~/.claude.json` (no auto-detection);
  `term-theme` flips it between `light`/`dark`, preserving a
  daltonized/ansi variant and leaving `auto`/custom themes alone. Restart
  running sessions (or use `/theme`) to repaint; a running session may
  clobber the flip when it saves state — re-run `term-theme` if a later
  session comes up wrong.
- **codex** — detects the background itself at startup; restart it after a
  flip.
- **neovim** — follows the appearance at startup via the nvim config
  (`vim.o.background` resolved from the macOS appearance, tokyonight picks
  its day/night style from it). Restart nvim after a flip.

Flips made outside `term-theme` (System Settings, scheduled Auto) restyle
Ghostty but skip these hooks. Afterwards, run `term-theme day` or
`term-theme night` to match — setting the mode it is already in is
idempotent and runs all the hooks. `term-theme status` only refreshes the
mode file (enough for the zsh prompt, not for hive/Claude).

## Hive tmux

Examples:

```bash
hive status --compact
hive create
hive tmux
hive tmux --hive ~/src/infra
hive tmux --list
hive tmux --new-window
```

`hive tmux` starts (or attaches to) a per-hive tmux session — one window per
workspace, a per-hive color theme, automatic window labels, and backtick-prefix
keybindings for the common hive operations. The session survives a closed
terminal or a dropped SSH connection. The `tmux/tmux.conf` base config carries
the settings that make Claude CLI render correctly inside tmux (notably
`allow-passthrough on` plus synchronized output).

## Tests

Run:

```bash
pytest
```

GitHub Actions runs the test suite on push and pull request.
