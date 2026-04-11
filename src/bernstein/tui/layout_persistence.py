"""Persistent layout customization and presets for the Bernstein TUI."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_LAYOUT_PATH = Path.home() / ".bernstein" / "tui_layout.yaml"
_REQUIRED_PANELS: frozenset[str] = frozenset({"task-list", "task-search"})
_DEFAULT_PANELS: frozenset[str] = frozenset(
    {"task-list", "task-search", "task-context", "runtime-health", "notification-center", "agent-log"}
)
_VALID_PRESETS: frozenset[str] = frozenset({"focus", "balanced", "observability"})

_PRESET_PANELS: dict[str, frozenset[str]] = {
    "focus": frozenset({"task-list", "task-search", "task-context", "runtime-health", "agent-log"}),
    "balanced": frozenset(
        {
            "task-list",
            "task-search",
            "task-context",
            "runtime-health",
            "notification-center",
            "agent-log",
            "task-timeline",
        }
    ),
    "observability": frozenset(
        {
            "task-list",
            "task-search",
            "task-context",
            "runtime-health",
            "notification-center",
            "agent-log",
            "task-timeline",
            "waterfall-view",
            "coordinator-dashboard",
            "approval-panel",
            "tool-observer",
        }
    ),
}

_PRESET_SPLIT_RATIO: dict[str, float] = {
    "focus": 0.72,
    "balanced": 0.64,
    "observability": 0.58,
}


@dataclass(frozen=True)
class LayoutConfig:
    """Immutable layout configuration for the TUI."""

    split_ratio: float = _PRESET_SPLIT_RATIO["balanced"]
    split_enabled: bool = True
    visible_panels: frozenset[str] = _DEFAULT_PANELS
    orientation: str = "horizontal"
    preset: str = "balanced"

    def toggle_panel(self, panel_id: str) -> LayoutConfig:
        """Return a new config with the given panel toggled on or off."""
        if panel_id in self.visible_panels:
            if panel_id in _REQUIRED_PANELS:
                return self
            return replace(self, visible_panels=self.visible_panels - {panel_id})
        return replace(self, visible_panels=self.visible_panels | {panel_id})

    def apply_preset(self, preset: str) -> LayoutConfig:
        """Return a new config seeded from a named layout preset."""
        preset_name = preset if preset in _VALID_PRESETS else "balanced"
        return replace(
            self,
            preset=preset_name,
            split_enabled=True,
            split_ratio=_PRESET_SPLIT_RATIO[preset_name],
            visible_panels=_PRESET_PANELS[preset_name] | _REQUIRED_PANELS,
        )


def _clamp_ratio(value: object) -> float:
    """Coerce *value* to a float clamped to [0.2, 0.8]."""
    try:
        ratio = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _PRESET_SPLIT_RATIO["balanced"]
    return max(0.2, min(0.8, ratio))


def _coerce_orientation(value: object) -> str:
    """Normalise orientation to ``horizontal`` or ``vertical``."""
    if isinstance(value, str) and value in ("horizontal", "vertical"):
        return value
    return "horizontal"


def _coerce_preset(value: object) -> str:
    """Normalise preset to one of the supported layout presets."""
    if isinstance(value, str) and value in _VALID_PRESETS:
        return value
    return "balanced"


def preset_layout(preset: str) -> LayoutConfig:
    """Return the default layout config for a named preset."""
    return LayoutConfig().apply_preset(preset)


def load_layout(config_path: Path | None = None) -> LayoutConfig:
    """Load the persisted layout from a YAML file."""
    path = config_path or _LAYOUT_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return preset_layout("balanced")

    if not isinstance(raw, dict):
        return preset_layout("balanced")

    preset = _coerce_preset(raw.get("preset"))
    split_ratio = _clamp_ratio(raw.get("split_ratio", _PRESET_SPLIT_RATIO[preset]))
    split_enabled = bool(raw.get("split_enabled", True))
    orientation = _coerce_orientation(raw.get("orientation"))
    raw_panels = raw.get("visible_panels")
    if isinstance(raw_panels, list) and all(isinstance(panel, str) for panel in raw_panels):
        panels = frozenset(raw_panels) | _REQUIRED_PANELS
    else:
        panels = _PRESET_PANELS[preset] | _REQUIRED_PANELS

    return LayoutConfig(
        split_ratio=split_ratio,
        split_enabled=split_enabled,
        visible_panels=panels,
        orientation=orientation,
        preset=preset,
    )


def save_layout(config: LayoutConfig, config_path: Path | None = None) -> None:
    """Persist the layout configuration to a YAML file."""
    path = config_path or _LAYOUT_PATH
    data: dict[str, object] = {
        "split_ratio": config.split_ratio,
        "split_enabled": config.split_enabled,
        "visible_panels": sorted(config.visible_panels),
        "orientation": config.orientation,
        "preset": config.preset,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save layout config to %s: %s", path, exc)
