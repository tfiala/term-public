"""Guard: no machine-specific content in tracked files.

`setup.sh` links `~/.bash_profile`, `~/.bashrc` and friends into this
repo, so a package installer that "adds itself to your PATH" follows the
symlink and appends to the *tracked repo file* rather than a private
dotfile.  Docker Desktop did exactly that on 2026-08-18.  This repo is
public and excludes personal paths.

Four detectors, because no one of them is sufficient: a literal
`/Users/<name>` anywhere; a self-announcing installer marker comment; the
`$HOME`-parameterized init blocks version managers write (recognized by
the variables and files their documented setup must use); and a header
landmark that catches a prepended block of any wording because it pushes
the file's own first line down.

Four properties this file has to keep, each learned from a real miss:

1. **Fixtures are the installer's emitted contract.**  Every entry in
   `INSTALLER_FIXTURES` reproduces what the tool actually writes --
   including whether the home path appears literally or as `$HOME`, since
   that decides whether the hardcoded-home detector fires -- and carries
   a resolvable provenance reference.  An invented rustup marker once made
   a named installer look covered while its real line was undetected.
2. **The inventory is single and parseable, and both parsers fail
   closed.**  The README table is the one list of covered installers;
   fixtures must match it exactly, in both directions, including the
   claimed reasons.  Each parser is scoped to its own table/array and
   rejects any data line it cannot parse, because a parser that silently
   skips unrecognized-but-valid syntax reopens the gap it was written to
   close.
3. **Nothing fails open.**  Unreadable or undecodable tracked content is
   rejected, not skipped, and the sweep asserts it inspected files.
4. **No assertion republishes rejected content.**  `scan_text` returning
   bounded reasons is not sufficient on its own: pytest's assertion
   rewriting renders the *operands* of a failing assert, so
   `assert not RE.search(text)` or `assert read(p) is None` prints the
   whole file into a public CI log.  Every observation is therefore
   reduced to a bool or a reason keyword *before* the assert boundary,
   and messages are static.  `TestNoLeakEndToEnd` runs this file in a
   subprocess with a sentinel injected and asserts real pytest output
   never contains it.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = REPO_ROOT / "setup.sh"
README = REPO_ROOT / "README.md"

# When set, every file read returns sentinel-bearing content so the
# assertion surface is forced to fail.  Used only by TestNoLeakEndToEnd's
# subprocess run; unset in normal runs.
LEAK_CANARY_ENV = "TERM_PUBLIC_GUARD_LEAK_CANARY"
_LEAK_CANARY = os.environ.get(LEAK_CANARY_ENV)

HARDCODED_HOME_RE = re.compile(r"/Users/(?!\$)[A-Za-z0-9._-]+")

INSTALLER_MARKER_RE = re.compile(
    r"(added by .{0,40}(installer|desktop|toolbox|sdk)"
    r"|the following lines? (were|was) added"
    r"|the next lines? updates? path for"
    r"|>>> .{0,30} initiali[sz]e >>>"
    r"|!! contents within this block are managed by)",
    re.IGNORECASE,
)

THIRD_PARTY_INIT_RE = re.compile(
    r"(NVM_DIR|nvm\.sh"
    r"|PYENV_ROOT|pyenv init"
    # `conda shell.bash hook` is not adjacent in the emitted line -- it
    # reads `conda' shell.bash` -- so match what conda actually writes.
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

# How the README's "Detected as" column spells each reason.
README_REASON_NAMES = {
    "hardcoded home": REASON_HOME,
    "installer marker": REASON_MARKER,
    "third-party init": REASON_THIRD_PARTY,
}

CONFIG_PREFIXES = ("bash/", "starship/", "tmux/", "ghostty/", "setup/")
CONFIG_FILES = ("setup.sh",)

LINKED_FILE_HEADERS = {
    "bash/bash_profile": "# term-public bash_profile.",
    "bash/bashrc": "# term-public bash baseline.",
    "bash/inputrc": "# term-public readline defaults.",
    "starship/starship.toml": "# term-public Starship prompt.",
    "tmux/tmux.conf": "# term-public base tmux config.",
}

# Tracked paths that are legitimately not utf-8 text.  Empty today; an
# entry is a reviewable exemption, never a silent skip.
BINARY_ALLOWLIST = frozenset()

# A stand-in account name, assembled so this file never contains a
# literal home path of its own.
LITERAL_HOME = "/Users/" + "someaccount"

# Installers whose emitted block interpolates the home directory
# literally at install time.  `{home}` is substituted with LITERAL_HOME
# for these and with the string "$HOME" for the rest -- the difference
# decides whether REASON_HOME fires, so it is part of the contract, not
# a formatting detail.
LITERAL_HOME_INSTALLERS = {"docker desktop", "conda", "google cloud sdk"}

# Provenance must be a resolvable URL, or a durable observed-event
# reference in the form `observed:YYYY-MM-DD: <where it is recorded>`.
PROVENANCE_RE = re.compile(
    r"^(https?://\S+|observed:\d{4}-\d{2}-\d{2}: .+)")

# name -> (block template using {home}, expected reasons, provenance)
INSTALLER_FIXTURES = {
    "docker desktop": (
        "# The following lines were added by Docker Desktop to add commands to your PATH.\n"
        'export PATH="$PATH:{home}/.docker/bin"\n'
        "# End of Docker Desktop section.\n",
        {REASON_MARKER, REASON_HOME},
        "observed:2026-08-18: block recorded in README.md "
        "'Installers that edit your shell profile' and ADR-0001",
    ),
    "conda": (
        "# >>> conda initialize >>>\n"
        "__conda_setup=\"$('{home}/miniconda3/bin/conda' shell.bash hook 2> /dev/null)\"\n"
        "# <<< conda initialize <<<\n",
        {REASON_MARKER, REASON_THIRD_PARTY, REASON_HOME},
        "https://github.com/conda/conda — `conda init` managed block",
    ),
    "rustup": (
        '. "{home}/.cargo/env"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/rust-lang/rustup/blob/main/src/cli/self_update/shell.rs",
    ),
    "nvm": (
        'export NVM_DIR="{home}/.nvm"\n'
        '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm\n',
        {REASON_THIRD_PARTY},
        "https://github.com/nvm-sh/nvm/blob/master/install.sh",
    ),
    "pyenv": (
        'export PYENV_ROOT="{home}/.pyenv"\n'
        'eval "$(pyenv init - bash)"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/pyenv/pyenv/blob/master/README.md",
    ),
    "rbenv": (
        'export RBENV_ROOT="{home}/.rbenv"\n'
        'eval "$(rbenv init - bash)"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/rbenv/rbenv/blob/master/README.md",
    ),
    "sdkman": (
        'export SDKMAN_DIR="{home}/.sdkman"\n'
        '[[ -s "$SDKMAN_DIR/bin/sdkman-init.sh" ]] && source "$SDKMAN_DIR/bin/sdkman-init.sh"\n',
        {REASON_THIRD_PARTY},
        "https://github.com/sdkman/sdkman-cli",
    ),
    "google cloud sdk": (
        "# The next line updates PATH for the Google Cloud SDK.\n"
        "if [ -f '{home}/google-cloud-sdk/path.bash.inc' ]; then . '{home}/google-cloud-sdk/path.bash.inc'; fi\n",
        {REASON_MARKER, REASON_THIRD_PARTY, REASON_HOME},
        "https://cloud.google.com/sdk/docs/install",
    ),
    "jetbrains toolbox": (
        "# added by JetBrains Toolbox\n"
        'export PATH="$PATH:{home}/Library/Application Support/JetBrains/Toolbox/scripts"\n',
        {REASON_MARKER},
        "https://www.jetbrains.com/help/idea/toolbox-app.html",
    ),
}


def fixture_block(name):
    """The emitted bytes for one installer, with the home directory in
    the form that installer actually writes."""
    template = INSTALLER_FIXTURES[name][0]
    home = LITERAL_HOME if name in LITERAL_HOME_INSTALLERS else "$HOME"
    return template.replace("{home}", home)


def file_text(path):
    """Single read path for every file this module inspects.

    Routing all reads through here is what lets the leak canary poison
    them, so the end-to-end sentinel test exercises the real assertion
    surface rather than a copy of it.
    """
    if _LEAK_CANARY:
        return (f"# {_LEAK_CANARY}\n"
                f'export NVM_DIR="$HOME/{_LEAK_CANARY}"\n')
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan_text(rel_path, text):
    """Bounded reason strings for one file's content.  Never returns the
    offending text -- only `path:line: reason`."""
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


def scan_repo():
    """Return (offenders, inspected_count).  An uninspectable tracked
    path becomes an offender, so the sweep cannot pass by reading
    nothing."""
    offenders, inspected = [], 0
    for path in tracked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = file_text(path)
        if text is None:
            if rel not in BINARY_ALLOWLIST:
                offenders.append(f"{rel}:0: {REASON_UNREADABLE}")
            continue
        inspected += 1
        offenders += scan_text(rel, text)
    return offenders, inspected


def setup_linked_relatives():
    """Return (values, unparsed_count) for setup.sh's link table.

    Scoped to `_REPO_LINK_RELATIVES` and fails closed: a member line the
    parser cannot read is counted, never skipped, because an unquoted but
    perfectly valid Bash entry would otherwise escape the contract.
    """
    text = file_text(SETUP_SH) or ""
    m = re.search(r"^_REPO_LINK_RELATIVES=\(\n(.*?)^\)", text, re.S | re.M)
    if not m:
        return set(), 1
    values, unparsed = set(), 0
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        member = re.fullmatch(r'"([^"]+)"', stripped)
        if member:
            values.add(member.group(1))
        else:
            unparsed += 1
    return values, unparsed


def readme_installer_inventory():
    """Return (name -> reasons, unparsed_count) for the README table.

    Scoped to the coverage table and fails closed on any body row the
    strict pattern cannot read, so a validly-formatted but unrecognized
    row cannot be silently omitted.
    """
    text = file_text(README) or ""
    m = re.search(r"^\| Installer \| Emitted shape \| Detected as \|\n"
                  r"\|[-| ]+\|\n(.*?)(?:\n\n|\Z)", text, re.S | re.M)
    if not m:
        return {}, 1
    inventory, unparsed = {}, 0
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = re.fullmatch(r"\| *`([^`]+)` *\| *(.+?) *\| *(.+?) *\|", stripped)
        if not row:
            unparsed += 1
            continue
        names = [README_REASON_NAMES.get(p.strip())
                 for p in row.group(3).split(",")]
        if any(n is None for n in names):
            unparsed += 1
            continue
        inventory[row.group(1).strip().lower()] = set(names)
    return inventory, unparsed


class TestRepoIsClean:
    """The tracked tree carries no machine-specific content."""

    def test_repo_has_tracked_files(self):
        assert len(tracked_files()) > 10

    def test_this_file_is_tracked(self):
        """An *unadded* copy of this guard exempts itself and passes on
        an empty scan -- how its own example paths slipped through once.
        (A staged file is already in `git ls-files`.)"""
        assert Path(__file__).resolve() in {p.resolve() for p in tracked_files()}

    def test_no_machine_specific_content(self):
        offenders, inspected = scan_repo()
        assert inspected > 10, "too few tracked files inspected; sweep is not covering the tree"
        assert not offenders, (
            "machine-specific content in tracked files (PATH/env exports "
            "belong in local/env.local, shell-init blocks in "
            "local/bashrc.local):\n  " + "\n  ".join(offenders))

    def test_third_party_tokens_absent_from_repo_config(self):
        """Reduced to a bool before the assert: `assert not RE.search(text)`
        renders the whole file as a pytest operand."""
        hits = sorted(
            rel for rel in list(LINKED_FILE_HEADERS) + ["setup.sh"]
            if THIRD_PARTY_INIT_RE.search(file_text(REPO_ROOT / rel) or ""))
        assert not hits, (
            f"third-party init token present in tracked config: {hits} "
            "(paths only; narrow the detector)")

    def test_binary_allowlist_is_accurate(self):
        """A stale entry would exempt a readable file from scanning.
        Same reduction as above: `assert read(p) is None` would render
        the whole file as a pytest operand."""
        stale = sorted(
            rel for rel in BINARY_ALLOWLIST
            if not (REPO_ROOT / rel).is_file() or _decodes_as_text(REPO_ROOT / rel))
        assert not stale, (
            f"allowlisted paths are missing or decode as text: {stale}")


def _decodes_as_text(path):
    try:
        path.read_text(encoding="utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


class TestExternalContracts:
    """Bindings this guard must resolve rather than restate."""

    def test_linked_file_set_matches_setup_sh(self):
        linked, unparsed = setup_linked_relatives()
        assert unparsed == 0, (
            f"{unparsed} member(s) of setup.sh's _REPO_LINK_RELATIVES could "
            "not be parsed; the contract must fail closed, not skip them")
        files = {r for r in linked if (REPO_ROOT / r).is_file()}
        dirs = {r for r in linked if (REPO_ROOT / r).is_dir()}
        assert files | dirs == linked, (
            f"setup.sh links paths that do not exist: {sorted(linked - files - dirs)}")
        assert files == set(LINKED_FILE_HEADERS), (
            "LINKED_FILE_HEADERS out of sync with setup.sh: missing "
            f"{sorted(files - set(LINKED_FILE_HEADERS))}, stale "
            f"{sorted(set(LINKED_FILE_HEADERS) - files)}")

    def test_declared_headers_are_the_real_first_lines(self):
        """Value-free: names the files that disagree, never their content
        -- a prepended block's first line is exactly what must not be
        echoed into a public CI log."""
        wrong = sorted(
            rel for rel, header in LINKED_FILE_HEADERS.items()
            if ((file_text(REPO_ROOT / rel) or "").splitlines() or [None])[0] != header)
        assert not wrong, (
            f"declared header does not match the real first line in: {wrong} "
            "(content withheld; run `head -1` locally)")

    def test_readme_inventory_matches_fixtures_exactly(self):
        documented, unparsed = readme_installer_inventory()
        assert unparsed == 0, (
            f"{unparsed} README coverage-table row(s) could not be parsed; "
            "the inventory must fail closed, not skip them")
        assert documented, "README installer table is missing or empty"
        assert set(documented) == set(INSTALLER_FIXTURES), (
            "README table and INSTALLER_FIXTURES disagree: documented-but-"
            f"unproven {sorted(set(documented) - set(INSTALLER_FIXTURES))}, "
            f"undocumented {sorted(set(INSTALLER_FIXTURES) - set(documented))}")

    def test_readme_claimed_reasons_match_fixtures(self):
        """The table's 'Detected as' column is a claim too."""
        documented, _ = readme_installer_inventory()
        mismatched = sorted(
            name for name, reasons in documented.items()
            if reasons != INSTALLER_FIXTURES[name][1] - {REASON_HOME}
            and reasons != INSTALLER_FIXTURES[name][1])
        assert not mismatched, (
            f"README 'Detected as' disagrees with fixture reasons: {mismatched}")

    def test_every_fixture_declares_resolvable_provenance(self):
        """A length check accepted junk; require a URL or a dated
        observed-event reference."""
        bad = sorted(name for name, (_, _, prov) in INSTALLER_FIXTURES.items()
                     if not PROVENANCE_RE.match(prov or ""))
        assert not bad, (
            f"fixtures without resolvable provenance: {bad} "
            "(need an http(s) URL or 'observed:YYYY-MM-DD: <record>')")

    def test_literal_home_installers_are_declared_consistently(self):
        """A fixture that interpolates a literal home must expect
        REASON_HOME, and one that uses $HOME must not."""
        for name, (_, expected, _) in INSTALLER_FIXTURES.items():
            literal = name in LITERAL_HOME_INSTALLERS
            assert (REASON_HOME in expected) == literal, (
                f"{name}: literal-home={literal} but expected reasons "
                f"{'include' if REASON_HOME in expected else 'omit'} "
                "hardcoded-home-directory")
            assert ("$HOME" in fixture_block(name)) != literal or not literal, (
                f"{name}: literal fixture must not also use $HOME for the home path")


