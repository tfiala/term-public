"""Tests for the base tmux config's copy-mode & message styling.

tmux's defaults for these styles use ANSI named colors, which the
appearance-pair Ghostty theme remaps per mode — the day theme maps
yellow/cyan/magenta/red to dark shades, turning the defaults
dark-on-dark (an invisible mouse-drag selection). The base config must
pin every style it sets in truecolor hex so the palette remap can't
reach it.

The guard works on logical lines (tmux's backslash-newline
continuations joined exactly as tmux joins them) and fails closed: any
non-comment command containing a ``*-style`` token that the canonical
direct-``set`` parser does not handle — nested commands like
``if-shell '...' 'set ...'`` or ``bind X set ...`` included — is
itself reported as a violation, so unrecognized-but-valid tmux syntax
can never smuggle a named color past the sweep.

tmux also resolves unambiguous option-name abbreviations (``mode-sty``
sets ``mode-style``), which carry no literal ``-style`` token. Policy:
this config must spell style option names in full. Any token that is a
proper prefix of a known style option name — in a direct set or a
nested payload — fails closed, backed by a frozen list of tmux 3.7's
style option names. Complete option names that happen to prefix a
style name (``status``, ``status-left``, ...) stay exempt, since tmux
resolves exact names before abbreviations.
"""

import re
from pathlib import Path

import pytest

TMUX_CONF = Path(__file__).resolve().parents[1] / "tmux" / "tmux.conf"

# Styles whose tmux defaults are ANSI named colors, and the attributes
# each must pin. The message styles' defaults also carry fill= (used by
# the default message-format to clear the bar's full width), so pinning
# only fg/bg would silently regress the bar background.
REQUIRED_ATTRS = {
    "mode-style": {"fg", "bg"},
    "copy-mode-match-style": {"fg", "bg"},
    "copy-mode-current-match-style": {"fg", "bg"},
    "copy-mode-mark-style": {"fg", "bg"},
    "message-style": {"fg", "bg", "fill"},
    "message-command-style": {"fg", "bg", "fill"},
}

# Every color-valued attribute in tmux's style grammar. Attributes like
# bold/noattr carry no color and are exempt from the truecolor rule.
COLOR_ATTRS = ("fg", "bg", "us", "fill")

# Canonical parser: a set spelling (set, set-option, setw,
# set-window-option), bare flags, the option name, then a value tail.
_STYLE_LINE = re.compile(
    r"^\s*set(?:-option|-window-option|w)?\s+(?:-[a-zA-Z]+\s+)*"
    r"([\w-]+-style)\s+(.+?)\s*$"
)

# Broad detector: a style-option-shaped token anywhere in a command.
# Non-comment logical lines it matches that the canonical parser cannot
# handle are reported as violations rather than skipped (fail closed) —
# this is what catches nested payloads and unforeseen command shapes.
_STYLE_TOKEN = re.compile(r"[\w-]+-style\b")

# Every style-valued option name in tmux 3.7 (from show-options on a
# pristine server). A style option added by a future tmux is still
# caught by _STYLE_TOKEN when spelled in full; only its abbreviations
# would need this list extended.
STYLE_OPTIONS = frozenset({
    "clock-mode-style",
    "copy-mode-current-line-number-style",
    "copy-mode-current-match-style",
    "copy-mode-line-number-style",
    "copy-mode-mark-style",
    "copy-mode-match-style",
    "copy-mode-position-style",
    "copy-mode-selection-style",
    "cursor-style",
    "menu-border-style",
    "menu-selected-style",
    "menu-style",
    "message-command-style",
    "message-style",
    "mode-style",
    "pane-active-border-style",
    "pane-border-style",
    "pane-scrollbars-style",
    "pane-status-current-style",
    "pane-status-style",
    "popup-border-style",
    "popup-style",
    "prompt-command-cursor-style",
    "prompt-cursor-style",
    "session-status-current-style",
    "session-status-style",
    "status-left-style",
    "status-right-style",
    "status-style",
    "tree-mode-preview-style",
    "window-active-style",
    "window-status-activity-style",
    "window-status-bell-style",
    "window-status-current-style",
    "window-status-last-style",
    "window-status-style",
    "window-style",
})

