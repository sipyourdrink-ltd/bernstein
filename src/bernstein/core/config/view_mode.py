"""Progressive disclosure view modes for the Bernstein dashboard.

Defines three detail levels (novice, standard, expert) that control
which sections appear in ``bernstein status`` and the TUI dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

import yaml


class ViewMode(Enum):
    """Dashboard detail level."""

    NOVICE = "novice"
    STANDARD = "standard"
    EXPERT = "expert"


@dataclass(frozen=True)
class ViewConfig:
    """Feature flags derived from a :class:`ViewMode`.

    Each boolean controls visibility of a dashboard section.
    """

    mode: ViewMode
    show_tokens: bool
    """Show token usage (expert only)."""
    show_cost_per_task: bool
    """Per-task cost breakdown (standard+)."""
    show_model_details: bool
    """Model names and config (expert only)."""
    show_agent_ids: bool
    """Raw session IDs (expert only)."""
    show_quality_gates: bool
    """Quality gate details (standard+)."""
    show_error_traces: bool
    """Full error traces (expert only)."""


_VIEW_CONFIGS: dict[ViewMode, ViewConfig] = {
    ViewMode.NOVICE: ViewConfig(
        mode=ViewMode.NOVICE,
        show_tokens=False,
        show_cost_per_task=False,
        show_model_details=False,
        show_agent_ids=False,
        show_quality_gates=False,
        show_error_traces=False,
    ),
    ViewMode.STANDARD: ViewConfig(
        mode=ViewMode.STANDARD,
        show_tokens=False,
        show_cost_per_task=True,
        show_model_details=False,
        show_agent_ids=False,
        show_quality_gates=True,
        show_error_traces=False,
    ),
    ViewMode.EXPERT: ViewConfig(
        mode=ViewMode.EXPERT,
        show_tokens=True,
        show_cost_per_task=True,
        show_model_details=True,
        show_agent_ids=True,
        show_quality_gates=True,
        show_error_traces=True,
    ),
}


def get_view_config(mode: ViewMode) -> ViewConfig:
    """Return the :class:`ViewConfig` for *mode*.

    Args:
        mode: Desired detail level.

    Returns:
        Frozen dataclass with the correct boolean flags.
    """
    return _VIEW_CONFIGS[mode]


def load_view_mode(workdir: Path) -> ViewMode:
    """Read the persisted view mode from ``.sdd/config.yaml``.

    Falls back to :attr:`ViewMode.STANDARD` when the key is absent or
    the file does not exist.

    Args:
        workdir: Project root containing ``.sdd/``.

    Returns:
        The stored :class:`ViewMode`, or ``STANDARD`` by default.
    """
    config_path = workdir / ".sdd" / "config.yaml"
    if not config_path.exists():
        return ViewMode.STANDARD
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ViewMode.STANDARD
    if not isinstance(data, dict):
        return ViewMode.STANDARD
    raw = cast("dict[str, object]", data).get("view_mode")
    if not isinstance(raw, str):
        return ViewMode.STANDARD
    try:
        return ViewMode(raw.lower())
    except ValueError:
        return ViewMode.STANDARD


def save_view_mode(workdir: Path, mode: ViewMode) -> None:
    """Persist *mode* to ``.sdd/config.yaml`` under the ``view_mode`` key.

    Creates ``.sdd/`` and the YAML file if they don't exist.  Preserves
    any other keys already present in the file.

    Args:
        workdir: Project root containing ``.sdd/``.
        mode: The view mode to store.
    """
    sdd_dir = workdir / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    config_path = sdd_dir / "config.yaml"

    data: dict[str, object] = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = cast("dict[str, object]", loaded)
        except Exception:
            pass

    data["view_mode"] = mode.value
    config_path.write_text(yaml.dump(dict(data), default_flow_style=False), encoding="utf-8")
