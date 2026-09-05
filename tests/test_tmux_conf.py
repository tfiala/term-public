"""Regression tests for tmux copy-mode and message colors.

Ghostty remaps ANSI colors when the system appearance changes.  Any tmux
style color that uses a name (``red``), palette index (``colour123``), or
dynamic value can therefore change meaning between the day and night themes.

The production contract has two deliberately separate proofs:

* the six affected options occur exactly once with their intended truecolor
  maps, including ``fill`` on both message styles; and
* every literal tmux style color attribute on a command-bearing logical line
  is ``#rrggbb``, independent of how the surrounding option target is quoted,
  escaped, abbreviated, continued, nested, or expanded.

Keeping the value guard independent of option-name parsing is intentional.
tmux has a rich command lexer; the defect is the palette-dependent color
value, not the spelling used to reach an option.
"""

import re
from pathlib import Path

import pytest

TMUX_CONF = Path(__file__).resolve().parents[1] / "tmux" / "tmux.conf"

EXPECTED_STYLE_COLORS = {
    "mode-style": {"fg": "#eceff1", "bg": "#607d8b"},
    "copy-mode-match-style": {"fg": "#292d3e", "bg": "#ffcb6b"},
    "copy-mode-current-match-style": {
        "fg": "#292d3e",
        "bg": "#ffa726",
    },
    "copy-mode-mark-style": {"fg": "#292d3e", "bg": "#f07178"},
    "message-style": {
        "fg": "#292d3e",
        "bg": "#ffcb6b",
        "fill": "#ffcb6b",
    },
    "message-command-style": {
        "fg": "#ffcb6b",
        "bg": "#292d3e",
        "fill": "#292d3e",
    },
}

COLOR_ATTRS = frozenset({"fg", "bg", "us", "fill"})

_DIRECT_STYLE = re.compile(
    r"^\s*set(?:-option|-window-option|w)?\s+(?:-[a-zA-Z]+\s+)*"
    r"(?P<option>[\w-]+-style)\s+(?P<value>.+?)\s*$"
)
_COLOR_ASSIGNMENT = re.compile(
    r"(?<![\w-])(?P<attr>fg|bg|us|fill)\s*=\s*"
    r"(?P<color>[^\s,;]+)"
)
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _continues(line: str) -> bool:
    """Whether a physical line ends in an unescaped continuation slash."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _logical_lines(text: str) -> list[str]:
    """Join backslash-newline continuations as tmux does."""
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


def _literal_command(line: str) -> str:
    """Normalize literal tmux quoting/escapes and remove an inline comment.

    This is intentionally not a tmux option parser.  It exposes literal style
    terms regardless of how other command words are assembled.  Environment
    references remain visible and therefore fail the hex-only check when used
    as a color value.
    """
    result: list[str] = []
    quote: str | None = None
    escaped = False

    for index, char in enumerate(line):
        if escaped:
            result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
            else:
                result.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        else:
            result.append(char)

    if escaped:
        result.append("\\")
    return "".join(result)


def _style_terms(value: str) -> list[str]:
    return [term for term in re.split(r"[\s,]+", value.strip()) if term]


def _style_colors(value: str) -> dict[str, str]:
    colors = {}
    for term in _style_terms(value):
        if "=" not in term:
            continue
        key, color = term.split("=", 1)
        if key in COLOR_ATTRS:
            colors[key] = color
    return colors


def _direct_style_assignments(text: str) -> list[tuple[str, str]]:
    assignments = []
    for logical_line in _logical_lines(text):
        command = _literal_command(logical_line)
        if match := _DIRECT_STYLE.match(command):
            assignments.append((match.group("option"), match.group("value")))
    return assignments


def _expected_style_violations(text: str) -> list[str]:
    assignments = _direct_style_assignments(text)
    violations = []
    for option, expected in EXPECTED_STYLE_COLORS.items():
        values = [value for name, value in assignments if name == option]
        if len(values) != 1:
            violations.append(f"{option}: expected one assignment, got {len(values)}")
            continue
        actual = _style_colors(values[0])
        if actual != expected:
            violations.append(f"{option}: expected {expected}, got {actual}")
    return violations


def _color_violations(text: str) -> list[str]:
    """Return every literal style color attribute that is not #rrggbb."""
    violations = []
    for logical_line in _logical_lines(text):
        command = _literal_command(logical_line)
        for match in _COLOR_ASSIGNMENT.finditer(command):
            color = match.group("color")
            if not _HEX_COLOR.fullmatch(color):
                violations.append(f"{match.group('attr')}={color}")
    return violations


