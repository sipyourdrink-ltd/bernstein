"""TUI-017: Mouse support for panel interaction.

Enable mouse click to select tasks, scroll with mouse wheel,
and drag to resize panes. Textual supports mouse input natively;
this module provides configuration and event routing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MouseConfig:
    """Mouse interaction configuration.

    Attributes:
        click_to_select: Click on task row to select it.
        scroll_enabled: Mouse wheel scrolling in panels.
        drag_resize: Drag pane borders to resize.
    """

    click_to_select: bool = True
    scroll_enabled: bool = True
    drag_resize: bool = True


def load_mouse_config(yaml_path: Path | None = None) -> MouseConfig:
    """Load mouse configuration from bernstein.yaml.

    Args:
        yaml_path: Path to config file. Searches defaults if None.

    Returns:
        MouseConfig with user preferences or defaults.
    """
    try:
        import yaml
    except ImportError:
        return MouseConfig()

    candidates: list[Path] = []
    if yaml_path:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            mouse = data.get("mouse")
            if not isinstance(mouse, dict):
                continue
            mouse_dict: dict[str, object] = dict(mouse)
            return MouseConfig(
                click_to_select=bool(mouse_dict.get("click_to_select", True)),
                scroll_enabled=bool(mouse_dict.get("scroll_enabled", True)),
                drag_resize=bool(mouse_dict.get("drag_resize", True)),
            )
        except Exception:
            continue
    return MouseConfig()