# Complete tmux option names that are proper prefixes of a style name.
# tmux resolves an exact option name before considering abbreviations,
# so these are legitimate targets, never style abbreviations.
_ABBREV_EXEMPT = frozenset({
    "pane-scrollbars",
    "status",
    "status-left",
    "status-right",
})

# Any token tmux could accept as an abbreviation of a style option.
# Proper prefixes shorter than 4 characters are always ambiguous in
# tmux's option table, so 4 is a safe floor that keeps the net from
# matching stray short words.
_STYLE_ABBREVS = frozenset(
    name[:i]
    for name in STYLE_OPTIONS
    for i in range(4, len(name))
) - _ABBREV_EXEMPT

_TOKEN = re.compile(r"[\w-]+")

# The value tail must be exactly one double-quoted, single-quoted, or
# bare token, followed by nothing but an optional comment.
_VALUE_FORMS = (
    re.compile(r'^"([^"]*)"\s*(?:#.*)?$'),
    re.compile(r"^'([^']*)'\s*(?:#.*)?$"),
    re.compile(r"^([^\s\"']+)\s*(?:#.*)?$"),
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _continues(line: str) -> bool:
    """True if the line ends in an unescaped backslash (continuation)."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _logical_lines(text: str) -> list[str]:
    """Join backslash-newline continuations exactly as tmux does.

    The backslash and newline are removed with nothing inserted — a
    token split across the boundary joins back into one token, keeping
    this view faithful to what tmux executes. Joining happens before
    comment detection, matching tmux's parse order.
    """
    logical: list[str] = []
    pending = ""
    for line in text.splitlines():
        if _continues(line):
            pending += line[:-1]
        else:
            logical.append(pending + line)
            pending = ""
    if pending:
        logical.append(pending)
    return logical


def _parse_value(tail: str) -> str | None:
    """The style value from a set line's tail, or None if unparseable."""
    for form in _VALUE_FORMS:
        m = form.match(tail)
        if m:
            return m.group(1)
    return None


def _mentions_style(line: str) -> bool:
    """True if the line carries a style option, spelled out or abbreviated."""
    if _STYLE_TOKEN.search(line):
        return True
    return any(t in _STYLE_ABBREVS for t in _TOKEN.findall(line))


def _style_assignments(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """All style assignments in order, plus unhandled style-bearing lines."""
    settings: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _STYLE_LINE.match(line)
        if m:
            value = _parse_value(m.group(2))
            if value is None:
                malformed.append(line)
            else:
                settings.append((m.group(1), value))
        elif _mentions_style(line):
            malformed.append(line)
    return settings, malformed


def _style_terms(value: str) -> list[str]:
    """A style value's terms — tmux separates them by commas or spaces."""
    return [t for t in re.split(r"[\s,]+", value.strip()) if t]


def _attrs_present(value: str) -> set[str]:
    return {t.split("=", 1)[0] for t in _style_terms(value) if "=" in t}


def _color_violations(text: str) -> list[str]:
    """Color-valued style attributes that are not truecolor hex.

    Scans every assignment (not last-wins), so a later good line can't
    mask an earlier bad one. Style-bearing lines the parser can't read
    are violations in their own right — the guard never skips what it
    can't read.
    """
    settings, malformed = _style_assignments(text)
    violations = [f"unparseable style assignment: {ln.strip()}"
                  for ln in malformed]
    for style, value in settings:
        for term in _style_terms(value):
            if "=" not in term:
                continue
            key, color = term.split("=", 1)
            if key in COLOR_ATTRS and not _HEX_COLOR.match(color):
                violations.append(f"{style}: {key}={color}")
    return violations


def test_base_config_parses_canonically():
    _, malformed = _style_assignments(TMUX_CONF.read_text())
    assert malformed == []


def test_every_ansi_defaulted_style_is_pinned():
    settings, _ = _style_assignments(TMUX_CONF.read_text())
    present = dict(settings)
    missing = [s for s in REQUIRED_ATTRS if s not in present]
    assert not missing, f"styles left at ANSI-named tmux defaults: {missing}"


def test_pinned_styles_set_their_required_attributes():
    settings, _ = _style_assignments(TMUX_CONF.read_text())
    present = dict(settings)
    for style, required in REQUIRED_ATTRS.items():
        value = present.get(style, "")
        missing = required - _attrs_present(value)
        assert not missing, (
            f"{style} must pin {sorted(required)}, missing "
            f"{sorted(missing)} in: {value!r}"
        )


def test_no_style_in_config_uses_non_truecolor_colors():
    """Defect-class sweep over the real config: truecolor-only.

    A named color (yellow, brightcyan, ...) or a palette index
    (colour123) changes meaning with the appearance-pair theme; only
    #rrggbb hex renders identically in day and night mode.
    """
    assert _color_violations(TMUX_CONF.read_text()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        # fill is color-valued too — an ANSI-named fill must be caught.
        'set -g message-style "fg=#292d3e,bg=#ffcb6b,fill=yellow"',
        # Underscore color is the fourth color-valued attribute.
        'set -g mode-style "fg=#eceff1,bg=#607d8b,us=red"',
        # A palette-cube index flips with the theme just like a name.
        'set -g mode-style "fg=colour123,bg=#607d8b"',
        # The long and short window-option spellings assign styles too.
        'set-window-option -g mode-style "fg=red,bg=#607d8b"',
        'setw -g mode-style "fg=red,bg=#607d8b"',
        # Unquoted values are valid tmux syntax as well.
        "set -g mode-style fg=red,bg=#607d8b",
        # set-option is a synonym for set.
        'set-option -g message-style "fg=black,bg=#ffcb6b,fill=#ffcb6b"',
        # tmux accepts whitespace after the comma between terms.
        'set -g mode-style "fg=#eceff1, bg=red"',
        # Terms may be space-separated, including after a non-color
        # attribute keyword.
        'set -g mode-style "bold fg=red bg=#607d8b"',
        # An inline comment must not hide the assignment from the guard.
        'set -g mode-style "fg=#eceff1,bg=red" # matches selection',
        # Single-quoted values are valid tmux syntax as well.
        "set -g mode-style 'fg=red,bg=#607d8b'",
        "set -g mode-style 'fg=red,bg=#607d8b' # comment",
        # Bare values can carry a trailing comment too.
        "set -g mode-style fg=red,bg=#607d8b # comment",
        # A backslash continuation after the option name is one logical
        # command to tmux; joining must expose the assignment.
        'set -g mode-style \\\n  "fg=red,bg=#607d8b"',
        # Continuation can split the command even before the option.
        'set -g \\\n  mode-style "fg=red,bg=#607d8b"',
    ],
)
def test_mutated_config_fails_the_color_sweep(mutation):
    """Negative proof: each bad spelling is detected, not skipped.

    Appends the mutation so a compliant earlier assignment of the same
    style cannot mask it.
    """
    mutated = TMUX_CONF.read_text() + "\n" + mutation + "\n"
    assert _color_violations(mutated), f"guard missed: {mutation!r}"


@pytest.mark.parametrize(
    "unparseable",
    [
        # Trailing junk after the value — not valid single-value syntax.
        'set -g mode-style "fg=#eceff1,bg=#607d8b" stray',
        # An escaped quote defeats the simple quote scanner.
        'set -g mode-style "fg=\\"x\\""',
        # A flag taking an argument (-t target) breaks the canonical
        # flag parse; the token detector must still see the style.
        "set -t work mode-style fg=red",
        # Nested command payloads execute style assignments too — the
        # base config already uses if-shell around set for
        # default-shell, so this shape is not hypothetical.
        "if-shell 'true' 'set -g mode-style \"fg=red,bg=#607d8b\"'",
        # A key binding can carry a set command as its action.
        'bind M set -g mode-style "fg=red,bg=#607d8b"',
        # tmux resolves unambiguous option-name abbreviations, which
        # carry no literal -style token — each of these sets the real
        # option on tmux 3.7b.
        'set -g mode-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-match-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-current-match-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-mark-sty "fg=red,bg=#607d8b"',
        'set -g message-sty "fg=red,bg=#607d8b"',
        'set -g message-command-sty "fg=red,bg=#607d8b"',
        # Abbreviations can cut far deeper than the -style suffix.
        'set -g mode-s "fg=red,bg=#607d8b"',
        # An abbreviation inside a nested payload must be seen too.
        "if-shell 'true' 'set -g message-sty \"fg=red,bg=#607d8b\"'",
    ],
)
def test_unhandled_style_lines_fail_closed(unparseable):
    """A style-bearing command the parser can't read is a violation."""
    mutated = TMUX_CONF.read_text() + "\n" + unparseable + "\n"
    assert any(
        v.startswith("unparseable style assignment")
        for v in _color_violations(mutated)
    ), f"guard silently skipped: {unparseable!r}"


@pytest.mark.parametrize(
    "compliant",
    [
        # The accepted syntax variants must not false-positive when the
        # colors themselves are truecolor.
        "set -g mode-style 'fg=#eceff1,bg=#607d8b'",
        'set -g mode-style "bold fg=#eceff1 bg=#607d8b"',
        'set -g mode-style "fg=#eceff1, bg=#607d8b"',
        'set -g mode-style "fg=#eceff1,bg=#607d8b" # why',
        "set -g mode-style fg=#eceff1,bg=#607d8b # why",
        'set -g message-style "noattr fg=#292d3e bg=#ffcb6b fill=#ffcb6b"',
        # A compliant continued command parses instead of failing closed.
        'set -g mode-style \\\n  "fg=#eceff1,bg=#607d8b"',
        # Joining is faithful: a token split across the continuation
        # boundary reassembles without an inserted space, as in tmux.
        'set -g mode-sty\\\nle "fg=#eceff1,bg=#607d8b"',
        # Complete option names that happen to prefix a style name are
        # exact matches to tmux, not abbreviations — they must not trip
        # the abbreviation net.
        "set -g status off",
        'set -g status-left " #{session_name} "',
        'set -g status-right " %H:%M "',
    ],
)
def test_compliant_syntax_variants_pass_the_sweep(compliant):
    mutated = TMUX_CONF.read_text() + "\n" + compliant + "\n"
    assert _color_violations(mutated) == []


def test_dropping_fill_from_message_style_is_caught():
    """Negative proof for the required-attribute check.

    Strips fill= from the two message-style assignments only, so a
    future compliant style that also pins a truecolor fill (e.g. a
    status-style) cannot break this test.
    """
    message_styles = ("message-style", "message-command-style")
    lines = []
    stripped = 0
    for line in _logical_lines(TMUX_CONF.read_text()):
        m = _STYLE_LINE.match(line)
        if m and m.group(1) in message_styles:
            line, n = re.subn(r",?fill=#[0-9a-fA-F]{6}", "", line)
            stripped += n
        lines.append(line)
    assert stripped == 2, "expected fill= on both message styles"
    settings, malformed = _style_assignments("\n".join(lines))
    assert malformed == []
    present = dict(settings)
    for style in message_styles:
        attrs = _attrs_present(present[style])
        assert "fill" not in attrs  # the mutation really removed it
        assert not REQUIRED_ATTRS[style] <= attrs, (
            f"required-attribute check would miss a dropped fill on {style}"
        )
