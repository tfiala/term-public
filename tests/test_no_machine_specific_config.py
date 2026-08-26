"""Guard: no machine-specific content in tracked files.

`setup.sh` links `~/.bash_profile`, `~/.bashrc` and friends into this
repo, so a package installer that "adds itself to your PATH" follows the
symlink and appends to the *tracked repo file* rather than a private
dotfile.  Docker Desktop did exactly that on 2026-08-18, writing an
`export PATH="$PATH:/Users/<user>/.docker/bin"` block to the top of
`bash/bash_profile`.  This repo is public and excludes personal paths.

Four detectors, because no one of them is sufficient:

- **hardcoded home** — a literal `/Users/<name>` anywhere in a tracked
  file, whatever the surrounding wording;
- **installer marker** — a block whose paths are parameterized but whose
  comment announces itself (Docker Desktop, conda, the Google Cloud SDK,
  JetBrains Toolbox);
- **third-party init** — the version managers that leave neither: nvm,
  pyenv, rustup, rbenv and SDKMAN write `$HOME`-parameterized lines with
  no marker comment, so they are recognized by the variables, files and
  init calls their documented setup must use;
- **header displaced** — a prepended block of *any* wording, caught
  because it pushes the file's own first line down.

Three properties this file has to keep, each learned from a real miss:

1. **Fixtures are the installer's emitted contract, not a plausible
   paraphrase.**  Every entry in `INSTALLER_FIXTURES` carries the line
   the tool actually writes plus the provenance URL to check it against.
   An invented marker comment made rustup look covered while the real
   `. "$HOME/.cargo/env"` was undetected and all tests were green.
2. **The inventory is single and parseable.**  The README table is the
   one list of covered installers; `INSTALLER_FIXTURES` must match it as
   a set, in both directions, so neither a documented-but-unproven
   installer nor an undocumented fixture can exist.
3. **Nothing fails open.**  Unreadable or undecodable tracked content is
   rejected, not skipped, and the sweep asserts it actually inspected
   files.  Equating "could not read" with "clean" once let an
   always-`None` reader skip all 38 tracked files and still pass.

Diagnostics are value-free: a failure names the file, the line number,
and a bounded reason keyword, never the matched text.  CI logs on a
public repo are public, so echoing the rejected line would republish the
machine identifier this guard exists to contain.

**Boundary.** This runs in CI, which is after a push.  It blocks
integration; it cannot stop a literal path from first appearing in a
commit on a public branch.  That would need a local pre-push hook, which
this repo does not install.
"""

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = REPO_ROOT / "setup.sh"
README = REPO_ROOT / "README.md"

# A literal home directory -- /Users/ followed by a real account name --
# but not the parameterized /Users/$USER or /Users/${USER} that macOS
# `dscl` lookups legitimately use.
#
# This file is tracked and therefore scanned, so example paths use a
# bracketed placeholder (/Users/<name>): `<` is outside the name
# character class.  Positive cases assemble a literal at runtime instead.
HARDCODED_HOME_RE = re.compile(r"/Users/(?!\$)[A-Za-z0-9._-]+")

INSTALLER_MARKER_RE = re.compile(
    r"(added by .{0,40}(installer|desktop|toolbox|sdk)"
    r"|the following lines? (were|was) added"
    r"|the next lines? updates? path for"
    r"|>>> .{0,30} initiali[sz]e >>>"
    r"|!! contents within this block are managed by)",
    re.IGNORECASE,
)

# Version managers whose profile blocks are $HOME-parameterized and carry
# no marker comment.  Keyed on what their documented setup must emit.
# test_third_party_tokens_absent_from_repo_config proves none of these
# collide with this repo's own tracked config.
THIRD_PARTY_INIT_RE = re.compile(
    r"(NVM_DIR|nvm\.sh"
    r"|PYENV_ROOT|pyenv init"
    # `conda shell.bash hook` is not adjacent in the emitted line -- it
    # reads `conda' shell.bash` -- so match the variable and install dir
    # conda actually writes instead of the prose-looking phrase.
    r"|CONDA_PREFIX|conda\.sh|__conda_setup|miniconda|anaconda"
    r"|\.cargo/env|rustup"
    r"|rbenv init|RBENV_ROOT"
    r"|SDKMAN_DIR|sdkman-init"
    r"|google-cloud-sdk/)",
    re.IGNORECASE,
)

