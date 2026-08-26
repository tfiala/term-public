"""Guard: no machine-specific content in tracked files.

`setup.sh` links `~/.bash_profile` and `~/.bashrc` to `bash/bash_profile`
and `bash/bashrc`.  Package installers that "add themselves to your
PATH" follow those symlinks and append to the *tracked repo file*, not
to a private dotfile.  Docker Desktop did exactly that on 2026-08-18,
writing an `export PATH="$PATH:/Users/<user>/.docker/bin"` block to the
top of `bash/bash_profile`.

That is machine-specific and belongs in the untracked `local/` overlay;
this repo is public and explicitly excludes personal paths.

Three independent detectors, because no one of them is sufficient:

- **hardcoded home** catches a literal `/Users/<name>` anywhere in a
  tracked file, whatever the surrounding wording;
- **installer marker** catches a block whose paths are parameterized but
  whose comment announces itself (Docker Desktop, conda, rustup, the
  Google Cloud SDK, JetBrains Toolbox);
- **third-party init** catches the version managers that leave neither —
  nvm and pyenv write `$HOME`-parameterized lines with no marker comment,
  so they are recognized by the variables and init calls they must use.

Scanning is factored into `scan_text` so the failing boundary is
directly testable: `INSTALLER_FIXTURES` holds a real block from every
installer the README names, and each is asserted rejected in both linked
profiles, prepended and appended.  Without those, a detector that never
matches would keep the suite green -- verified: replacing both regexes
with `(?!)` left the earlier version of this file fully passing.

Diagnostics are value-free.  A failure names the file, the line number,
and a bounded reason keyword -- never the matched text.  CI logs for a
public repo are public, so echoing the rejected line would republish the
machine identifier this guard exists to contain.

**Boundary.** This runs in CI, which is after a push.  It blocks
integration; it cannot stop a literal path from first appearing in a
commit on a public branch.  Preventing that requires a local pre-push
hook, which this repo does not currently install.
"""

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# A literal home directory -- /Users/ followed by a real account name --
# but not the parameterized /Users/$USER or /Users/${USER} that macOS
# `dscl` lookups legitimately use (scripts/term-public-troubleshoot.sh,
# setup/bootstrap-macos.sh).
#
# This file is itself tracked and therefore scanned, so example paths use
# a bracketed placeholder (/Users/<name>): `<` is outside the name
# character class.  Where a positive test needs a real-looking literal,
# it is assembled at runtime from parts rather than written out.
HARDCODED_HOME_RE = re.compile(r"/Users/(?!\$)[A-Za-z0-9._-]+")

# Marker comments installers leave behind when they edit a shell profile.
INSTALLER_MARKER_RE = re.compile(
    r"(added by .{0,40}(installer|desktop|toolbox|sdk)"
    r"|the following lines? (were|was) added"
    r"|the next lines? updates? path for"
    r"|>>> .{0,30} initiali[sz]e >>>"
    r"|!! contents within this block are managed by)",
    re.IGNORECASE,
)

# Version managers whose profile blocks are $HOME-parameterized and carry
# no marker comment.  They are identified by the variables and init calls
# their documented setup requires, none of which appear in this repo's own
# config (asserted by test_third_party_tokens_absent_from_repo_config).
THIRD_PARTY_INIT_RE = re.compile(
    r"(NVM_DIR|nvm\.sh"
    r"|PYENV_ROOT|pyenv init"
    r"|CONDA_PREFIX|conda\.sh|conda shell\."
    r"|rbenv init"
    r"|SDKMAN_DIR|sdkman-init"
    r"|google-cloud-sdk/)",
    re.IGNORECASE,
)

# Bounded reason keywords.  These are the only failure vocabulary; no
# matched text ever reaches an assertion message.
REASON_HOME = "hardcoded-home-directory"
REASON_MARKER = "installer-marker-comment"
REASON_THIRD_PARTY = "third-party-init-block"
REASON_HEADER = "file-header-displaced"

CONFIG_PREFIXES = ("bash/", "starship/", "tmux/", "ghostty/", "setup/")
CONFIG_FILES = ("setup.sh",)

