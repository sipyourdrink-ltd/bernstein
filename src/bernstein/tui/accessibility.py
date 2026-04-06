"""TUI-013: Accessibility mode for the Bernstein TUI.

Provides screen reader friendly output, high contrast colors,
and options to disable animations. When accessibility mode is
enabled, unicode indicators are replaced with text labels and
all output is simplified for assistive technology compatibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from rich.text import Text


class AccessibilityLevel(Enum):
    """Accessibility support levels."""

    OFF = "off"
    BASIC = "basic"  # Text labels, no unicode indicators
    FULL = "full"  # Text labels, high contrast, no animations


@dataclass(frozen=True)
class AccessibilityConfig:
    """Configuration for accessibility features.

    Attributes:
        level: Accessibility support level.
        screen_reader: Enable screen reader optimizations.
        high_contrast: Force high contrast colors.
        no_animations: Disable all animations and transitions.
        no_unicode: Replace unicode symbols with text equivalents.
        verbose_labels: Use verbose text labels instead of icons.
        announce_changes: Announce status changes via ARIA-like text.
    """

    level: AccessibilityLevel = AccessibilityLevel.OFF
    screen_reader: bool = False
    high_contrast: bool = False
    no_animations: bool = False
    no_unicode: bool = False
    verbose_labels: bool = False
    announce_changes: bool = False

    @classmethod
    def from_level(cls, level: AccessibilityLevel) -> AccessibilityConfig:
        """Create config from an accessibility level.

        Args:
            level: The accessibility level.

        Returns:
            Configured AccessibilityConfig.
        """
        if level == AccessibilityLevel.OFF:
            return cls(level=level)
        if level == AccessibilityLevel.BASIC:
            return cls(
                level=level,
                no_unicode=True,
                verbose_labels=True,
            )
        # FULL
        return cls(
            level=level,
            screen_reader=True,
            high_contrast=True,
            no_animations=True,
            no_unicode=True,
            verbose_labels=True,
            announce_changes=True,
        )


def detect_accessibility() -> AccessibilityLevel:
    """Auto-detect if accessibility features should be enabled.

    Checks:
    1. BERNSTEIN_ACCESSIBILITY env var
    2. Common screen reader environment variables
    3. Reduced motion preferences

    Returns:
        Detected AccessibilityLevel.
    """
    # Explicit override
    explicit = os.environ.get("BERNSTEIN_ACCESSIBILITY", "").lower()
    if explicit in ("full", "on", "1", "true"):
        return AccessibilityLevel.FULL
    if explicit == "basic":
        return AccessibilityLevel.BASIC
    if explicit in ("off", "0", "false", "none"):
        return AccessibilityLevel.OFF

    # Screen reader detection
    if os.environ.get("ORCA_RUNNING"):
        return AccessibilityLevel.FULL
    if os.environ.get("NVDA_RUNNING"):
        return AccessibilityLevel.FULL
    if os.environ.get("JAWS_RUNNING"):
        return AccessibilityLevel.FULL

    # macOS VoiceOver (may set accessibility env vars)
    if os.environ.get("VOICEOVER_RUNNING"):
        return AccessibilityLevel.FULL

    # Reduced motion preference
    if os.environ.get("REDUCE_MOTION", "").lower() in ("1", "true"):
        return AccessibilityLevel.BASIC

    return AccessibilityLevel.OFF


# Text replacements for unicode symbols in accessibility mode
_UNICODE_REPLACEMENTS: dict[str, str] = {
    "\u25cf": "[*]",  # Filled circle -> [*]
    "\u25cb": "[ ]",  # Empty circle -> [ ]
    "\u25d4": "[~]",  # Quarter circle -> [~]
    "\u25d0": "[/]",  # Half circle -> [/]
    "\u25cc": "[.]",  # Dotted circle -> [.]
    "\u2713": "[OK]",  # Check mark -> [OK]
    "\u2717": "[X]",  # X mark -> [X]
    "\u26a0": "[!]",  # Warning -> [!]
    "\u2139": "[i]",  # Info -> [i]
    "\u2588": "#",  # Full block -> #
    "\u2591": "-",  # Light shade -> -
    "\u2592": "=",  # Medium shade -> =
    "\u2593": "=",  # Dark shade -> =
    "\u25b8": ">",  # Right triangle -> >
    "\u2500": "-",  # Horizontal line -> -
    "\u21c9": "=>",  # Double arrow -> =>
    "\u2026": "...",  # Ellipsis -> ...
    "\u26a1": "[Z]",  # Lightning -> [Z]
}

# Sparkline replacements (block chars -> simple ASCII scale)
_SPARKLINE_REPLACEMENTS: dict[str, str] = {
    "\u2581": "_",
    "\u2582": ".",
    "\u2583": "-",
    "\u2584": "=",
    "\u2585": "+",
    "\u2586": "#",
    "\u2587": "#",
    "\u2588": "#",
}


def replace_unicode(text: str, config: AccessibilityConfig | None = None) -> str:
    """Replace unicode symbols with accessible text equivalents.

    Args:
        text: Text with unicode symbols.
        config: Accessibility config. If None, no replacements made.

    Returns:
        Text with accessible replacements.
    """
    if config is None or not config.no_unicode:
        return text
    result = text
    for unicode_char, replacement in _UNICODE_REPLACEMENTS.items():
        result = result.replace(unicode_char, replacement)
    for unicode_char, replacement in _SPARKLINE_REPLACEMENTS.items():
        result = result.replace(unicode_char, replacement)
    return result


def accessible_status_label(status: str, config: AccessibilityConfig | None = None) -> str:
    """Return an accessible text label for a task status.

    Args:
        status: Status string (e.g. "in_progress", "done").
        config: Accessibility config.

    Returns:
        Text label suitable for screen readers.
    """
    labels: dict[str, str] = {
        "open": "OPEN",
        "claimed": "CLAIMED",
        "in_progress": "IN PROGRESS",
        "done": "DONE",
        "failed": "FAILED",
        "blocked": "BLOCKED",
        "cancelled": "CANCELLED",
        "spawning": "SPAWNING",
        "running": "RUNNING",
        "stalled": "STALLED",
        "dead": "DEAD",
    }
    if config and config.verbose_labels:
        return labels.get(status.lower(), status.upper())
    return status


def render_accessible_progress(
    percentage: float,
    *,
    width: int = 20,
    config: AccessibilityConfig | None = None,
) -> Text:
    """Render an accessible progress bar.

    Args:
        percentage: Completion percentage (0-100).
        width: Bar width.
        config: Accessibility config.

    Returns:
        Rich Text with accessible progress display.
    """
    pct = max(0.0, min(100.0, percentage))
    text = Text()

    if config and config.no_unicode:
        filled = int((pct / 100.0) * width)
        empty = width - filled
        text.append("[" + "#" * filled + "-" * empty + "]")
        text.append(f" {int(pct)}%")
    else:
        filled = int((pct / 100.0) * width)
        empty = width - filled
        if pct >= 100.0:
            color = "green"
        elif pct >= 60.0:
            color = "cyan"
        elif pct >= 30.0:
            color = "yellow"
        else:
            color = "dim"
        text.append("\u2588" * filled, style=color)
        text.append("\u2591" * empty, style="dim")
        text.append(f" {int(pct)}%", style=color)

    return text


def make_announcement(message: str, config: AccessibilityConfig | None = None) -> str | None:
    """Create a screen reader announcement string.

    Only produces output when accessibility config has announce_changes enabled.

    Args:
        message: The announcement message.
        config: Accessibility config.

    Returns:
        Announcement string, or None if announcements are disabled.
    """
    if config and config.announce_changes:
        return f"[Announcement] {message}"
    return None


def accessible_keybinding_label(key: str, config: AccessibilityConfig | None = None) -> str:
    """Format a keybinding for accessible display.

    Args:
        key: Key combination (e.g. "ctrl+p", "q").
        config: Accessibility config.

    Returns:
        Accessible key label (e.g. "Control+P", "Q key").
    """
    if config and config.verbose_labels:
        result = key.replace("ctrl+", "Control+").replace("alt+", "Alt+").replace("shift+", "Shift+")
        if len(result) == 1:
            result = f"{result.upper()} key"
        return result
    return key