REASON_HOME = "hardcoded-home-directory"
REASON_MARKER = "installer-marker-comment"
REASON_THIRD_PARTY = "third-party-init-block"
REASON_HEADER = "file-header-displaced"
REASON_UNREADABLE = "unreadable-tracked-file"
ALL_REASONS = {REASON_HOME, REASON_MARKER, REASON_THIRD_PARTY,
               REASON_HEADER, REASON_UNREADABLE}

CONFIG_PREFIXES = ("bash/", "starship/", "tmux/", "ghostty/", "setup/")
CONFIG_FILES = ("setup.sh",)

# First line each linked repo file must still start with.  The *set* of
# keys is checked against setup.sh's own link table, so adding a link
# there fails this guard until a header contract is declared for it.
LINKED_FILE_HEADERS = {
    "bash/bash_profile": "# term-public bash_profile.",
    "bash/bashrc": "# term-public bash baseline.",
    "bash/inputrc": "# term-public readline defaults.",
    "starship/starship.toml": "# term-public Starship prompt.",
    "tmux/tmux.conf": "# term-public base tmux config.",
}

# Tracked paths that are legitimately not utf-8 text.  Empty today; an
# entry here is a deliberate, reviewable exemption rather than a silent
# skip.  test_binary_allowlist_is_accurate keeps it honest.
BINARY_ALLOWLIST = frozenset()

# name -> (emitted block, expected reasons, provenance)
#
# The block is what the tool actually writes to a shell profile.  Check
# any change against the provenance URL; a paraphrase that happens to
# trip a detector proves nothing about the real installer.
INSTALLER_FIXTURES = {
    "docker desktop": (
        "# The following lines were added by Docker Desktop to add commands to your PATH.\n"
        'export PATH="$PATH:$HOME/.docker/bin"\n'
        "# End of Docker Desktop section.\n",
        {REASON_MARKER},
        "Docker Desktop macOS installer shell-completion/PATH block",
    ),
    "conda": (
        "# >>> conda initialize >>>\n"
        '__conda_setup="$(\'$HOME/miniconda3/bin/conda\' shell.bash hook 2> /dev/null)"\n'
        "# <<< conda initialize <<<\n",
        {REASON_MARKER, REASON_THIRD_PARTY},
        "https://github.com/conda/conda — `conda init` managed block",
    ),
    "rustup": (
        '. "$HOME/.cargo/env"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/rust-lang/rustup/blob/main/src/cli/self_update/shell.rs"
        " — Posix::source_string / update_rcs",
    ),
    "nvm": (
        'export NVM_DIR="$HOME/.nvm"\n'
        '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm\n'
        '[ -s "$NVM_DIR/bash_completion" ] && \\. "$NVM_DIR/bash_completion"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/nvm-sh/nvm/blob/master/install.sh",
    ),
    "pyenv": (
        'export PYENV_ROOT="$HOME/.pyenv"\n'
        '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"\n'
        'eval "$(pyenv init - bash)"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/pyenv/pyenv/blob/master/README.md — Set up your shell",
    ),
    "rbenv": (
        'export RBENV_ROOT="$HOME/.rbenv"\n'
        'eval "$(rbenv init - bash)"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/rbenv/rbenv/blob/master/README.md",
    ),
    "sdkman": (
        'export SDKMAN_DIR="$HOME/.sdkman"\n'
        '[[ -s "$HOME/.sdkman/bin/sdkman-init.sh" ]] && source "$HOME/.sdkman/bin/sdkman-init.sh"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/sdkman/sdkman-cli — install.sh rc append",
    ),
    "google cloud sdk": (
        "# The next line updates PATH for the Google Cloud SDK.\n"
        "if [ -f '$HOME/google-cloud-sdk/path.bash.inc' ]; then . '$HOME/google-cloud-sdk/path.bash.inc'; fi\n",
        {REASON_MARKER, REASON_THIRD_PARTY},
        "google-cloud-sdk install.sh — path.bash.inc rc append",
    ),
    "jetbrains toolbox": (
        "# added by JetBrains Toolbox\n"
        'export PATH="$PATH:$HOME/Library/Application Support/JetBrains/Toolbox/scripts"\n',
        {REASON_MARKER},
        "JetBrains Toolbox shell-scripts PATH block",
    ),
}