# The profiles setup.sh links, and the first line each must still start
# with.  An installer that prepends displaces these.
LINKED_PROFILES = {
    "bash/bash_profile": "# term-public bash_profile.",
    "bash/bashrc": "# term-public bash baseline.",
}

# One real block per installer the README names.  `$HOME` forms are
# verbatim; any literal-home form is assembled in `_literal_home_block`
# so this file never contains one.
INSTALLER_FIXTURES = {
    "docker desktop": (
        "# The following lines were added by Docker Desktop to add commands to your PATH.\n"
        'export PATH="$PATH:$HOME/.docker/bin"\n'
        "# End of Docker Desktop section.\n"
    ),
    "conda": (
        "# >>> conda initialize >>>\n"
        '__conda_setup="$(\'$HOME/miniconda3/bin/conda\' shell.bash hook 2> /dev/null)"\n'
        "# <<< conda initialize <<<\n"
    ),
    "rustup": (
        "# !! Contents within this block are managed by rustup !!\n"
        'source "$HOME/.cargo/env"\n'
    ),
    "google cloud sdk": (
        "# The next line updates PATH for the Google Cloud SDK.\n"
        "if [ -f '$HOME/google-cloud-sdk/path.bash.inc' ]; then . '$HOME/google-cloud-sdk/path.bash.inc'; fi\n"
    ),
    "jetbrains toolbox": (
        "# added by JetBrains Toolbox\n"
        'export PATH="$PATH:$HOME/Library/Application Support/JetBrains/Toolbox/scripts"\n'
    ),
    # The two that motivated this rewrite: $HOME-parameterized, no marker
    # comment.  Caught only by THIRD_PARTY_INIT_RE.
    "nvm": (
        'export NVM_DIR="$HOME/.nvm"\n'
        '[ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"  # This loads nvm\n'
        '[ -s "$NVM_DIR/bash_completion" ] && \\. "$NVM_DIR/bash_completion"\n'
    ),
    "pyenv": (
        'export PYENV_ROOT="$HOME/.pyenv"\n'
        '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"\n'
        'eval "$(pyenv init - bash)"\n'
    ),
}


def _literal_home_block():
    """A hardcoded-home line, assembled so this file never contains one."""
    return "export PATH=" + '"$PATH:' + "/Users/" + "someaccount" + '/tools/bin"\n'


