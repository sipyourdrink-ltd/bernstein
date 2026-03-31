"""Nerd Font icon integration for Bernstein CLI.

Detection: set NERD_FONT=1 or BERNSTEIN_NERD_FONT=1 to enable Nerd Font glyphs.
Falls back to standard Unicode characters otherwise.

Usage::

    from bernstein.cli.icons import get_icons, get_agent_icon, get_status_icon

    icons = get_icons()
    print(icons.status_done)          # ✓ or nf-fa-check_circle glyph
    print(get_agent_icon("claude"))   # brain glyph or fallback
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NerdFontIcons:
    """Icon set using Nerd Font glyphs (10K+ extra glyphs).

    Requires a patched Nerd Font to render correctly.
    Enable via: NERD_FONT=1 or BERNSTEIN_NERD_FONT=1
    """

    # Agent icons
    agent_claude: str = "\uf5dc"   # nf-md-brain
    agent_codex: str = "\uf872"    # nf-md-robot
    agent_gemini: str = "\uf80d"   # nf-md-star_four_points
    agent_cursor: str = "\uf061"   # nf-fa-arrow_right

    # Status icons
    status_running: str = "\uf110"  # nf-fa-spinner
    status_done: str = "\uf058"     # nf-fa-check_circle
    status_failed: str = "\uf057"   # nf-fa-times_circle
    status_blocked: str = "\uf023"  # nf-fa-lock

    # Quality gate icons
    gate_lint: str = "\uf8e3"   # nf-md-broom
    gate_test: str = "\uf0c3"   # nf-md-flask
    gate_security: str = "\uf9be"  # nf-md-shield_check


@dataclass(frozen=True)
class UnicodeFallbackIcons:
    """Icon set using standard Unicode characters (works in any terminal)."""

    # Agent icons
    agent_claude: str = "\u25c9"   # ◉  (fisheye)
    agent_codex: str = "\u25ce"    # ◎  (bullseye)
    agent_gemini: str = "\u2726"   # ✦  (four-pointed star)
    agent_cursor: str = "\u2192"   # →  (right arrow)

    # Status icons
    status_running: str = "\u25c9"  # ◉
    status_done: str = "\u2713"     # ✓
    status_failed: str = "\u2717"   # ✗
    status_blocked: str = "\u2298"  # ⊘

    # Quality gate icons
    gate_lint: str = "\u21ba"    # ↺  (counterclockwise arrow)
    gate_test: str = "\u2697"    # ⚗  (alembic / flask)
    gate_security: str = "\u25c8"  # ◈  (diamond with dot)


def _is_truthy(value: str) -> bool:
    """Return True for opt-in values like '1', 'true', 'yes'."""
    return value.lower() in ("1", "true", "yes", "on")


def get_icons() -> NerdFontIcons | UnicodeFallbackIcons:
    """Return the appropriate icon set based on environment detection.

    Checks NERD_FONT and BERNSTEIN_NERD_FONT env vars.
    Any value treated as truthy (1, true, yes, on) activates Nerd Font mode.
    """
    nerd_font_env = os.environ.get("NERD_FONT", "")
    bernstein_nf_env = os.environ.get("BERNSTEIN_NERD_FONT", "")

    if _is_truthy(nerd_font_env) or _is_truthy(bernstein_nf_env):
        return NerdFontIcons()
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