def _literal_home_block():
    """A hardcoded-home line, assembled so this file never contains one."""
    return "export PATH=" + '"$PATH:' + "/Users/" + "someaccount" + '/tools/bin"\n'


def setup_linked_relatives():
    """Repo-relative paths setup.sh links, parsed from its own table.

    The binding lives in `_REPO_LINK_RELATIVES` in setup.sh; restating it
    here would let a new linked file bypass this guard entirely.
    """
    text = SETUP_SH.read_text()
    m = re.search(r"^_REPO_LINK_RELATIVES=\((.*?)^\)", text, re.S | re.M)
    assert m, "setup.sh no longer defines _REPO_LINK_RELATIVES"
    return {v for v in re.findall(r'"([^"]+)"', m.group(1))}


def scan_text(rel_path, text):
    """Bounded reason strings for one file's content.

    Never returns the offending text -- only `path:line: reason`, so a
    failure is safe to print in a public CI log.
    """
    offenders = []
    is_config = rel_path.startswith(CONFIG_PREFIXES) or rel_path in CONFIG_FILES
    lines = text.splitlines()

    for num, line in enumerate(lines, 1):
        if HARDCODED_HOME_RE.search(line):
            offenders.append(f"{rel_path}:{num}: {REASON_HOME}")
        if is_config:
            if INSTALLER_MARKER_RE.search(line):
                offenders.append(f"{rel_path}:{num}: {REASON_MARKER}")
            if THIRD_PARTY_INIT_RE.search(line):
                offenders.append(f"{rel_path}:{num}: {REASON_THIRD_PARTY}")

    expected_header = LINKED_FILE_HEADERS.get(rel_path)
    if expected_header is not None and (not lines or lines[0] != expected_header):
        offenders.append(f"{rel_path}:1: {REASON_HEADER}")

    return offenders


