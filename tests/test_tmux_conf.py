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

TMUX_CONF = Path(__file__).resolve().parents[1] / "tmux" / "tmux.conf"

# Styles whose tmux defaults are ANSI named colors; each must be pinned.
PINNED_STYLES = [
    "mode-style",
    "copy-mode-match-style",
    "copy-mode-current-match-style",
    "copy-mode-mark-style",
    "message-style",
    "message-command-style",
]

_SET_STYLE = re.compile(
    r'^\s*set(?:-option)?\s+(?:-[a-zA-Z]+\s+)*([\w-]+-style)\s+"?([^"\n]*)"?\s*$'
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _style_settings() -> dict[str, str]:
    """Every `set ... <option>-style <value>` line in the base config."""
    settings = {}
    for line in TMUX_CONF.read_text().splitlines():
        m = _SET_STYLE.match(line)
        if m:
            settings[m.group(1)] = m.group(2)
    return settings


def test_every_ansi_defaulted_style_is_pinned():
    settings = _style_settings()
    missing = [s for s in PINNED_STYLES if s not in settings]
    assert not missing, f"styles left at ANSI-named tmux defaults: {missing}"


def test_pinned_styles_set_both_fg_and_bg():
    settings = _style_settings()
    for style in PINNED_STYLES:
        value = settings.get(style, "")
        attrs = dict(
            part.split("=", 1) for part in value.split(",") if "=" in part
        )
        assert "fg" in attrs and "bg" in attrs, (
            f"{style} must pin both fg and bg, got: {value!r}"
        )


def test_no_style_in_config_uses_ansi_named_colors():
    """Defect-class sweep: any style set here must be truecolor-only.

    A named color (yellow, brightcyan, ...) or a palette index
    (colour123) changes meaning with the appearance-pair theme; only
    #rrggbb hex renders identically in day and night mode.
    """
    for style, value in _style_settings().items():
        for part in value.split(","):
            if "=" not in part:
                continue
            key, color = part.split("=", 1)
            if key not in ("fg", "bg"):
                continue
            assert _HEX_COLOR.match(color), (
                f"{style} uses non-truecolor {key}={color!r}; "
                "pin a #rrggbb value instead"
            )
