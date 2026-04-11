"""Compact runtime and worktree health pane for the Bernstein TUI."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.widgets import Static


def render_runtime_health(snapshot: dict[str, Any] | None) -> Text:
    """Render a compact runtime-health summary for the side pane."""
    text = Text()
    if not snapshot:
        text.append("Runtime health unavailable.", style="dim")
        return text

    branch = str(snapshot.get("git_branch", "") or "unknown")
    worktrees = int(snapshot.get("active_worktrees", 0) or 0)
    restarts = int(snapshot.get("restart_count", 0) or 0)
    memory_mb = float(snapshot.get("memory_mb", 0.0) or 0.0)
    disk_usage_mb = float(snapshot.get("disk_usage_mb", 0.0) or 0.0)
    config_hash = str(snapshot.get("config_hash", "") or "")

    text.append("Runtime Health\n", style="bold")
    text.append("Branch: ", style="dim")
    text.append(branch + "\n")
    text.append("Worktrees / Restarts: ", style="dim")
    text.append(f"{worktrees} / {restarts}\n")
    text.append("Memory / Disk: ", style="dim")
    text.append(f"{memory_mb:.1f} MB / {disk_usage_mb:.1f} MB\n")
    if config_hash:
        text.append("Config: ", style="dim")
        text.append(config_hash[:12], style="cyan")
    return text


class RuntimeHealthPanel(Static):
    """Panel that shows compact runtime and worktree health."""

    DEFAULT_CSS = """
    RuntimeHealthPanel {
        height: auto;
        min-height: 7;
        border: round $accent 20%;
        padding: 1 1;
        background: $surface-darken-1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._snapshot: dict[str, Any] | None = None

    def set_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Update the runtime snapshot rendered by the panel."""
        self._snapshot = snapshot
        self.refresh()

    def render(self) -> Text:
        """Render the current runtime snapshot."""
        return render_runtime_health(self._snapshot)