def reasons_of(offenders):
    return {o.rsplit(": ", 1)[-1] for o in offenders}


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def readable_text(path):
    """File text, or None if it cannot be read or decoded.

    None means "could not inspect", which `scan_repo` treats as a
    failure -- never as clean.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan_repo():
    """Return (offenders, inspected_count).

    A tracked path that cannot be inspected becomes an offender unless it
    is an explicit BINARY_ALLOWLIST entry, so the sweep cannot pass by
    reading nothing.
    """
    offenders, inspected = [], 0
    for path in tracked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = readable_text(path)
        if text is None:
            if rel not in BINARY_ALLOWLIST:
                offenders.append(f"{rel}:0: {REASON_UNREADABLE}")
            continue
        inspected += 1
        offenders += scan_text(rel, text)
    return offenders, inspected


class TestRepoIsClean:
    """The tracked tree carries no machine-specific content."""

    def test_repo_has_tracked_files(self):
        assert len(tracked_files()) > 10

    def test_this_file_is_tracked(self):
        """The sweep only sees tracked files, so an *unadded* copy of
        this guard exempts itself and passes on an empty scan -- how its
        own example paths slipped through locally once.  (A staged file
        is already in `git ls-files`; only a never-added one is
        invisible.)"""
        assert Path(__file__).resolve() in {p.resolve() for p in tracked_files()}, (
            "this test file is untracked; `git add` it before trusting a "
            "local pass")

    def test_no_machine_specific_content(self):
        offenders, inspected = scan_repo()
        assert inspected > 10, (
            f"only {inspected} tracked files were inspected; the sweep is "
            "not covering the tree")
        assert not offenders, (
            "machine-specific content in tracked files (PATH/env exports "
            "belong in local/env.local, shell-init blocks in "
            "local/bashrc.local):\n  " + "\n  ".join(offenders))

    def test_third_party_tokens_absent_from_repo_config(self):
        """The third-party detector must not collide with this repo's own
        config, or it would be unusable."""
        for rel in sorted(LINKED_FILE_HEADERS) + ["setup.sh"]:
            text = (REPO_ROOT / rel).read_text()
            assert not THIRD_PARTY_INIT_RE.search(text), (
                f"{rel} contains a third-party init token; narrow the detector")

    def test_binary_allowlist_is_accurate(self):
        """Every allowlisted path must exist and actually be undecodable;
        a stale entry would exempt a readable file from scanning."""
        for rel in BINARY_ALLOWLIST:
            path = REPO_ROOT / rel
            assert path.is_file(), f"allowlisted path {rel} does not exist"
            assert readable_text(path) is None, (
                f"{rel} is allowlisted as binary but decodes as text")


class TestExternalContracts:
    """Bindings this guard must not restate from memory."""

    def test_linked_file_set_matches_setup_sh(self):
        """setup.sh's link table is the definition.  A new linked file
        must gain a header contract here rather than silently escape the
        header check."""
        linked = setup_linked_relatives()
        files = {r for r in linked if (REPO_ROOT / r).is_file()}
        dirs = {r for r in linked if (REPO_ROOT / r).is_dir()}
        assert files | dirs == linked, (
            f"setup.sh links paths that do not exist: {linked - files - dirs}")
        assert files == set(LINKED_FILE_HEADERS), (
            "LINKED_FILE_HEADERS is out of sync with setup.sh's "
            f"_REPO_LINK_RELATIVES: missing {files - set(LINKED_FILE_HEADERS)}, "
            f"stale {set(LINKED_FILE_HEADERS) - files}")

    def test_declared_headers_are_the_real_first_lines(self):
        for rel, header in LINKED_FILE_HEADERS.items():
            first = (REPO_ROOT / rel).read_text().splitlines()[0]
            assert first == header, (
                f"{rel} first line is {first!r}, declared {header!r}")

    def test_readme_inventory_matches_fixtures_exactly(self):
        """One inventory, checked both ways: a documented installer with
        no fixture is an unproven claim, and a fixture missing from the
        table is undocumented coverage."""
        documented = readme_installer_inventory()
        assert documented, "README installer table is missing or unparseable"
        assert documented == set(INSTALLER_FIXTURES), (
            "README table and INSTALLER_FIXTURES disagree: "
            f"documented-but-unproven {documented - set(INSTALLER_FIXTURES)}, "
            f"undocumented {set(INSTALLER_FIXTURES) - documented}")

    def test_every_fixture_declares_provenance(self):
        for name, (_, _, provenance) in INSTALLER_FIXTURES.items():
            assert provenance and len(provenance) > 15, (
                f"{name} fixture has no usable provenance reference")


def readme_installer_inventory():
    """Installer names from the README's coverage table (column 1)."""
    rows = re.findall(r"^\| *`([^`]+)` *\|", README.read_text(), re.M)
    return {r.strip().lower() for r in rows}


class TestDetectorsAreLive:
    """Positive cases.  Without these the detectors could never match and
    the suite would stay green -- the state this file once shipped in."""

    def test_fixture_tables_are_populated(self):
        """Fail closed: emptying either table collapses the parametrized
        cases below to zero instances, which pytest reports as success."""
        assert INSTALLER_FIXTURES, "no installer fixtures; positives are vacuous"
        assert len(LINKED_FILE_HEADERS) >= 2, "too few linked files exercised"

    @pytest.mark.parametrize("installer", sorted(INSTALLER_FIXTURES))
    @pytest.mark.parametrize("target", sorted(LINKED_FILE_HEADERS))
    def test_appended_block_detected_with_expected_reasons(self, installer, target):
        """Append is the shape that preserves the header, so only the
        content detectors can catch it -- and the *specific* reasons are
        asserted, not merely that something fired."""
        block, expected, _ = INSTALLER_FIXTURES[installer]
        text = (REPO_ROOT / target).read_text() + block
        actual = reasons_of(scan_text(target, text))
        assert actual == expected, (
            f"{installer} appended to {target}: reasons {sorted(actual)}, "
            f"expected {sorted(expected)}")

    @pytest.mark.parametrize("installer", sorted(INSTALLER_FIXTURES))
    @pytest.mark.parametrize("target", sorted(LINKED_FILE_HEADERS))
    def test_prepended_block_detected(self, installer, target):
        block, expected, _ = INSTALLER_FIXTURES[installer]
        text = block + (REPO_ROOT / target).read_text()
        actual = reasons_of(scan_text(target, text))
        assert expected <= actual, f"{installer} prepended to {target}: {actual}"
        assert REASON_HEADER in actual, "prepend must displace the header"

    def test_hardcoded_home_rejected(self):
        for target in LINKED_FILE_HEADERS:
            text = (REPO_ROOT / target).read_text() + _literal_home_block()
            assert REASON_HOME in reasons_of(scan_text(target, text))

    def test_unknown_prepended_block_detected(self):
        """A future installer no regex knows is still caught by shape."""
        for target in LINKED_FILE_HEADERS:
            text = "# some future installer nobody has seen\n" \
                   + (REPO_ROOT / target).read_text()
            assert REASON_HEADER in reasons_of(scan_text(target, text))

    def test_clean_files_are_not_flagged(self):
        """Negative control: the real files pass, so the positives above
        detect the block rather than the file."""
        for target in LINKED_FILE_HEADERS:
            assert not scan_text(target, (REPO_ROOT / target).read_text())