def scan_text(rel_path, text):
    """Return bounded reason strings for one file's content.

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

    expected_header = LINKED_PROFILES.get(rel_path)
    if expected_header is not None and (not lines or lines[0] != expected_header):
        offenders.append(f"{rel_path}:1: {REASON_HEADER}")

    return offenders


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def readable_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan_repo():
    offenders = []
    for path in tracked_files():
        text = readable_text(path)
        if text is None:
            continue
        offenders += scan_text(path.relative_to(REPO_ROOT).as_posix(), text)
    return offenders


class TestRepoIsClean:
    """The tracked tree carries no machine-specific content."""

    def test_repo_has_tracked_files(self):
        """Fail closed: an empty file list would make this vacuous."""
        assert len(tracked_files()) > 10

    def test_this_file_is_tracked(self):
        """The sweep only sees tracked files, so an *unadded* copy of
        this guard exempts itself and passes on an empty scan -- which
        is how its own example paths slipped through locally once.
        (A staged file is already in `git ls-files`; only a never-added
        one is invisible.)"""
        assert Path(__file__).resolve() in {p.resolve() for p in tracked_files()}, (
            "this test file is untracked; `git add` it before trusting a "
            "local pass")

    def test_linked_profiles_exist(self):
        """The header contract names real files."""
        for rel in LINKED_PROFILES:
            assert (REPO_ROOT / rel).is_file(), f"{rel} is missing"

    def test_no_machine_specific_content(self):
        offenders = scan_repo()
        assert not offenders, (
            "machine-specific content in tracked files (move it to "
            "local/env.local and use $HOME):\n  " + "\n  ".join(offenders))

    def test_third_party_tokens_absent_from_repo_config(self):
        """THIRD_PARTY_INIT_RE must not collide with this repo's own
        config, or the detector would be unusable."""
        for rel in list(LINKED_PROFILES) + ["setup.sh"]:
            text = (REPO_ROOT / rel).read_text()
            assert not THIRD_PARTY_INIT_RE.search(text), (
                f"{rel} contains a third-party init token; the detector "
                "needs narrowing")


class TestDetectorsAreLive:
    """Positive cases.  Without these the detectors could never match and
    the suite would stay green -- the state this file shipped in."""

    def test_fixture_tables_are_populated(self):
        """Fail closed.  Emptying either table collapses every
        parametrized case below to zero instances, and pytest reports
        zero instances as success -- the positives would vanish rather
        than fail."""
        assert INSTALLER_FIXTURES, "no installer fixtures; positives are vacuous"
        assert set(LINKED_PROFILES) == {"bash/bash_profile", "bash/bashrc"}, (
            "both linked profiles must be exercised")

    @pytest.mark.parametrize("installer", sorted(INSTALLER_FIXTURES))
    @pytest.mark.parametrize("profile", sorted(LINKED_PROFILES))
    @pytest.mark.parametrize("where", ["prepend", "append"])
    def test_installer_block_rejected(self, installer, profile, where):
        """Every named installer, in both linked profiles, both ends."""
        original = (REPO_ROOT / profile).read_text()
        block = INSTALLER_FIXTURES[installer]
        text = block + original if where == "prepend" else original + block
        assert scan_text(profile, text), (
            f"{installer} block ({where}ed to {profile}) was not detected")

    def test_hardcoded_home_rejected(self):
        for profile in LINKED_PROFILES:
            text = (REPO_ROOT / profile).read_text() + _literal_home_block()
            reasons = scan_text(profile, text)
            assert any(REASON_HOME in r for r in reasons), (
                f"literal home path in {profile} was not detected")

    def test_prepend_displaces_header(self):
        """A prepended block is caught by the header landmark even if its
        wording is unknown to every regex."""
        for profile in LINKED_PROFILES:
            text = "# some future installer nobody has seen\n" \
                   + (REPO_ROOT / profile).read_text()
            reasons = scan_text(profile, text)
            assert any(REASON_HEADER in r for r in reasons), (
                f"prepended unknown block in {profile} was not detected")

    def test_readme_named_installers_have_fixtures(self):
        """Both ends of the documentation contract: every installer the
        README claims is covered must have a fixture proving it."""
        readme = (REPO_ROOT / "README.md").read_text().lower()
        section = readme[readme.index("installers that edit your shell profile"):]
        section = section[:section.index("\n## ")] if "\n## " in section else section
        named = [n for n in ("docker desktop", "conda", "nvm", "rustup",
                             "pyenv", "google cloud sdk", "jetbrains toolbox")
                 if n.split()[0] in section]
        assert named, "README no longer names any installer"
        missing = [n for n in named if n not in INSTALLER_FIXTURES]
        assert not missing, (
            f"README claims coverage with no fixture proving it: {missing}")

    def test_clean_profiles_are_not_flagged(self):
        """Negative control: the real files pass, so the positives above
        are detecting the block and not merely the file."""
        for profile in LINKED_PROFILES:
            assert not scan_text(profile, (REPO_ROOT / profile).read_text())


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
        allowed = {REASON_HOME, REASON_MARKER, REASON_THIRD_PARTY, REASON_HEADER}
        for text in self._texts().values():
            for reason in scan_text("bash/bash_profile", text):
                keyword = reason.rsplit(": ", 1)[-1]
                assert keyword in allowed, f"unbounded reason: {keyword!r}"

    def test_assertion_message_is_value_free(self):
        """End-to-end: the message a failing run would print carries no
        rejected content."""
        text = (REPO_ROOT / "bash" / "bash_profile").read_text() \
            + "export PATH=" + '"/Users/' + self.SENTINEL + '/bin"\n'
        offenders = scan_text("bash/bash_profile", text)
        message = ("machine-specific content in tracked files (move it to "
                   "local/env.local and use $HOME):\n  " + "\n  ".join(offenders))
        assert self.SENTINEL not in message
