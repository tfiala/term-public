# term-public

Public terminal baseline for macOS with:

- Ghostty
- `zsh` + `oh-my-zsh` + `powerlevel10k`
- a `hive` workflow for multi-checkout hives and `dtach`-backed persistent shells

## Scope

This repo is intentionally narrower than a personal dotfiles repo. It keeps:

- terminal and shell baseline
- `hive` / `apiary` / `dtach` workflow
- a small bootstrap flow

It avoids:

- personal tokens and machine-local secrets
- vendor- or employer-specific paths
- tmux-specific workflow
- language runtime clutter

## Layout

- `ghostty/` terminal config and theme
- `zsh/` shell config and hive prompt overlay
- `scripts/hive.py` hive/apiary/dtach entrypoint
- `setup.sh` symlink installer
- `setup/bootstrap-macos.sh` package/bootstrap helper

## Install

1. Run `setup/bootstrap-macos.sh` to install baseline dependencies.
2. Run `./setup.sh` from the repo root to link config files into place.
3. Restart Ghostty and open a new shell.

## Hive Shell

Examples:

```bash
hive status --compact
hive create
hive shell
hive shell --hive ~/src/infra
hive shell --number 3
hive shell list
hive shell cleanup
```

`hive shell` uses `dtach`, not tmux, so Claude CLI and similar tools render as they do in a bare terminal while still surviving accidental window closure.

## Tests

Run:

```bash
pytest
```

GitHub Actions runs the test suite on push and pull request.
