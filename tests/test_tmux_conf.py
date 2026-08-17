"""Tests for the base tmux config's copy-mode & message styling.

tmux's defaults for these styles use ANSI named colors, which the
appearance-pair Ghostty theme remaps per mode — the day theme maps
yellow/cyan/magenta/red to dark shades, turning the defaults
dark-on-dark (an invisible mouse-drag selection). The base config must
pin every style it sets in truecolor hex so the palette remap can't
reach it.

The guard fails closed: a line that sets a ``*-style`` option but does
not match the canonical parser below is itself reported as a violation,
so unrecognized-but-valid tmux syntax can never smuggle a named color
past the sweep.
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

# Broad detector: any set-form line that mentions a *-style option at
# all. Lines it sees that the canonical parser cannot handle are
# reported as violations rather than skipped (fail closed).
_STYLE_MENTION = re.compile(
    r"^\s*set(?:-option|-[\w-]+|w)?\s+(?=.*[\w-]+-style)"
)

# The value tail must be exactly one double-quoted, single-quoted, or
# bare token, followed by nothing but an optional comment.
_VALUE_FORMS = (
    re.compile(r'^"([^"]*)"\s*(?:#.*)?$'),
    re.compile(r"^'([^']*)'\s*(?:#.*)?$"),
    re.compile(r"^([^\s\"']+)\s*(?:#.*)?$"),
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _parse_value(tail: str) -> str | None:
    """The style value from a set line's tail, or None if unparseable."""
    for form in _VALUE_FORMS:
        m = form.match(tail)
        if m:
            return m.group(1)
    return None


def _style_assignments(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """All style assignments in order, plus unparseable style lines."""
    settings: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line in text.splitlines():
        m = _STYLE_LINE.match(line)
        if m:
            value = _parse_value(m.group(2))
            if value is None:
                malformed.append(line)
            else:
                settings.append((m.group(1), value))
        elif _STYLE_MENTION.match(line):
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
    mask an earlier bad one. Unparseable style lines are violations in
    their own right — the guard never skips what it can't read.
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
        # flag parse; the mention detector must still see the style.
        "set -t work mode-style fg=red",
    ],
)
def test_unparseable_style_lines_fail_closed(unparseable):
    """A style assignment the parser can't read is itself a violation."""
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
    for line in TMUX_CONF.read_text().splitlines():
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
