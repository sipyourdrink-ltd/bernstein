"""TUI-008: Split-pane view (tasks list + live agent log).

Provides a split-pane layout that shows the task list on the left
and live agent log on the right (or top/bottom). Toggle with a
configurable keybinding (default: Ctrl+L).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.text import Text
from textual.widgets import Static


class SplitOrientation(Enum):
    """Split pane orientation."""

    HORIZONTAL = "horizontal"  # side by side
    VERTICAL = "vertical"  # top/bottom


@dataclass(frozen=True)
class SplitPaneConfig:
    """Configuration for split-pane layout.

    Attributes:
        orientation: How to split the pane.
        ratio: Fraction allocated to the primary (left/top) pane. 0.0-1.0.
        enabled: Whether split-pane is active.
    """

    orientation: SplitOrientation = SplitOrientation.HORIZONTAL
    ratio: float = 0.5
    enabled: bool = False


class SplitPaneState:
    """Mutable state for the split-pane view.

    Tracks whether the split is active, the current orientation,
    and provides toggle/cycle methods.
    """

    def __init__(self, config: SplitPaneConfig | None = None) -> None:
        """Initialize split pane state.

        Args:
            config: Initial configuration. Defaults to disabled.
        """
        if config is None:
            config = SplitPaneConfig()
        self._enabled = config.enabled
        self._orientation = config.orientation
        self._ratio = config.ratio

    @property
    def enabled(self) -> bool:
        """Whether split-pane is currently active."""
        return self._enabled

    @property
    def orientation(self) -> SplitOrientation:
        """Current split orientation."""
        return self._orientation

    @property
    def ratio(self) -> float:
        """Current split ratio."""
        return self._ratio

    def toggle(self) -> bool:
        """Toggle split-pane on/off.

        Returns:
            New enabled state.
        """
        self._enabled = not self._enabled
        return self._enabled

    def cycle_orientation(self) -> SplitOrientation:
        """Cycle between horizontal and vertical orientation.

        Returns:
            New orientation.
        """
        if self._orientation == SplitOrientation.HORIZONTAL:
            self._orientation = SplitOrientation.VERTICAL
        else:
            self._orientation = SplitOrientation.HORIZONTAL
        return self._orientation

    def set_ratio(self, ratio: float) -> None:
        """Set the split ratio.

        Args:
            ratio: New ratio (clamped to 0.2-0.8).
        """
        self._ratio = max(0.2, min(0.8, ratio))

    def to_config(self) -> SplitPaneConfig:
        """Export current state as an immutable config.

        Returns:
            SplitPaneConfig snapshot.
        """
        return SplitPaneConfig(
            orientation=self._orientation,
            ratio=self._ratio,
            enabled=self._enabled,
        )


class SplitPaneContainer(Static):
    """A container widget that manages split-pane layout.

    When enabled, displays two child widgets side-by-side or stacked.
    When disabled, shows only the primary widget full-width.
    """

    DEFAULT_CSS = """
    SplitPaneContainer {
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the split pane container.

        Args:
            **kwargs: Forwarded to Static.
        """
        super().__init__(**kwargs)
        self._state = SplitPaneState()

    @property
    def state(self) -> SplitPaneState:
        """Access the split pane state."""
        return self._state

    def render(self) -> Text:
        """Render split pane status indicator.

        Returns:
            Rich Text showing current split state.
        """
        if not self._state.enabled:
            return Text("Split: off", style="dim")
        orient = self._state.orientation.value
        ratio = int(self._state.ratio * 100)
        return Text(f"Split: {orient} ({ratio}%)", style="cyan")


def build_split_layout_css(
    state: SplitPaneState,
    primary_id: str = "primary-pane",
    secondary_id: str = "secondary-pane",
) -> str:
    """Generate CSS rules for the current split-pane configuration.

    Args:
        state: Current split pane state.
        primary_id: CSS id of the primary widget.
        secondary_id: CSS id of the secondary widget.

    Returns:
        CSS string to inject into the app.
    """
    if not state.enabled:
        return f"""
        #{primary_id} {{ width: 100%; height: 100%; }}
        #{secondary_id} {{ display: none; }}
        """

    ratio_pct = int(state.ratio * 100)
    other_pct = 100 - ratio_pct

    if state.orientation == SplitOrientation.HORIZONTAL:
        return f"""
        #{primary_id} {{ width: {ratio_pct}%; height: 100%; }}
        #{secondary_id} {{ width: {other_pct}%; height: 100%; }}
        """
    return f"""
    #{primary_id} {{ width: 100%; height: {ratio_pct}%; }}
    #{secondary_id} {{ width: 100%; height: {other_pct}%; }}
    """


def render_split_status(state: SplitPaneState) -> Text:
    """Render a status indicator for the split-pane state.

    Args:
        state: Current split pane state.

    Returns:
        Rich Text with colored status indicator.
    """
    if not state.enabled:
        return Text("[split: off]", style="dim")
    orient = "H" if state.orientation == SplitOrientation.HORIZONTAL else "V"
    return Text(f"[split: {orient}]", style="cyan")
