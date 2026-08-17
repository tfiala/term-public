"""Tests for the base tmux config's copy-mode & message styling.

tmux's defaults for these styles use ANSI named colors, which the
appearance-pair Ghostty theme remaps per mode — the day theme maps
yellow/cyan/magenta/red to dark shades, turning the defaults
dark-on-dark (an invisible mouse-drag selection). The base config must
pin every style it sets in truecolor hex so the palette remap can't
reach it.
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

# All spellings tmux accepts for setting an option: set, set-option,
# setw, set-window-option — any of them can assign a style.
_SET_STYLE = re.compile(
    r"^\s*set(?:-option|-window-option|w)?\s+(?:-[a-zA-Z]+\s+)*"
    r'([\w-]+-style)\s+"?([^"\n]*?)"?\s*$'
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _style_settings(text: str) -> list[tuple[str, str]]:
    """Every style assignment in the config, in order, all spellings."""
    return [
        (m.group(1), m.group(2))
        for line in text.splitlines()
        if (m := _SET_STYLE.match(line))
    ]


def _color_violations(text: str) -> list[str]:
    """Color-valued style attributes that are not truecolor hex.

    Scans every assignment (not last-wins), so a later good line can't
    mask an earlier bad one.
    """
    violations = []
    for style, value in _style_settings(text):
        for part in value.split(","):
            if "=" not in part:
                continue
            key, color = part.split("=", 1)
            if key in COLOR_ATTRS and not _HEX_COLOR.match(color):
                violations.append(f"{style}: {key}={color}")
    return violations


def test_every_ansi_defaulted_style_is_pinned():
    settings = dict(_style_settings(TMUX_CONF.read_text()))
    missing = [s for s in REQUIRED_ATTRS if s not in settings]
    assert not missing, f"styles left at ANSI-named tmux defaults: {missing}"


def test_pinned_styles_set_their_required_attributes():
    settings = dict(_style_settings(TMUX_CONF.read_text()))
    for style, required in REQUIRED_ATTRS.items():
        value = settings.get(style, "")
        present = {
            part.split("=", 1)[0] for part in value.split(",") if "=" in part
        }
        missing = required - present
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
    ],
)
def test_mutated_config_fails_the_color_sweep(mutation):
    """Negative proof: each bad spelling is detected, not skipped.

    Appends the mutation so a compliant earlier assignment of the same
    style cannot mask it.
    """
    mutated = TMUX_CONF.read_text() + "\n" + mutation + "\n"
    assert _color_violations(mutated), f"guard missed: {mutation!r}"


def test_dropping_fill_from_message_style_is_caught():
    """Negative proof for the required-attribute check."""
    text = TMUX_CONF.read_text()
    stripped, n = re.subn(r",fill=#[0-9a-fA-F]{6}", "", text)
    assert n == 2, "expected fill= on both message styles"
    settings = dict(_style_settings(stripped))
    for style in ("message-style", "message-command-style"):
        present = {
            part.split("=", 1)[0]
            for part in settings[style].split(",")
            if "=" in part
        }
        assert "fill" not in present  # the mutation really removed it
        assert not REQUIRED_ATTRS[style] <= present, (
            f"required-attribute check would miss a dropped fill on {style}"
        )
