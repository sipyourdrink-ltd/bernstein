"""Compact runtime and worktree health pane for the Bernstein TUI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.widgets import Static

from bernstein.core.worktrees.classifier import (
    ClassifiedWorktree,
    WorktreeState,
    classify_worktrees,
    format_size,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from collections.abc import Iterable
    from pathlib import Path


@dataclass(frozen=True)
class WorktreeStatus:
    """Git worktree status snapshot."""

    branch: str
    is_dirty: bool = False
    ahead: int = 0
    behind: int = 0


def format_worktree_display(status: WorktreeStatus) -> str:
    """Format worktree status for display."""
    parts = [status.branch]
    if status.is_dirty:
        parts.append("[dirty]")
    else:
        parts.append("[clean]")
    if status.ahead:
        parts.append(f"{status.ahead}\u2191")
    if status.behind:
        parts.append(f"{status.behind}\u2193")
    return " ".join(parts)


def get_worktree_status(workdir: Path) -> WorktreeStatus | None:
    """Get git worktree status for a directory."""
    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if branch_result.returncode != 0:
            return None
        branch = branch_result.stdout.strip()

        dirty_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if dirty_result.returncode != 0:
            return None
        is_dirty = bool(dirty_result.stdout.strip())

        return WorktreeStatus(branch=branch, is_dirty=is_dirty)
    except (subprocess.TimeoutExpired, OSError):
        return None


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


# ---------------------------------------------------------------------------
# Worktree list pane (feat-worktree-gc-ui)
# ---------------------------------------------------------------------------


_STATE_COLOURS: dict[WorktreeState, str] = {
    WorktreeState.ACTIVE: "green",
    WorktreeState.ORPHAN: "yellow",
    WorktreeState.STALE: "red",
    WorktreeState.CORRUPT: "magenta",
}

#: Default refresh cadence for the worktree list pane.
WORKTREE_LIST_REFRESH_S: float = 10.0


def count_reapable(rows: Iterable[ClassifiedWorktree]) -> int:
    """Return how many rows are safe to reap.

    Surfaced in the TUI status bar so operators see clutter even when
    the list pane is collapsed.
    """
    return sum(1 for row in rows if row.is_reapable)


def render_worktree_list(rows: Iterable[ClassifiedWorktree]) -> Text:
    """Render the worktree inventory for the side pane.

    The output is deliberately compact (one row per worktree) so it fits
    in the narrow side column. Reapable rows are highlighted.
    """
    text = Text()
    rows_list = list(rows)
    text.append("Worktrees\n", style="bold")
    if not rows_list:
        text.append("(none)", style="dim")
        return text

    for row in rows_list:
        colour = _STATE_COLOURS.get(row.state, "white")
        text.append("• ", style="dim")
        text.append(f"{row.session_id[:18]:<18} ")
        text.append(f"{row.state.value:<7}", style=colour)
        text.append(f"  {format_size(row.size_bytes)}\n", style="dim")

    reapable = count_reapable(rows_list)
    if reapable:
        text.append(f"{reapable} reapable", style="yellow bold")
    else:
        text.append("clean", style="green")
    return text


class WorktreeListPanel(Static):
    """Side pane that lists every worktree with its classification.

    The panel does not poll the filesystem on its own; the dashboard
    refresh loop calls :meth:`refresh_from_repo` every
    :data:`WORKTREE_LIST_REFRESH_S` seconds. Tests can call
    :meth:`set_rows` directly to bypass disk I/O.
    """

    DEFAULT_CSS = """
    WorktreeListPanel {
        height: auto;
        min-height: 6;
        border: round $accent 20%;
        padding: 1 1;
        background: $surface-darken-1;
    }
    """

    def __init__(self, repo_root: Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rows: list[ClassifiedWorktree] = []
        self._repo_root = repo_root

    def set_repo_root(self, repo_root: Path | None) -> None:
        """Update the repository root used by :meth:`refresh_from_repo`."""
        self._repo_root = repo_root

    def set_rows(self, rows: Iterable[ClassifiedWorktree]) -> None:
        """Replace the rendered rows."""
        self._rows = list(rows)
        self.refresh()

    def reapable_count(self) -> int:
        """Number of currently-known reapable worktrees."""
        return count_reapable(self._rows)

    def refresh_from_repo(self) -> None:
        """Re-classify all worktrees under the configured repo root."""
        if self._repo_root is None:
            return
        self.set_rows(classify_worktrees(self._repo_root))

    def render(self) -> Text:
        """Render the current worktree rows."""
        return render_worktree_list(self._rows)