def test_expected_styles_are_pinned_once_with_exact_colors():
    assert _expected_style_violations(TMUX_CONF.read_text()) == []


def test_all_literal_style_colors_are_truecolor():
    assert _color_violations(TMUX_CONF.read_text()) == []


def test_arbitrary_window_index_prompt_binding_is_explicit():
    lines = TMUX_CONF.read_text().splitlines()
    expected = r'''bind \' command-prompt -p "window:" "select-window -t ':%%'"'''
    bindings = [line for line in lines if line.startswith(r"bind \' ")]
    assert bindings == [expected]


@pytest.mark.parametrize(
    "mutation",
    [
        'set -g mode-style "fg=red,bg=#607d8b"',
        'set -g message-style "fg=#292d3e,bg=#ffcb6b,fill=yellow"',
        'set -g mode-style "fg=#eceff1,bg=#607d8b,us=colour123"',
        'set-window-option -g mode-style "fg=red,bg=#607d8b"',
        'setw -g mode-style "fg=red,bg=#607d8b"',
        'set-option -g mode-style fg=red,bg=#607d8b',
        'set -g mode-style "bold fg=red bg=#607d8b"',
        'set -g mode-style "fg=#eceff1, bg=red" # comment',
        "set -g mode-style 'fg=red,bg=#607d8b'",
        'set -g mode-style \\\n          "fg=red,bg=#607d8b"',
        'set -g \\\n          mode-style "fg=red,bg=#607d8b"',
        "if-shell 'true' 'set -g mode-style \"fg=red,bg=#607d8b\"'",
        'bind M set -g mode-style "fg=red,bg=#607d8b"',
        'set -g mode-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-match-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-current-match-sty "fg=red,bg=#607d8b"',
        'set -g copy-mode-mark-sty "fg=red,bg=#607d8b"',
        'set -g message-sty "fg=red,bg=#607d8b"',
        'set -g message-command-sty "fg=red,bg=#607d8b"',
        'set -g mo\\de-sty "fg=red,bg=#607d8b"',
        'set -g mo\'de-sty\' "fg=red,bg=#607d8b"',
        'set -g mo"de-sty" "fg=red,bg=#607d8b"',
        'set -g "$REVIEW_STYLE_OPTION" "fg=red,bg=#607d8b"',
        'set -g mode-style "f\\g=red,bg=#607d8b"',
        "set -g mode-style f'g'=red,bg=#607d8b",
    ],
)
def test_non_truecolor_values_fail_regardless_of_command_shape(mutation):
    assert _color_violations(mutation), f"guard missed: {mutation!r}"


@pytest.mark.parametrize(
    "compliant",
    [
        'set -g mode-style "fg=#eceff1,bg=#607d8b"',
        'set -g mode-style "bold fg=#eceff1 bg=#607d8b"',
        'set -g mode-style "fg=#eceff1, bg=#607d8b" # comment',
        "set -g mode-style 'fg=#eceff1,bg=#607d8b'",
        'set -g mode-sty "fg=#eceff1,bg=#607d8b"',
        'set -g mo\\de-sty "fg=#eceff1,bg=#607d8b"',
        'set -g "$REVIEW_STYLE_OPTION" "fg=#eceff1,bg=#607d8b"',
        'set -g @mode-sty "unrelated"',
        'set -g mouse on # an example fg=red belongs only to this comment',
        '# set -g mode-style "fg=red,bg=black"',
    ],
)
def test_truecolor_and_unrelated_commands_do_not_false_positive(compliant):
    assert _color_violations(compliant) == []


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (",fill=#ffcb6b", ""),
        (",fill=#292d3e", ""),
        ("fg=#eceff1,bg=#607d8b", "fg=#ffffff,bg=#607d8b"),
    ],
)
def test_required_style_mutations_fail(needle, replacement):
    original = TMUX_CONF.read_text()
    assert original.count(needle) == 1
    mutated = original.replace(needle, replacement, 1)
    assert _expected_style_violations(mutated)


def test_duplicate_required_style_assignment_fails():
    original = TMUX_CONF.read_text()
    duplicate = 'set -g mode-style "fg=#eceff1,bg=#607d8b"'
    assert _expected_style_violations(original + "\n" + duplicate + "\n")