class TestDetectorsAreLive:
    """Positive cases.  Without these the detectors could never match and
    the suite would stay green -- the state this file once shipped in."""

    def test_fixture_tables_are_populated(self):
        assert INSTALLER_FIXTURES, "no installer fixtures; positives are vacuous"
        assert len(LINKED_FILE_HEADERS) >= 2, "too few linked files exercised"

    @pytest.mark.parametrize("installer", sorted(INSTALLER_FIXTURES))
    @pytest.mark.parametrize("target", sorted(LINKED_FILE_HEADERS))
    def test_appended_block_detected_with_expected_reasons(self, installer, target):
        expected = INSTALLER_FIXTURES[installer][1]
        text = (file_text(REPO_ROOT / target) or "") + fixture_block(installer)
        actual = reasons_of(scan_text(target, text))
        assert actual == expected, (
            f"{installer} appended to {target}: reasons {sorted(actual)}, "
            f"expected {sorted(expected)}")

    @pytest.mark.parametrize("installer", sorted(INSTALLER_FIXTURES))
    @pytest.mark.parametrize("target", sorted(LINKED_FILE_HEADERS))
    def test_prepended_block_detected(self, installer, target):
        expected = INSTALLER_FIXTURES[installer][1]
        text = fixture_block(installer) + (file_text(REPO_ROOT / target) or "")
        actual = reasons_of(scan_text(target, text))
        assert expected <= actual, f"{installer} prepended to {target}"
        assert REASON_HEADER in actual, "prepend must displace the header"

    def test_hardcoded_home_rejected(self):
        block = "export PATH=" + '"$PATH:' + LITERAL_HOME + '/tools/bin"\n'
        missed = []
        for target in LINKED_FILE_HEADERS:
            reasons = reasons_of(scan_text(
                target, (file_text(REPO_ROOT / target) or "") + block))
            if REASON_HOME not in reasons:
                missed.append(target)
        assert not missed, f"literal home not detected in: {missed}"

    def test_unknown_prepended_block_detected(self):
        missed = []
        for target in LINKED_FILE_HEADERS:
            reasons = reasons_of(scan_text(
                target, "# some future installer nobody has seen\n"
                        + (file_text(REPO_ROOT / target) or "")))
            if REASON_HEADER not in reasons:
                missed.append(target)
        assert not missed, f"prepended unknown block not detected in: {missed}"

    def test_clean_files_are_not_flagged(self):
        flagged = sorted(
            target for target in LINKED_FILE_HEADERS
            if scan_text(target, file_text(REPO_ROOT / target) or ""))
        assert not flagged, f"clean files unexpectedly flagged: {flagged}"


