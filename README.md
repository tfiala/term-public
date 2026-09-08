# term-public

Public terminal baseline for macOS, and for the Linux hosts (RHEL 9 family,
Ubuntu) you reach from it over SSH, with:

- Ghostty (macOS; Linux hosts are its SSH targets)
- `bash` 5.x + Starship prompt + `bash-completion` 2.x (Homebrew bash on
  macOS, the system bash on Linux)
- LazyVim, NvChad, and AstroNvim config checkouts with short launcher aliases
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
- `neovim/configs.bash` Neovim config sources, app names, and aliases
- `scripts/hive.py` hive/apiary/tmux entrypoint
- `setup.sh` config linker and Neovim config installer
- `setup/bootstrap-macos.sh` package/bootstrap helper (macOS)
- `setup/bootstrap-linux.sh` package/bootstrap helper (RHEL 9 family, Ubuntu)
- `setup/term-public-ssh-agent.service` per-user agent for Linux releases that ship
  no socket-activated one of their own
- `ghostty/xterm-ghostty.terminfo` vendored terminfo source, compiled by
  `setup.sh` on hosts without the Ghostty app bundle
- `tests/smoke/bootstrap-linux.sh` post-bootstrap check for a Linux host
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
sessions run it even before (or without) the `chsh`. Linux already ships
bash 5.x as `/usr/bin/bash`; there `setup/bootstrap-linux.sh` leaves the
system shell alone and `tmux.conf` pins that path instead (see
[Linux hosts](#linux-hosts-rhel-ubuntu)).

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

1. Install the dependencies for your OS:
   - **macOS:** run `setup/bootstrap-macos.sh` to install baseline
     dependencies and switch the login shell to Homebrew bash (prompts for
     your password for `/etc/shells` and `chsh`).
   - **RHEL 9 family / Ubuntu:** run `setup/bootstrap-linux.sh` (add
     `--dry-run` first to see the plan). Details under
     [Linux hosts](#linux-hosts-rhel-ubuntu).
2. Run `./setup.sh` from the repo root to link config files into place and
   clone any missing Neovim configs into `$XDG_CONFIG_HOME` (default
   `~/.config`).
   Existing Neovim config directories are never updated or replaced.
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

## Neovim configs

`setup.sh` installs three independent public config repositories. The shell
aliases select them through Neovim's `NVIM_APPNAME`; they are available when
`nvim` is on `PATH` (or installed as `~/apps/nvim.appimage`).

| Alias | `NVIM_APPNAME` | Source | Branch |
|---|---|---|---|
| `vl` | `nvim-lazyvim` | [`tfiala/nvim-config`](https://github.com/tfiala/nvim-config) | `tfiala` |
| `vn` | `nvim-nvchad` | [`tfiala/nvim-nvchad`](https://github.com/tfiala/nvim-nvchad) | `tfiala` |
| `va` | `nvim-astro5` | [`tfiala/nvim-astro5`](https://github.com/tfiala/nvim-astro5) | `tfiala` |

The repositories are cloned from their personalized `tfiala` configuration
branch with shallow history (their default `main` branches are upstream
starter baselines, not these installed configs).
After installation, each directory is an ordinary working tree: setup leaves
its branch, remotes, local changes, and lockfiles alone on every later run.
`v` continues to launch the default `nvim`, and `vv` presents every `nvim-*`
directory under the XDG config home as a numbered selector.

## Linux hosts (RHEL, Ubuntu)

A Linux host gets the same shell stack as the Mac — bash 5 + Starship, tmux
with the hive config, fzf/ripgrep/fd/bat/eza/gh/jq — and is treated as the
SSH target of a Ghostty window rather than a Ghostty host. Supported: the
RHEL 9 family (RHEL, Rocky, AlmaLinux, CentOS Stream; Fedora takes the same
`dnf` path) and Ubuntu/Debian.

```bash
setup/bootstrap-linux.sh --dry-run   # print the plan: every privileged/network command, nothing changed
setup/bootstrap-linux.sh             # install (sudo, or run as root; without either see below)
./setup.sh                           # link config, install the xterm-ghostty terminfo
tests/smoke/bootstrap-linux.sh       # optional: prove it took
```

What the bootstrap does, and the order it does it in:

- **Package manager first, upstream release second — per tool.** Each tool
  is skipped if already complete, then requested from `dnf`/`apt` one
  package per call, and only if that fails — or reports success while the
  tool is still incomplete — is it installed from the project's own GitHub
  release (starship, ripgrep, fd, bat, eza, gh, jq, fzf have one; tmux,
  git, bash-completion, python3 do not and are reported instead). Release
  binaries go to `/usr/local/bin` (`--bin-dir` overrides), are fetched over
  HTTPS, and are checked against the project's per-asset `.sha256` where
  one is published. Nothing is piped from `curl` into a shell. fzf is two
  artifacts, the binary and the shell bindings `bash/bashrc` sources; each
  is checked and repaired on its own, at the installed binary's version, so
  an interrupted run or a hand-installed bare binary is completed on the
  next run rather than skipped as present.
- **Never a distribution upgrade.** The script installs a tool set; it does
  not run `dnf upgrade` or `apt-get upgrade`, and a failed package-index
  refresh is a warning, not a stop. On Ubuntu the `needrestart` post-install
  scan is suspended for these installs (nothing here is a service), so the
  one-package-per-call loop does not print its report seven times. A RHEL host whose subscription has
  lapsed still gets everything EPEL and its enabled repos provide (fzf,
  ripgrep, fd, bat, gh live in EPEL, which the script enables when
  missing), and the upstream fallback covers the rest — on RHEL 9 that is
  starship and eza, which no enabled repo carries. One unreachable repo or
  missing package never aborts the run; the summary lists anything it could
  not install and exits non-zero in that case.
- **Without root or sudo** the package-manager steps are skipped, the
  upstream releases go to `~/bin` (first on PATH via `bash/bashrc`), and the
  tools that only a package can provide are reported. Enough for a working
  prompt on a box you do not administer.
- **`--dry-run`** prints each privileged command and, for every upstream
  fallback, the concrete download, verification and install commands it
  would run, with `<latest>` standing in for a release version that is
  resolved over the network at run time.
- **Login shell.** Linux already ships bash 5.x, so nothing is installed
  or registered in `/etc/shells`; `chsh -s /bin/bash` runs only when the
  account's login shell is something else (it prompts for your password).
- **Ghostty is not installed.** `setup.sh` compiles the vendored
  `ghostty/xterm-ghostty.terminfo` into `~/.terminfo`, so an SSH session
  from a Ghostty window keeps `TERM=xterm-ghostty` instead of the
  `xterm-256color` fallback in `bash/bashrc`. Regenerate the file after a
  Ghostty upgrade with the command in its header; the test suite compares
  it against the installed bundle on a Mac.

Other differences from the macOS baseline:

- `tmux/tmux.conf` pins `default-shell` to `/usr/bin/bash` on Linux (the
  line is inert on macOS, which has no such path); a hand-built
  `/usr/local/bin/bash` still wins.
- `bash/bashrc` finds `bash-completion` and fzf's key bindings at the RHEL
  (`/usr/share/fzf/shell`), Debian (`/usr/share/doc/fzf/examples`) and
  `~/.fzf/shell` (upstream fallback) locations as well as the Homebrew
  prefixes.
- On Ubuntu the packages install `fdfind` and `batcat`; the bootstrap links
  `~/bin/fd` and `~/bin/bat` to them so the upstream names work.
- `bash/bash_profile` sources `~/.profile` when — and only when — bash
  itself would have read it: no prior `~/.bash_profile` (preserved as
  `.bak`) and no `~/.bash_login`. Ubuntu's stock `~/.profile` is the
  account's login file and puts `~/bin` and `~/.local/bin` on PATH;
  linking `~/.bash_profile` would otherwise shadow it silently.
- `term-theme` is macOS-only (it flips the system appearance). On Linux,
  `hive tmux` reads the mode from `~/.cache/term-theme/mode` if present
  and defaults to night otherwise.
- **ssh-agent.** macOS hands every login session an agent: launchd runs
  `com.openssh.ssh-agent` and injects `SSH_AUTH_SOCK`. Linux has no
  equivalent for an sshd-spawned shell, and what the distribution provides
  varies by release. Checked against the packages themselves:

  | Release | `openssh-client` | user units shipped | socket path |
  |---|---|---|---|
  | Ubuntu 26.04 | 1:10.2p1 | `ssh-agent.service` + `.socket` | `$XDG_RUNTIME_DIR/openssh_agent` |
  | Debian 13 | 1:10.0p1 | `ssh-agent.service` + `.socket` | `$XDG_RUNTIME_DIR/openssh_agent` |
  | Fedora 41 | 9.9p1 | `ssh-agent.service` + `.socket` | `$XDG_RUNTIME_DIR/ssh-agent.socket` |
  | Ubuntu 24.04 / 22.04 | 1:9.6p1 / 1:8.9p1 | `ssh-agent.service` only | — |
  | Debian 12 | 1:9.2p1 | `ssh-agent.service` only | — |
  | RHEL 9 family | — | none | — |

  **Where a socket unit exists**, its `SSH_AUTH_SOCK` is set in the systemd
  *user manager's* environment, which `sshd`'s children never inherit — so
  the agent is listening and no shell can reach it, and `git fetch` over SSH
  fails with `Permission denied (publickey)` on a passphrase-protected key.
  `bash/bashrc` adopts the socket when the agent behind it answers, above the
  interactive-only cutoff so `ssh host 'git fetch'` gets it too. It probes
  reachability rather than trusting the inode: Debian/Ubuntu and Fedora omit
  `RemoveOnStop=`, whose systemd default is off, so a stopped unit can leave a
  socket node that `[[ -S ]]` accepts and `connect()` refuses. Arch sets
  `RemoveOnStop=yes`. The probe accepts only `ssh-add -l`'s documented live
  statuses (0/1) and has a half-second timeout, so a listening but nonresponsive
  endpoint cannot hang shell startup.

  It never starts an agent — an `ssh-agent` per shell orphans one agent per
  login that no later shell can reach.

  **Where no socket unit is shipped** — Ubuntu 24.04 and older, Debian 12 and
  older, the RHEL 9 family — there is nothing to adopt and the block is a
  no-op. `systemctl --user enable --now ssh-agent.socket` fails there, and
  the `ssh-agent.service` those releases do ship is not a substitute: it has
  no `[Install]` section, and on Debian/Ubuntu it is gated behind
  `ConditionPathExists=/etc/X11/Xsession.options`, so it never starts on a
  headless host. Install the unit this repo ships instead:

  ```bash
  mkdir -p ~/.config/systemd/user
  cp setup/term-public-ssh-agent.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now term-public-ssh-agent.service
  loginctl enable-linger "$USER"   # optional: keep the agent across logins
  ```

  The repo-specific unit and socket names cannot override or unlink the
  distribution's `ssh-agent.service` / `.socket`. `bash/bashrc` checks both
  vendor paths first, then this fallback's
  `$XDG_RUNTIME_DIR/term-public-ssh-agent/agent.sock`.

  After upgrading to a release that ships the socket unit, enable that unit,
  open a new shell, and load the identities into the now-preferred distribution
  agent before disabling and removing `term-public-ssh-agent.service`.

  Either way the agent starts empty: `ssh-add` once per agent lifetime, or
  set `AddKeysToAgent yes` in `~/.ssh/config`.

CI runs the bootstrap for real on every push: natively on `ubuntu-latest`
and inside an `almalinux:9` container as root, each followed by `setup.sh`
and the smoke test, so both package-manager paths and the upstream fallbacks
are exercised.

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

Some installers interpolate your home directory **literally** at install time
(shown as `/Users/<user>` below) and some write `$HOME`. That difference decides
whether the hardcoded-home detector fires, so it is part of each fixture's
contract rather than a formatting detail.

| Installer | Emitted shape | Detected as |
|---|---|---|
| `docker desktop` | marker comment + `export PATH="$PATH:/Users/<user>/.docker/bin"` | installer marker, hardcoded home |
| `conda` | `# >>> conda initialize >>>` block naming `/Users/<user>/miniconda3` | installer marker, third-party init, hardcoded home |
| `rustup` | `. "$HOME/.cargo/env"` | third-party init |
| `nvm` | `export NVM_DIR="$HOME/.nvm"` + `nvm.sh` source | third-party init |
| `pyenv` | `export PYENV_ROOT="$HOME/.pyenv"` + `pyenv init` | third-party init |
| `rbenv` | `export RBENV_ROOT="$HOME/.rbenv"` + `rbenv init` | third-party init |
| `sdkman` | `export SDKMAN_DIR="$HOME/.sdkman"` + `sdkman-init.sh` | third-party init |
| `google cloud sdk` | marker comment + `/Users/<user>/google-cloud-sdk/path.bash.inc` source | installer marker, third-party init, hardcoded home |
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
hive tmux role implementer
hive tmux role reviewer
hive tmux role clear
hive tmux pairs
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

Agent windows on the same non-default `(remote, branch)` also get the
ADR-0003 turn indicator: `▶` marks the next prompt, `✓` marks the implementer
window where an approved PR is merged, `?` is reserved for a future
question-aware enricher, and `↩` means the PR is terminal and the checkout can
be put back. Eligibility comes from the pane's process tree, so an ordinary
Node process is never mistaken for Codex. Roles normally come from branch
reflog provenance; use `hive tmux role implementer|reviewer` in a pane when
that provenance is absent, and `hive tmux role clear` to remove the override.
Backtick+`p` opens the pair detail popup. Backtick+`R` clears mutable PR
observations and refreshes immediately while preserving immutable merged
receipts. Turn state is evaluated when a session starts or reloads, when a role
is changed, when a window is selected, and by those two bindings. Selection
refreshes are user-paced and single-flight; turn evaluation is deliberately not
launched from `status-right`, because tmux may restart a completed `#(…)`
command far more often than `status-interval`. Press backtick+`R` after PR
activity when the popup is not already being opened.

Window selection keeps the single-key path for the first ten workspaces:
backtick+`1` through `9` select those window numbers, and backtick+`0` selects
window 10 in a hive session. For any window number, press backtick+`'`, enter
the number, and press Enter. Numbered workspaces are assigned to windows in
numeric suffix order (`wg-2` before `wg-10`).

## Tests

Run:

```bash
pytest
```

GitHub Actions runs the test suite on push and pull request, plus the two
Linux bootstrap jobs described under [Linux hosts](#linux-hosts-rhel-ubuntu).
