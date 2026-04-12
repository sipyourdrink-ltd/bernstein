"""Keybinding system for the Bernstein TUI.

Provides a keybinding manager that supports:
- Default key bindings with action→key mappings
- User overrides from ~/.bernstein/keybindings.json
- Reserved non-rebindable keys (Ctrl+C, Ctrl+D)

The keybindings are resolved at import time by ``get_bindings()``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from textual.binding import Binding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reserved keys — these cannot be overridden by the user
# ---------------------------------------------------------------------------

RESERVED_KEYS: frozenset[str] = frozenset({"ctrl+c", "ctrl+d"})


@dataclass(frozen=True)
class KeyAction:
    """A single key-to-action mapping.

    Attributes:
        key: The key combination (e.g. "q", "ctrl+s", "enter").
        action: The action name (e.g. "quit", "hard_stop").
        description: Human-readable description for help display.
        show: Whether to show in the help footer.
        priority: If True, binding is processed before any others.
    """

    key: str
    action: str
    description: str
    show: bool = False
    priority: bool = False


# ---------------------------------------------------------------------------
# Default bindings
# ---------------------------------------------------------------------------

DEFAULT_BINDINGS: list[KeyAction] = [
    KeyAction("q", "quit", "Quit", show=False),
    KeyAction("r", "refresh", "Refresh", show=False),
    KeyAction("S", "hard_stop", "Hard stop", show=False, priority=True),
    KeyAction("enter", "toggle_action_bar", "Actions", show=False),
    KeyAction("s", "spawn_now", "Spawn now", show=False),
    KeyAction("p", "prioritize", "Prioritize", show=False),
    KeyAction("k", "kill_agent", "Kill agent", show=False),
    KeyAction("x", "cancel_task", "Cancel task", show=False),
    KeyAction("t", "retry_task", "Retry task", show=False),
    KeyAction("v", "toggle_timeline", "Timeline", show=True),
    KeyAction("f", "toggle_waterfall", "Waterfall", show=True),
    KeyAction("c", "toggle_scratchpad", "Scratchpad", show=True),
    KeyAction("w", "toggle_coordinator", "Coordinator", show=True),
    KeyAction("a", "toggle_approvals", "Approvals", show=True),
    KeyAction("l", "toggle_tool_observer", "Tool calls", show=True),
    KeyAction("/", "scratchpad_filter", "Filter", show=False),
    KeyAction("h", "show_help", "Help", show=False),
    KeyAction("escape", "close_action_bar", "Close", show=False),
    KeyAction("up", "cursor_up", "Up", show=False),
    KeyAction("down", "cursor_down", "Down", show=False),
    KeyAction("j", "cursor_down", "Down", show=False),
    KeyAction("?", "show_help", "Help", show=True),
]


# ---------------------------------------------------------------------------
# User overrides
# ---------------------------------------------------------------------------


def _default_keybindings_path() -> Path:
    """Return the path to the user keybindings file.

    Returns:
        Path to ~/.bernstein/keybindings.json
    """
    home = Path.home()
    return home / ".bernstein" / "keybindings.json"


def load_user_overrides(path: Path | None = None) -> dict[str, str]:
    """Load user keybinding overrides from JSON file.

    Args:
        path: Path to the overrides file. If None, uses default path.

    Returns:
        Dict mapping action name → key. Only overrides specified are returned.
    """
    if path is None:
        path = _default_keybindings_path()

    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8")
        data: Any = json.loads(content)
        if not isinstance(data, dict):
            logger.warning("Keybindings file %s is not a JSON object", path)
            return {}
        typed: dict[str, Any] = cast("dict[str, Any]", data)
        return {str(k): str(v) for k, v in typed.items()}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load keybindings from %s: %s", path, exc)
        return {}


def _validate_override(action: str, key: str) -> bool:
    """Check if an override is valid (key not reserved).

    Args:
        action: The action name.
        key: The proposed key binding.

    Returns:
        True if the override is allowed, False if the key is reserved.
    """
    normalized = key.lower().strip()
    if normalized in RESERVED_KEYS:
        logger.warning(
            "Key '%s' for action '%s' is reserved and cannot be overridden",
            key,
            action,
        )
        return False
    return True


def apply_overrides(defaults: list[KeyAction], overrides: dict[str, str]) -> list[KeyAction]:
    """Apply user overrides to default bindings.

    Args:
        defaults: The default key bindings.
        overrides: Dict mapping action name → new key.

    Returns:
        New list of KeyAction with overrides applied.
    """
    result: list[KeyAction] = []
    for action in defaults:
        if action.action in overrides:
            new_key = overrides[action.action]
            if _validate_override(action.action, new_key):
                result.append(
                    KeyAction(
                        key=new_key,
                        action=action.action,
                        description=action.description,
                        show=action.show,
                        priority=action.priority,
                    )
                )
            else:
                result.append(action)
        else:
            result.append(action)
    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_bindings(
    overrides_path: Path | None = None,
) -> list[KeyAction]:
    """Resolve the final keybindings with user overrides applied.

    Args:
        overrides_path: Path to user overrides JSON. None for default path.

    Returns:
        List of resolved KeyAction objects.
    """
    overrides = load_user_overrides(overrides_path)
    return apply_overrides(DEFAULT_BINDINGS, overrides)


def to_textual_bindings(
    actions: list[KeyAction] | None = None,
) -> list[Binding]:
    """Convert KeyAction list to Textual Binding objects.

    Args:
        actions: List of KeyAction objects. If None, uses resolve_bindings().

    Returns:
        List of Textual Binding objects suitable for BINDINGS class variable.
    """
    if actions is None:
        actions = resolve_bindings()

    return [
        Binding(
            action.key,
            action.action,
            action.description,
            show=action.show,
            priority=action.priority,
        )
        for action in actions
    ]


def format_keybindings_help(actions: list[KeyAction] | None = None) -> str:
    """Format keybindings as a help string.

    Args:
        actions: List of KeyAction objects. If None, uses resolve_bindings().

    Returns:
        Formatted help string showing available keybindings.
    """
    if actions is None:
        actions = resolve_bindings()

    lines: list[str] = []
    lines.append("Keyboard shortcuts")
    lines.append("=" * 40)
    for action in actions:
        key_display = action.key.replace("ctrl+", "Ctrl+")
        lines.append(f"  {key_display:15s} {action.description}")

    lines.append("")
    lines.append("Reserved (non-rebindable): Ctrl+C, Ctrl+D")
    return "\n".join(lines)