class TestParsersFailClosed:
    """A parser that skips syntax it does not recognize reopens the gap
    the contract was written to close."""

    def test_unparsable_setup_member_is_counted(self):
        text = 'x\n_REPO_LINK_RELATIVES=(\n  "bash/bashrc"\n  bash/unquoted\n)\n'
        m = re.search(r"^_REPO_LINK_RELATIVES=\(\n(.*?)^\)", text, re.S | re.M)
        unparsed = sum(
            1 for line in m.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")
            and not re.fullmatch(r'"([^"]+)"', line.strip()))
        assert unparsed == 1, "an unquoted-but-valid member must be counted"

    def test_unparsable_readme_row_is_counted(self):
        row = "| asdf | eval \"$(asdf ...)\" | third-party init |"
        assert not re.fullmatch(
            r"\| *`([^`]+)` *\| *(.+?) *\| *(.+?) *\|", row.strip()), (
            "a backtick-free row must not parse as a recognized row")

    def test_live_parsers_report_zero_unparsed(self):
        _, setup_unparsed = setup_linked_relatives()
        _, readme_unparsed = readme_installer_inventory()
        assert setup_unparsed == 0 and readme_unparsed == 0


class TestFailsClosed:
    """Unreadable content is never treated as clean."""

    def test_unreadable_tracked_file_is_rejected(self, monkeypatch):
        import test_no_machine_specific_config as mod
        monkeypatch.setattr(mod, "file_text", lambda p: None)
        offenders, inspected = mod.scan_repo()
        assert inspected == 0
        assert offenders, "an unreadable tree must fail, not pass vacuously"
        assert all(REASON_UNREADABLE in o for o in offenders)

    def test_zero_inspected_files_fails_the_production_assertion(self, monkeypatch):
        import test_no_machine_specific_config as mod
        monkeypatch.setattr(mod, "file_text", lambda p: None)
        with pytest.raises(AssertionError):
            TestRepoIsClean().test_no_machine_specific_content()

    def test_single_unreadable_candidate_fails(self, monkeypatch):
        import test_no_machine_specific_config as mod
        real, target = mod.file_text, REPO_ROOT / "bash" / "bashrc"
        monkeypatch.setattr(
            mod, "file_text", lambda p: None if p == target else real(p))
        offenders, inspected = mod.scan_repo()
        assert inspected > 10
        assert [o for o in offenders if o.startswith("bash/bashrc:0:")]


