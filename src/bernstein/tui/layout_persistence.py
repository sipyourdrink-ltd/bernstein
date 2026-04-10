"""TUI-016: Persistent layout customization.

Provides layout configuration persistence for the Bernstein TUI.
Users can save panel visibility, split ratio, and orientation so
the layout is restored across sessions.  Config is stored as YAML
at ``~/.bernstein/tui_layout.yaml``.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: Default path for persisting the user's layout preferences.
_LAYOUT_PATH = Path.home() / ".bernstein" / "tui_layout.yaml"

#: Panels that are always visible and cannot be hidden.
_REQUIRED_PANELS: frozenset[str] = frozenset({"task-list"})

#: Default set of visible panels.
_DEFAULT_PANELS: frozenset[str] = frozenset(
    {"task-list", "agent-log", "timeline", "status-bar"}
)


@dataclass(frozen=True)
class LayoutConfig:
    """Immutable layout configuration for the TUI.

    Attributes:
        split_ratio: Fraction allocated to the primary pane (0.2 -- 0.8).
        split_enabled: Whether the split-pane view is active.
        visible_panels: Frozenset of panel identifiers that are shown.
        orientation: ``"horizontal"`` or ``"vertical"``.
    """

    split_ratio: float = 0.5
    split_enabled: bool = False
    visible_panels: frozenset[str] = _DEFAULT_PANELS
    orientation: str = "horizontal"

    def toggle_panel(self, panel_id: str) -> LayoutConfig:
        """Return a new config with the given panel toggled on or off.

        The ``task-list`` panel cannot be hidden -- attempts to toggle
        it off are silently ignored.

        Args:
            panel_id: Identifier of the panel to toggle.

        Returns:
            New LayoutConfig with the panel added or removed.
        """
        if panel_id in self.visible_panels:
            if panel_id in _REQUIRED_PANELS:
                return self
            return replace(
                self, visible_panels=self.visible_panels - {panel_id}
            )
        return replace(
            self, visible_panels=self.visible_panels | {panel_id}
        )


def _clamp_ratio(value: object) -> float:
    """Coerce *value* to a float clamped to [0.2, 0.8].

    Args:
        value: Raw value from config.

    Returns:
        Clamped float.
    """
    try:
        ratio = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.2, min(0.8, ratio))


def _coerce_orientation(value: object) -> str:
    """Normalise orientation to ``"horizontal"`` or ``"vertical"``.

    Args:
        value: Raw value from config.

    Returns:
        Validated orientation string.
    """
    if isinstance(value, str) and value in ("horizontal", "vertical"):
        return value
    return "horizontal"


def load_layout(config_path: Path | None = None) -> LayoutConfig:
    """Load the persisted layout from a YAML file.

    If the file is missing, unreadable, or contains invalid data the
    function returns a default ``LayoutConfig`` instead of raising.

    Args:
        config_path: Path to the YAML config file.  Defaults to
            ``~/.bernstein/tui_layout.yaml``.

    Returns:
        The loaded (or default) LayoutConfig.
    """
    path = config_path or _LAYOUT_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return LayoutConfig()

    if not isinstance(raw, dict):
        return LayoutConfig()

    split_ratio = _clamp_ratio(raw.get("split_ratio", 0.5))
    split_enabled = bool(raw.get("split_enabled", False))
    orientation = _coerce_orientation(raw.get("orientation"))

    # Visible panels: accept a list of strings.
    raw_panels = raw.get("visible_panels")
    if isinstance(raw_panels, list) and all(
        isinstance(p, str) for p in raw_panels
    ):
        panels = frozenset(raw_panels) | _REQUIRED_PANELS
    else:
        panels = _DEFAULT_PANELS

    return LayoutConfig(
        split_ratio=split_ratio,
        split_enabled=split_enabled,
        visible_panels=panels,
        orientation=orientation,
    )


def save_layout(
    config: LayoutConfig, config_path: Path | None = None
) -> None:
    """Persist the layout configuration to a YAML file.

    Creates parent directories if they don't exist.  Errors are logged
    rather than raised so that a broken config directory never crashes
    the TUI.

    Args:
        config: LayoutConfig to persist.
        config_path: Path to the YAML config file.  Defaults to
            ``~/.bernstein/tui_layout.yaml``.
    """
    path = config_path or _LAYOUT_PATH
    data: dict[str, object] = {
        "split_ratio": config.split_ratio,
        "split_enabled": config.split_enabled,
        "visible_panels": sorted(config.visible_panels),
        "orientation": config.orientation,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not save layout config to %s: %s", path, exc)
