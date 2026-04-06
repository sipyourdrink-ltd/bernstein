"""TUI-004: Configurable keybinding system with YAML-based key map.

Extends the existing keybindings module to support loading key maps from
bernstein.yaml configuration files. Users can define custom shortcuts in
their project or global bernstein.yaml under a ``keybindings`` section.

Example bernstein.yaml::

    keybindings:
      quit: "Q"
      refresh: "F5"
      toggle_split_pane: "ctrl+l"
      copy_task_id: "ctrl+y"
      command_palette: "ctrl+p"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from bernstein.keybindings import (
    DEFAULT_BINDINGS,
    RESERVED_KEYS,
    KeyAction,
    apply_overrides,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyMapEntry:
    """A resolved key map entry with source tracking.

    Attributes:
        action: The action name (e.g. "quit", "toggle_split_pane").
        key: The key combination (e.g. "q", "ctrl+p").
        description: Human-readable description.
        source: Where the binding was defined ("default", "yaml", "json").
        show: Whether to show in the footer bar.
        priority: Whether this is a priority binding.
    """

    action: str
    key: str
    description: str
    source: str = "default"
    show: bool = False
    priority: bool = False


# Additional default bindings for new TUI features (TUI-004 through TUI-013)
EXTENDED_BINDINGS: list[KeyAction] = [
    KeyAction("ctrl+y", "copy_to_clipboard", "Copy to clipboard", show=False),
    KeyAction("ctrl+l", "toggle_split_pane", "Split pane", show=True),
    KeyAction("ctrl+p", "command_palette", "Command palette", show=True),
    KeyAction("ctrl+t", "cycle_theme", "Cycle theme", show=False),
    KeyAction("ctrl+a", "toggle_accessibility", "Accessibility mode", show=False),
]


def load_yaml_keybindings(yaml_path: Path | None = None) -> dict[str, str]:
    """Load keybinding overrides from a bernstein.yaml file.

    Looks for a ``keybindings`` section in the YAML file. Each key
    is an action name, each value is a key combination string.

    Args:
        yaml_path: Path to bernstein.yaml. If None, searches for
            ./bernstein.yaml and ~/.bernstein/bernstein.yaml.

    Returns:
        Dict mapping action name to key combination. Empty dict if
        no file found or no keybindings section present.
    """
    if yaml_path is None:
        candidates = [
            Path.cwd() / "bernstein.yaml",
            Path.home() / ".bernstein" / "bernstein.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                yaml_path = candidate
                break

    if yaml_path is None or not yaml_path.exists():
        return {}

    try:
        import yaml

        content = yaml_path.read_text(encoding="utf-8")
        data: Any = yaml.safe_load(content)
        if not isinstance(data, dict):
            return {}
        typed_data: dict[str, Any] = cast("dict[str, Any]", data)
        keybindings_raw: object = typed_data.get("keybindings")
        if not isinstance(keybindings_raw, dict):
            return {}
        typed_bindings: dict[str, object] = cast("dict[str, object]", keybindings_raw)
        result: dict[str, str] = {}
        for action, key in typed_bindings.items():
            action_str = str(action)
            key_str = str(key)
            normalized = key_str.lower().strip()
            if normalized in RESERVED_KEYS:
                logger.warning(
                    "YAML keybinding '%s' for action '%s' uses reserved key, skipping",
                    key_str,
                    action_str,
                )
                continue
            result[action_str] = key_str
        return result
    except ImportError:
        logger.debug("PyYAML not available, skipping YAML keybinding config")
        return {}
    except Exception as exc:
        logger.warning("Failed to load keybindings from %s: %s", yaml_path, exc)
        return {}


def resolve_all_bindings(
    yaml_path: Path | None = None,
    json_path: Path | None = None,
) -> list[KeyMapEntry]:
    """Resolve the complete keybinding map from all sources.

    Priority order (highest wins):
    1. JSON overrides (~/.bernstein/keybindings.json)
    2. YAML config (bernstein.yaml keybindings section)
    3. Default bindings

    Args:
        yaml_path: Path to bernstein.yaml, or None for auto-discovery.
        json_path: Path to keybindings.json, or None for default path.

    Returns:
        List of KeyMapEntry with source tracking.
    """
    from bernstein.keybindings import load_user_overrides

    # Start with defaults + extended
    all_defaults = DEFAULT_BINDINGS + EXTENDED_BINDINGS

    # Apply YAML overrides
    yaml_overrides = load_yaml_keybindings(yaml_path)
    after_yaml = apply_overrides(all_defaults, yaml_overrides)

    # Apply JSON overrides (highest priority)
    json_overrides = load_user_overrides(json_path)
    after_json = apply_overrides(after_yaml, json_overrides)

    # Build entries with source tracking
    entries: list[KeyMapEntry] = []
    for action in after_json:
        if action.action in json_overrides:
            source = "json"
        elif action.action in yaml_overrides:
            source = "yaml"
        else:
            source = "default"
        entries.append(
            KeyMapEntry(
                action=action.action,
                key=action.key,
                description=action.description,
                source=source,
                show=action.show,
                priority=action.priority,
            )
        )
    return entries


def get_key_for_action(
    action: str,
    entries: list[KeyMapEntry] | None = None,
) -> str | None:
    """Look up the configured key for a given action name.

    Args:
        action: Action name to look up.
        entries: Pre-resolved entries, or None to resolve fresh.

    Returns:
        Key combination string, or None if action not found.
    """
    if entries is None:
        entries = resolve_all_bindings()
    for entry in entries:
        if entry.action == action:
            return entry.key
    return None
