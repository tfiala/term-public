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
