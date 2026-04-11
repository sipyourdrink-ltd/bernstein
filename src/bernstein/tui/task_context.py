"""Task context panel for the Bernstein TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.widgets import Static


@dataclass(frozen=True)
class TaskContextSummary:
    """Compact summary of the currently selected task."""

    task_id: str
    title: str
    status: str
    role: str
    priority: int
    model: str
    assigned_agent: str
    age_display: str
    elapsed: str
    retry_count: int
    blocked_reason: str
    depends_on_count: int
    owned_files_count: int
    estimated_cost_usd: float
    verification_count: int
    flagged_unverified: bool


def render_task_context(
    summary: TaskContextSummary | None,
    runtime_snapshot: dict[str, Any] | None = None,
) -> Text:
    """Render selected-task context with a compact runtime footer."""

    text = Text()
    if summary is None:
        text.append("Select a task to inspect context.", style="dim")
    else:
        text.append(f"{summary.title}\n", style="bold")
        text.append(f"{summary.task_id}  ", style="cyan")
        text.append(f"{summary.status.upper()}  ", style="yellow")
        text.append(f"P{summary.priority}  ", style="magenta")
        text.append(f"{summary.role}\n", style="green")
        text.append("Agent: ", style="dim")
        text.append((summary.assigned_agent or "unassigned") + "\n")
        text.append("Model: ", style="dim")
        text.append(summary.model + "\n")
        text.append("Age / Time: ", style="dim")
        text.append(f"{summary.age_display} / {summary.elapsed}\n")
        text.append("Retries / Deps / Files: ", style="dim")
        text.append(f"{summary.retry_count} / {summary.depends_on_count} / {summary.owned_files_count}\n")
        text.append("Cost / Verifications: ", style="dim")
        text.append(f"${summary.estimated_cost_usd:.4f} / {summary.verification_count}\n")
        if summary.flagged_unverified:
            text.append("Verification: pending\n", style="red")
        if summary.blocked_reason:
            text.append("Blocked: ", style="dim")
            text.append(summary.blocked_reason + "\n", style="red")

    if runtime_snapshot:
        text.append("\n")
        text.append("Runtime\n", style="bold dim")
        branch = str(runtime_snapshot.get("git_branch", "") or "unknown")
        worktrees = int(runtime_snapshot.get("active_worktrees", 0) or 0)
        memory_mb = float(runtime_snapshot.get("memory_mb", 0.0) or 0.0)
        restart_count = int(runtime_snapshot.get("restart_count", 0) or 0)
        text.append("Branch: ", style="dim")
        text.append(branch + "\n")
        text.append("Worktrees / Restarts: ", style="dim")
        text.append(f"{worktrees} / {restart_count}\n")
        text.append("Memory: ", style="dim")
        text.append(f"{memory_mb:.1f} MB")

    return text


class TaskContextPanel(Static):
    """Panel that shows the selected task summary and runtime health."""

    DEFAULT_CSS = """
    TaskContextPanel {
        height: auto;
        min-height: 11;
        max-height: 16;
        border: round $primary 30%;
        padding: 1 1;
        background: $surface-darken-1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._summary: TaskContextSummary | None = None
        self._runtime_snapshot: dict[str, Any] | None = None

    def set_task(self, summary: TaskContextSummary | None) -> None:
        """Update the currently selected task summary."""
        self._summary = summary
        self.refresh()

    def set_runtime_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Update the runtime snapshot shown in the footer."""
        self._runtime_snapshot = snapshot
        self.refresh()

    def render(self) -> Text:
        """Render the current task context."""
        return render_task_context(self._summary, self._runtime_snapshot)