class TestDiagnosticsAreValueFree:
    """A failure must not republish the content it rejected."""

    SENTINEL = "s3cr3t" + "-machine-identifier"

    def _texts(self):
        base = file_text(REPO_ROOT / "bash" / "bash_profile") or ""
        return {
            "home": base + "export PATH=" + '"/Users/' + self.SENTINEL + '/bin"\n',
            "marker": base + f"# added by {self.SENTINEL} installer\n",
            "third_party": base + f'export NVM_DIR="$HOME/{self.SENTINEL}"\n',
            "header": f"# {self.SENTINEL}\n" + base,
        }

    def test_each_path_detects(self):
        undetected = sorted(name for name, text in self._texts().items()
                            if not scan_text("bash/bash_profile", text))
        assert not undetected, f"cases not detected: {undetected}"

    def test_no_rejected_content_in_diagnostics(self):
        leaked = sorted(
            name for name, text in self._texts().items()
            if any(self.SENTINEL in r
                   for r in scan_text("bash/bash_profile", text)))
        assert not leaked, f"rejected content leaked into diagnostics: {leaked}"

    def test_reasons_are_bounded_vocabulary(self):
        seen = set()
        for text in self._texts().values():
            seen |= reasons_of(scan_text("bash/bash_profile", text))
        assert seen <= ALL_REASONS, f"unbounded reasons: {sorted(seen - ALL_REASONS)}"


class TestNoLeakEndToEnd:
    """The scanner's own message being clean is not enough -- any sibling
    assertion can reintroduce the leak through pytest's operand
    rendering.  Run this file for real and inspect the actual output."""

    SENTINEL = "canary" + "-private-value-9137"

    @pytest.mark.skipif(
        _LEAK_CANARY is not None,
        reason="inner canary run: skipped so the subprocess cannot respawn "
               "itself (an env guard, not a --deselect nodeid, because a "
               "mismatched nodeid recurses forever)")
    def test_real_pytest_output_never_contains_rejected_content(self):
        env = {**os.environ, LEAK_CANARY_ENV: self.SENTINEL}
        r = subprocess.run(
            [sys.executable, "-m", "pytest", __file__, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=180)
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            "the canary must force failures, or this proves nothing")
        assert self.SENTINEL not in combined, (
            "rejected content reached real pytest output")
