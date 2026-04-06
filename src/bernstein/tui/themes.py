"""TUI-011: Dark/light theme support.

Provides theme definitions and auto-detection for the Bernstein TUI.
Supports dark and light themes, with high-contrast variants for
accessibility. Themes can be auto-detected from the terminal or
configured explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class ThemeMode(Enum):
    """Available theme modes."""

    DARK = "dark"
    LIGHT = "light"
    HIGH_CONTRAST = "high_contrast"
    AUTO = "auto"


@dataclass(frozen=True)
class ThemeColors:
    """Color definitions for a TUI theme.

    All colors are Rich color names or hex values.

    Attributes:
        background: Main background color.
        foreground: Main text color.
        primary: Primary accent color.
        secondary: Secondary accent color.
        success: Success/positive color.
        warning: Warning color.
        error: Error/negative color.
        muted: Muted/dim text color.
        border: Border color.
        selection: Selection/cursor background color.
        status_running: Color for running state.
        status_done: Color for completed state.
        status_failed: Color for failed state.
        status_pending: Color for pending/open state.
    """

    background: str
    foreground: str
    primary: str
    secondary: str
    success: str
    warning: str
    error: str
    muted: str
    border: str
    selection: str
    status_running: str
    status_done: str
    status_failed: str
    status_pending: str


# Theme definitions
DARK_THEME = ThemeColors(
    background="#1e1e2e",
    foreground="#cdd6f4",
    primary="#89b4fa",
    secondary="#74c7ec",
    success="#a6e3a1",
    warning="#f9e2af",
    error="#f38ba8",
    muted="#6c7086",
    border="#45475a",
    selection="#313244",
    status_running="#a6e3a1",
    status_done="#a6e3a1",
    status_failed="#f38ba8",
    status_pending="#cdd6f4",
)

LIGHT_THEME = ThemeColors(
    background="#eff1f5",
    foreground="#4c4f69",
    primary="#1e66f5",
    secondary="#209fb5",
    success="#40a02b",
    warning="#df8e1d",
    error="#d20f39",
    muted="#9ca0b0",
    border="#bcc0cc",
    selection="#ccd0da",
    status_running="#40a02b",
    status_done="#40a02b",
    status_failed="#d20f39",
    status_pending="#4c4f69",
)

HIGH_CONTRAST_THEME = ThemeColors(
    background="#000000",
    foreground="#ffffff",
    primary="#00ffff",
    secondary="#00ff00",
    success="#00ff00",
    warning="#ffff00",
    error="#ff0000",
    muted="#808080",
    border="#ffffff",
    selection="#333333",
    status_running="#00ff00",
    status_done="#00ff00",
    status_failed="#ff0000",
    status_pending="#ffffff",
)


THEMES: dict[ThemeMode, ThemeColors] = {
    ThemeMode.DARK: DARK_THEME,
    ThemeMode.LIGHT: LIGHT_THEME,
    ThemeMode.HIGH_CONTRAST: HIGH_CONTRAST_THEME,
}


def detect_terminal_theme() -> ThemeMode:
    """Auto-detect whether the terminal uses a dark or light theme.

    Detection strategy:
    1. BERNSTEIN_THEME env var (explicit override)
    2. COLORFGBG env var (set by some terminals)
    3. Default to dark (most terminals are dark-themed)

    Returns:
        Detected ThemeMode.
    """
    # Explicit override
    explicit = os.environ.get("BERNSTEIN_THEME", "").lower()
    if explicit == "light":
        return ThemeMode.LIGHT
    if explicit == "dark":
        return ThemeMode.DARK
    if explicit in ("high_contrast", "highcontrast", "hc"):
        return ThemeMode.HIGH_CONTRAST

    # COLORFGBG: "fg;bg" — if bg is a bright value, it's a light terminal
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        parts = colorfgbg.split(";")
        if len(parts) >= 2:
            try:
                bg = int(parts[-1])
                # Standard terminal colors: 0-6 dark, 7+ light
                if bg >= 7:
                    return ThemeMode.LIGHT
                return ThemeMode.DARK
            except ValueError:
                pass

    # Default to dark
    return ThemeMode.DARK


def get_theme(mode: ThemeMode | None = None) -> ThemeColors:
    """Get the theme colors for the specified mode.

    Args:
        mode: Theme mode. If None or AUTO, auto-detects.

    Returns:
        ThemeColors for the selected theme.
    """
    if mode is None or mode == ThemeMode.AUTO:
        mode = detect_terminal_theme()
    return THEMES.get(mode, DARK_THEME)


def cycle_theme(current: ThemeMode) -> ThemeMode:
    """Cycle to the next theme mode.

    Order: DARK -> LIGHT -> HIGH_CONTRAST -> DARK

    Args:
        current: Current theme mode.

    Returns:
        Next theme mode.
    """
    cycle_order = [ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.HIGH_CONTRAST]
    if current == ThemeMode.AUTO:
        current = detect_terminal_theme()
    try:
        idx = cycle_order.index(current)
        return cycle_order[(idx + 1) % len(cycle_order)]
    except ValueError:
        return ThemeMode.DARK


def generate_theme_css(theme: ThemeColors) -> str:
    """Generate Textual CSS variables from a theme.

    Args:
        theme: ThemeColors to convert.

    Returns:
        CSS string with variable definitions.
    """
    return f"""
    Screen {{
        background: {theme.background};
        color: {theme.foreground};
    }}

    #top-bar {{
        background: {theme.border};
        color: {theme.foreground};
    }}

    #shortcuts-footer {{
        background: {theme.border};
        color: {theme.muted};
    }}

    #agent-log {{
        border-top: solid {theme.border};
    }}

    .status-open {{ color: {theme.status_pending}; }}
    .status-claimed {{ color: {theme.secondary}; }}
    .status-in-progress {{ color: {theme.warning}; }}
    .status-done {{ color: {theme.status_done}; }}
    .status-failed {{ color: {theme.status_failed}; }}
    .status-blocked {{ color: {theme.muted}; }}
    .status-cancelled {{ color: {theme.muted}; }}
    """


def theme_color(theme: ThemeColors, role: str) -> str:
    """Get a specific color from the theme by role name.

    Args:
        theme: Current theme.
        role: Color role (e.g. "success", "error", "primary").

    Returns:
        Color string. Falls back to foreground color if role unknown.
    """
    mapping: dict[str, str] = {
        "background": theme.background,
        "foreground": theme.foreground,
        "primary": theme.primary,
        "secondary": theme.secondary,
        "success": theme.success,
        "warning": theme.warning,
        "error": theme.error,
        "muted": theme.muted,
        "border": theme.border,
        "selection": theme.selection,
    }
    return mapping.get(role, theme.foreground)
