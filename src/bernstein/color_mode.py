"""Terminal color mode detection — auto-detect truecolor/256-color/ANSI.

Probes the terminal environment to determine the best color rendering
mode so that Rich-based TUI output uses the optimal palette available.
"""

from __future__ import annotations

import os
from enum import Enum


class ColorMode(Enum):
    """Terminal color capability level."""

    TRUECOLOR = "truecolor"  # 24-bit RGB
    COLOR_256 = "256color"  # 256-color palette
    ANSI = "ansi"  # 16-color standard
    NONE = "none"  # No color support


def detect_color_mode() -> ColorMode:
    """Detect the terminal color mode.

    Checks environment variables and terminal type in priority order:
    1. COLORTERM environment variable (truecolor, 24bit → truecolor)
    2. TERM environment variable (xterm-256color → 256color)
    3. TERM variable contains "color" → ansi
    4. TERM is "dumb" or empty → none
    5. Fallback for CI environments (GitHub Actions, etc.)

    Returns:
        The detected ColorMode.
    """
    # COLORTERM is the most reliable indicator for truecolor
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return ColorMode.TRUECOLOR

    # Known CI environments
    if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
        return ColorMode.ANSI

    # TERM variable encoding - check if it's explicitly set
    term = os.environ.get("TERM")
    if term is not None:
        term = term.lower()
        if not term or term == "dumb":
            return ColorMode.NONE
        # Check for xterm-256color, screen-256color, etc.
        if "256color" in term or "256-color" in term:
            return ColorMode.COLOR_256
        # Generic color terminal
        if "color" in term:
            return ColorMode.ANSI

    # Conservative default — assume at least ANSI colors
    return ColorMode.ANSI


def color_mode_supports_truecolor(mode: ColorMode) -> bool:
    """Check if a color mode supports truecolor (24-bit) rendering.

    Args:
        mode: The color mode to check.

    Returns:
        True only for truecolor mode.
    """
    return mode == ColorMode.TRUECOLOR


def color_mode_supports_256(mode: ColorMode) -> bool:
    """Check if a color mode supports 256-color palette.

    Args:
        mode: The color mode to check.

    Returns:
        True for 256color or truecolor mode.
    """
    return mode in (ColorMode.TRUECOLOR, ColorMode.COLOR_256)