class TestFailsClosed:
    """Unreadable content is never treated as clean."""

    def test_unreadable_tracked_file_is_rejected(self, monkeypatch):
        import test_no_machine_specific_config as mod
        monkeypatch.setattr(mod, "readable_text", lambda p: None)
        offenders, inspected = mod.scan_repo()
        assert inspected == 0
        assert offenders, "an unreadable tree must fail, not pass vacuously"
        assert all(REASON_UNREADABLE in o for o in offenders)

    def test_zero_inspected_files_fails_the_production_assertion(self, monkeypatch):
        """The end-to-end shape: a reader that can inspect nothing must
        fail `test_no_machine_specific_content`, not satisfy it."""
        import test_no_machine_specific_config as mod
        monkeypatch.setattr(mod, "readable_text", lambda p: None)
        with pytest.raises(AssertionError):
            TestRepoIsClean().test_no_machine_specific_content()

    def test_single_unreadable_candidate_fails(self, monkeypatch):
        import test_no_machine_specific_config as mod
        real = mod.readable_text
        target = REPO_ROOT / "bash" / "bashrc"
        monkeypatch.setattr(
            mod, "readable_text",
            lambda p: None if p == target else real(p))
        offenders, inspected = mod.scan_repo()
        assert inspected > 10, "only the one candidate should be unreadable"
        assert [o for o in offenders if o.startswith("bash/bashrc:0:")], (
            "a single unreadable tracked file must fail closed")


class TestDiagnosticsAreValueFree:
    """A failure must not republish the content it rejected.  CI logs on
    a public repo are public."""

    SENTINEL = "s3cr3t" + "-machine-identifier"

    def _texts(self):
        base = (REPO_ROOT / "bash" / "bash_profile").read_text()
        return {
            "home": base + "export PATH=" + '"/Users/' + self.SENTINEL + '/bin"\n',
            "marker": base + f"# added by {self.SENTINEL} installer\n",
            "third_party": base + f'export NVM_DIR="$HOME/{self.SENTINEL}"\n',
            "header": f"# {self.SENTINEL}\n" + base,
        }

    def test_each_path_detects(self):
        """Guard the guard: if a case stopped detecting, its leak test
        below would pass for the wrong reason."""
        for name, text in self._texts().items():
            assert scan_text("bash/bash_profile", text), f"{name} not detected"

    def test_no_rejected_content_in_diagnostics(self):
        for name, text in self._texts().items():
            for reason in scan_text("bash/bash_profile", text):
                assert self.SENTINEL not in reason, (
                    f"{name}: rejected content leaked into diagnostics")

    def test_reasons_are_bounded_vocabulary(self):
        for text in self._texts().values():
            for keyword in reasons_of(scan_text("bash/bash_profile", text)):
                assert keyword in ALL_REASONS, f"unbounded reason: {keyword!r}"

    def test_assertion_message_is_value_free(self):
        text = (REPO_ROOT / "bash" / "bash_profile").read_text() \
            + "export PATH=" + '"/Users/' + self.SENTINEL + '/bin"\n'
        offenders = scan_text("bash/bash_profile", text)
        message = ("machine-specific content in tracked files:\n  "
                   + "\n  ".join(offenders))
        assert self.SENTINEL not in message
