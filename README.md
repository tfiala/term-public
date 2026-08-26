# term-public

Public terminal baseline for macOS with:

- Ghostty
- Homebrew `bash` 5.x + Starship prompt + `bash-completion@2`
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
- `bash/` shell config (`bash_profile`, `bashrc`, `inputrc`)
- `starship/` prompt config
- `tmux/` base tmux config (carries the Claude-CLI-safe settings)
- `scripts/hive.py` hive/apiary/tmux entrypoint
- `setup.sh` symlink installer
- `setup/bootstrap-macos.sh` package/bootstrap helper
- `local/` untracked per-machine overlay created by `setup.sh`
- `docs/decisions/` architecture decision records ([index](docs/decisions/README.md))

## Shell: bash + Starship

The interactive shell is Homebrew **bash 5.x** (issue #15). Anthropic and
OpenAI coding agents repeatedly tripped over zsh-isms — unmatched globs
aborting commands, history-style modifiers mangling `"$var:latest"`-shaped
expansions — so the baseline runs the bash they keep assuming. The
zsh + oh-my-zsh + powerlevel10k stack was removed with it; the prompt
chrome (hive badge, git status, PR number) now comes from
[Starship](https://starship.rs), configured in `starship/starship.toml`.

macOS ships bash 3.2 (2007, GPLv2 freeze) at `/bin/bash`, which is not
acceptable as a daily shell — `setup/bootstrap-macos.sh` installs Homebrew
bash, registers it in `/etc/shells`, and `chsh`es the login shell to it.
`tmux/tmux.conf` also pins `default-shell` to Homebrew bash so hive tmux
sessions run it even before (or without) the `chsh`.

vi editing mode with `jk` to escape is set in `bash/bashrc` (bash only);
`bash/inputrc` keeps only mild readline defaults so other readline programs
are unaffected. Ghost-text autosuggestions (ble.sh) were considered and left
out for now; the recall bindings below cover that role.

History matches the zsh-era behavior (which came from oh-my-zsh's
`lib/history.zsh`): entries are timestamped, duplicates and space-prefixed
commands stay out, history expansions (`!!`, `!$`) are loaded for review
instead of run blind, and — the bash spelling of zsh's `share_history` —
every command is flushed to `~/.bash_history` as it runs and the merged
file is reloaded at each prompt, so tmux panes share one live history and
a killed pane loses nothing. `scripts/import-zsh-history.py` does a
one-shot import of an existing `~/.zsh_history` into bash's timestamped
format (`setup.sh` reminds you while one is present).

Recall — the role zsh-autosuggestions played in the zsh era — comes from
two bindings in `bash/bashrc`. Up/Down prefix-search the history: with
`claude ` typed, Up cycles through only the history lines that start with
`claude `. And [fzf](https://github.com/junegunn/fzf)'s keybindings add
Ctrl-R (fuzzy full-history search — match any fragments in any order),
Ctrl-T (fuzzy-insert a file path), and Alt-C (fuzzy cd). Alt-C works
because `ghostty/config` sets `macos-option-as-alt`; fzf itself is
installed by `setup/bootstrap-macos.sh`.

Preview the shell without installing anything:

```bash
scripts/start-shell-preview.sh            # in the current terminal
scripts/start-shell-preview.sh --ghostty  # in a fresh Ghostty window
```

## Install

1. Run `setup/bootstrap-macos.sh` to install baseline dependencies and
   switch the login shell to Homebrew bash (prompts for your password for
   `/etc/shells` and `chsh`).
2. Run `./setup.sh` from the repo root to link config files into place.
   It also unlinks zsh-era symlinks (`~/.zshrc`, `~/.zshenv`,
   `~/.p10k.zsh`) left by earlier versions of this repo — only when the
   link target is verifiably a term-public checkout — restoring `.bak`
   backups where they exist, and prints migration guidance for any
   remaining zsh config (bash does not read it). When those installed links
   point to another checkout of this repository, it first carries customized
   machine-local overlay files into this checkout. Existing customizations in
   the new checkout are never overwritten; conflicts are reported with both
   file paths for manual reconciliation. A conflict exits with status 3 before
   any installed links are replaced, so interactive and unattended callers
   both detect the incomplete switch.
3. Restart Ghostty and open a new shell.

## Per-Machine Overlay

`setup.sh` creates an untracked `local/` directory in the repo for machine-specific additions.

- `local/env.local` for environment and PATH changes
- `local/bashrc.local` for aliases, functions, and extra shell setup
- `local/bin/` for private helper scripts
- `ghostty/local.config` for machine-specific Ghostty overrides

Template files in `local/` may be committed as examples using the normal
`<real-file>.template` convention. The real file stays untracked. For example:

- `ghostty/local.config.template` is committed
- `ghostty/local.config` is machine-local and ignored

On a fresh checkout, `setup.sh` copies the committed templates for
`local/env.local` and `local/bashrc.local` into place. If setup replaces links
to another verified term-public checkout, customized files under `local/`
(including private `local/bin/` helpers) and `ghostty/local.config` migrate when
the destination is absent or still matches its template. Template-only source
files are ignored.

Migration discovery is intentionally link-based. If an earlier setup run has
already replaced every link to the prior checkout, `setup.sh` cannot infer that
checkout's location; copy or reconcile its overlay manually before deleting it.

This is the place for things like Node path tweaks, k3s helper scripts, or workstation-only tooling that should not be committed back to the public repo.

### Installers that edit your shell profile

`~/.bash_profile` and `~/.bashrc` are symlinks into this repo, so an installer
that "adds itself to your PATH" writes into the **tracked** file rather than a
private dotfile. Docker Desktop did this on 2026-08-18, prepending an
`export PATH="$PATH:/Users/<user>/.docker/bin"` block to `bash/bash_profile`.

When it happens, move the block into the right overlay file — matching the
per-machine overlay contract above, and always with `$HOME` rather than a
literal home path:

- a plain **PATH or environment export** goes in `local/env.local`
- an installer's **shell-init or function block** (nvm, pyenv, conda, rbenv,
  SDKMAN) goes in `local/bashrc.local`, since it defines shell functions and
  completions rather than environment

Then remove **only** the installer's hunk from the tracked file. Inspect
first — a whole-file restore would also discard any in-flight edits of your
own:

```bash
git diff bash/bash_profile        # or bash/bashrc — see exactly what changed
git restore -p bash/bash_profile  # interactive: discard just the installer hunk
```

`git restore -p` walks the file's hunks and asks about each one. If the
installer's lines share a hunk with an edit you want to keep, use `s` to split
it or `e` to edit the hunk before accepting. A whole-file
`git restore bash/bash_profile` is safe only once `git diff` has shown the
installer block is the sole change.

#### What the guard covers

`tests/test_no_machine_specific_config.py` rejects four shapes in tracked
config: a hardcoded home directory, a known installer marker comment, a
`$HOME`-parameterized version-manager init block, and any unrecognized block
prepended to a linked file (caught because it displaces the file's own first
line). The set of linked files is derived from `setup.sh`'s own link table
rather than restated, so adding a linked file forces the guard to advance.

This table is the single inventory of covered installers: each row has a
committed fixture built from the line the tool actually emits, and the tests
assert this table and the fixture set match exactly in both directions.

| Installer | Emitted shape | Detected as |
|---|---|---|
| `docker desktop` | marker comment + `export PATH="$PATH:$HOME/.docker/bin"` | installer marker |
| `conda` | `# >>> conda initialize >>>` block | installer marker, third-party init |
| `rustup` | `. "$HOME/.cargo/env"` | third-party init |
| `nvm` | `export NVM_DIR="$HOME/.nvm"` + `nvm.sh` source | third-party init |
| `pyenv` | `export PYENV_ROOT="$HOME/.pyenv"` + `pyenv init` | third-party init |
| `rbenv` | `export RBENV_ROOT="$HOME/.rbenv"` + `rbenv init` | third-party init |
| `sdkman` | `export SDKMAN_DIR="$HOME/.sdkman"` + `sdkman-init.sh` | third-party init |
| `google cloud sdk` | `path.bash.inc` source + marker comment | installer marker, third-party init |
| `jetbrains toolbox` | `# added by JetBrains Toolbox` + PATH | installer marker |

**Boundary:** that test runs in CI, which is after a push. It blocks
integration, but it cannot stop a literal path from first appearing in a commit
on a public branch. Preventing that would need a local pre-push hook, which
this repo does not currently install.

## Day / night mode

`ghostty/config` uses an appearance-pair theme: `palenight` in dark mode and
`GitHub Light High Contrast` in light mode — a near-white, high-contrast
theme that stays readable under bright ambient light and glare. Ghostty
follows the macOS system appearance and restyles all live windows instantly,
including everything inside tmux sessions.

The Starship palette in `starship/starship.toml` uses only named ANSI
colors (`green`, `bright-black`, ...), never numeric 256-cube or hex
values: each Ghostty theme maps ANSI 0-15 against its own background, so
the prompt adapts to day and night automatically. Keep that constraint
when changing prompt colors — a fixed 256-cube value that looks fine on
one background is the washed-out case on the other.

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

- **bash prompt (Starship)** — needs no nudge: the palette is named ANSI
  colors only, so Ghostty's restyle recolors the prompt instantly, running
  shells included. That constraint is what removed the old p10k mode
  watcher; keep it when changing prompt colors.
- **hive tmux** — `scripts/hive.py` carries per-hive day palettes; sessions
  are styled for the current mode at creation, and `term-theme` restyles
  live sessions via `hive tmux restyle`. Existing shells keep the
  `HIVE_COLOR_*` env they started with until a new pane/window.
- **tmux copy-mode & messages** — needs no nudge, the opposite way from
  Starship: tmux's defaults for the mouse-drag selection (`mode-style`),
  search matches, and the message bar are ANSI named colors, and the day
  theme maps yellow/cyan/magenta/red to dark shades — dark-on-dark, an
  invisible selection. `tmux/tmux.conf` pins those styles in truecolor
  hex, which both themes render identically, so they never flip with the
  mode. Keep that constraint when changing them.
- **Claude Code** — stores its theme in `~/.claude/settings.json` (the
  `/config` preferences moved there in 2.1.119); `term-theme` flips it
  between `light`/`dark`, preserving a daltonized/ansi variant and leaving
  `auto`/custom themes alone — the supported `auto` theme already follows
  the terminal appearance by itself, so on `auto` there is nothing to
  sync. Restart running sessions (or use `/theme`) to repaint.
- **codex** — its accent palette (status line, inline code, links) comes
  from its syntax theme, not the terminal: background detection gets no
  answer inside tmux and falls back to the dark default, whose pastel
  truecolors wash out on the day background. `term-theme` flips
  `tui.theme` in `~/.codex/config.toml` between `catppuccin-latte` (day)
  and `catppuccin-mocha` (night), leaving any other deliberately pinned
  theme alone. Restart codex after a flip.
- **neovim** — follows the appearance at startup via the nvim config
  (`vim.o.background` resolved from the macOS appearance, tokyonight picks
  its day/night style from it). Restart nvim after a flip.

Flips made outside `term-theme` (System Settings, scheduled Auto) restyle
Ghostty but skip these hooks. Afterwards, run `term-theme day` or
`term-theme night` to match — setting the mode it is already in is
idempotent and runs all the hooks. `term-theme status` only refreshes the
mode file (the non-macOS fallback `hive tmux` reads), not the hive/Claude
hooks.

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
workspace, a per-hive color theme, compact numeric window tabs, and
backtick-prefix keybindings for the common hive operations. The current tab is
bracketed independent of color, while run-state glyphs remain beside the window
number. The selected branch appears once at right and contracts by task-word
initials as space tightens (`fix/automatic-publication-receipt-ledger` becomes
`fix/aprl`, then `f/aprl`, then `aprl`). The session survives a closed terminal
or a dropped SSH connection. The `tmux/tmux.conf` base config carries the
settings that make Claude CLI render correctly inside tmux (notably
`allow-passthrough on` plus synchronized output) and pins `default-shell` to
Homebrew bash.

## Tests

Run:

```bash
pytest
```

GitHub Actions runs the test suite on push and pull request.
