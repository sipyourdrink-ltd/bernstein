"""Nerd Font icon integration for Bernstein CLI.

Detection: set NERD_FONT=1 or BERNSTEIN_NERD_FONT=1 to enable Nerd Font glyphs.
Falls back to standard Unicode characters otherwise.
On Windows with cp1252 encoding, falls back to ASCII-safe characters.

Usage::

    from bernstein.cli.icons import get_icons, get_agent_icon, get_status_icon

    icons = get_icons()
    print(icons.status_done)          # ✓ or nf-fa-check_circle glyph
    print(get_agent_icon("claude"))   # brain glyph or fallback
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class NerdFontIcons:
    """Icon set using Nerd Font glyphs (10K+ extra glyphs).

    Requires a patched Nerd Font to render correctly.
    Enable via: NERD_FONT=1 or BERNSTEIN_NERD_FONT=1
    """

    # Agent icons
    agent_claude: str = "\uf5dc"  # nf-md-brain
    agent_codex: str = "\uf872"  # nf-md-robot
    agent_gemini: str = "\uf80d"  # nf-md-star_four_points
    agent_cursor: str = "\uf061"  # nf-fa-arrow_right

    # Status icons
    status_running: str = "\uf110"  # nf-fa-spinner
    status_done: str = "\uf058"  # nf-fa-check_circle
    status_failed: str = "\uf057"  # nf-fa-times_circle
    status_blocked: str = "\uf023"  # nf-fa-lock

    # Quality gate icons
    gate_lint: str = "\uf8e3"  # nf-md-broom
    gate_test: str = "\uf0c3"  # nf-md-flask
    gate_security: str = "\uf9be"  # nf-md-shield_check

    # Common symbols
    arrow_right: str = "\uf061"  # nf-fa-arrow_right
    arrow_left: str = "\uf060"  # nf-fa-arrow_left
    arrow_up: str = "\uf062"  # nf-fa-arrow_up
    arrow_down: str = "\uf063"  # nf-fa-arrow_down
    check: str = "\uf058"  # nf-fa-check_circle
    cross: str = "\uf057"  # nf-fa-times_circle
    warning: str = "\uf071"  # nf-fa-exclamation_triangle
    bullet: str = "\uf111"  # nf-fa-circle
    em_dash: str = "\u2014"  # —
    plus_minus: str = "\u00b1"  # ±


@dataclass(frozen=True)
class UnicodeFallbackIcons:
    """Icon set using standard Unicode characters (works in any terminal)."""

    # Agent icons
    agent_claude: str = "\u25c9"  # ◉  (fisheye)
    agent_codex: str = "\u25ce"  # ◎  (bullseye)
    agent_gemini: str = "\u2726"  # ✦  (four-pointed star)
    agent_cursor: str = "\u2192"  # →  (right arrow)

    # Status icons
    status_running: str = "\u25c9"  # ◉
    status_done: str = "\u2713"  # ✓
    status_failed: str = "\u2717"  # ✗
    status_blocked: str = "\u2298"  # ⊘

    # Quality gate icons
    gate_lint: str = "\u21ba"  # ↺  (counterclockwise arrow)
    gate_test: str = "\u2697"  # ⚗  (alembic / flask)
    gate_security: str = "\u25c8"  # ◈  (diamond with dot)

    # Common symbols
    arrow_right: str = "\u2192"  # →
    arrow_left: str = "\u2190"  # ←
    arrow_up: str = "\u2191"  # ↑
    arrow_down: str = "\u2193"  # ↓
    check: str = "\u2713"  # ✓
    cross: str = "\u2717"  # ✗
    warning: str = "\u26a0"  # ⚠
    bullet: str = "\u2022"  # •
    em_dash: str = "\u2014"  # —
    plus_minus: str = "\u00b1"  # ±


@dataclass(frozen=True)
class AsciiSafeIcons:
    """Icon set using ASCII-safe characters for Windows cp1252 compatibility."""

    # Agent icons
    agent_claude: str = "*"
    agent_codex: str = "o"
    agent_gemini: str = "+"
    agent_cursor: str = ">"

    # Status icons
    status_running: str = "*"
    status_done: str = "+"
    status_failed: str = "x"
    status_blocked: str = "#"

    # Quality gate icons
    gate_lint: str = "~"
    gate_test: str = "T"
    gate_security: str = "S"

    # Common symbols
    arrow_right: str = "->"
    arrow_left: str = "<-"
    arrow_up: str = "^"
    arrow_down: str = "v"
    check: str = "+"
    cross: str = "x"
    warning: str = "!"
    bullet: str = "*"
    em_dash: str = "-"
    plus_minus: str = "+/-"


def _is_truthy(value: str) -> bool:
    """Return True for opt-in values like '1', 'true', 'yes'."""
    return value.lower() in ("1", "true", "yes", "on")


def _needs_ascii_fallback() -> bool:
    """Check if we need ASCII-safe characters (Windows with legacy encoding)."""
    if sys.platform != "win32":
        return False

    # Check if UTF-8 mode is enabled
    if os.environ.get("PYTHONUTF8", "") == "1":
        return False

    # Check stdout encoding
    try:
        encoding = sys.stdout.encoding
        if encoding and encoding.lower() in ("utf-8", "utf8"):
            return False
    except Exception:
        pass

    # Default to ASCII on Windows to be safe
    return True


def get_icons() -> NerdFontIcons | UnicodeFallbackIcons | AsciiSafeIcons:
    """Return the appropriate icon set based on environment detection.

    Priority:
    1. NERD_FONT=1 or BERNSTEIN_NERD_FONT=1 -> NerdFontIcons
    2. Windows with cp1252/legacy encoding -> AsciiSafeIcons
    3. Otherwise -> UnicodeFallbackIcons
    """
    nerd_font_env = os.environ.get("NERD_FONT", "")
    bernstein_nf_env = os.environ.get("BERNSTEIN_NERD_FONT", "")

    if _is_truthy(nerd_font_env) or _is_truthy(bernstein_nf_env):
        return NerdFontIcons()

    if _needs_ascii_fallback():
        return AsciiSafeIcons()

    return UnicodeFallbackIcons()


# Mapping from adapter name → icon attribute name
_AGENT_ICON_MAP: dict[str, str] = {
    "claude": "agent_claude",
    "codex": "agent_codex",
    "gemini": "agent_gemini",
    "cursor": "agent_cursor",
}

# Mapping from task/agent status → icon attribute name
_STATUS_ICON_MAP: dict[str, str] = {
    "running": "status_running",
    "working": "status_running",
    "starting": "status_running",
    "in_progress": "status_running",
    "done": "status_done",
    "completed": "status_done",
    "failed": "status_failed",
    "error": "status_failed",
    "blocked": "status_blocked",
    "pending_approval": "status_blocked",
}


def get_agent_icon(adapter_name: str) -> str:
    """Return the icon for a given adapter/agent name.

    Falls back to the running status icon for unknown adapters.
    """
    icons = get_icons()
    attr = _AGENT_ICON_MAP.get(adapter_name.lower())
    if attr is not None:
        return str(getattr(icons, attr))
    return icons.status_running


def get_status_icon(status: str) -> str:
    """Return the icon for a given task/agent status.

    Falls back to the running icon for unknown statuses.
    """
    icons = get_icons()
    attr = _STATUS_ICON_MAP.get(status.lower())
    if attr is not None:
        return str(getattr(icons, attr))
    return icons.status_running
