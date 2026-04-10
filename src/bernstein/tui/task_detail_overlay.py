"""TUI-018: Task detail overlay with full context.

Full-screen overlay showing task description, agent assignment,
status, log tail, diff preview, quality gate results, and cost.
Triggered by pressing Enter on a task in the task list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

_MAX_LOG_LINES = 50


@dataclass
class TaskDetail:
    """All data needed to render a task detail overlay.

    Attributes:
        task_id: Unique task identifier.
        title: Task title.
        description: Full task description.
        status: Current task status.
        role: Assigned role.
        agent_id: Assigned agent session ID.
        cost_usd: Cost incurred so far.
        log_tail: Last N lines of agent log.
        diff_preview: Git diff preview string.
        quality_results: Quality gate results mapping.
    """

    task_id: str
    title: str
    description: str
    status: str
    role: str
    agent_id: str | None = None
    cost_usd: float | None = None
    log_tail: list[str] = field(default_factory=list)
    diff_preview: str = ""
    quality_results: dict[str, str] = field(default_factory=dict)


def format_task_detail(detail: TaskDetail) -> str:
    """Format task detail into a display string.

    Args:
        detail: Task detail data.

    Returns:
        Formatted multi-line string for display.
    """
    sections: list[str] = []

    # Header
    sections.append(f"{'=' * 60}")
    sections.append(f"  Task: {detail.task_id}")
    sections.append(f"  Title: {detail.title}")
    sections.append(f"  Status: {detail.status}  |  Role: {detail.role}")
    if detail.agent_id:
        sections.append(f"  Agent: {detail.agent_id}")
    if detail.cost_usd is not None:
        sections.append(f"  Cost: ${detail.cost_usd:.2f}")
    sections.append(f"{'=' * 60}")

    # Description
    if detail.description:
        sections.append("")
        sections.append("--- Description ---")
        sections.append(detail.description)

    # Log tail
    if detail.log_tail:
        sections.append("")
        sections.append("--- Recent Log ---")
        tail = detail.log_tail[-_MAX_LOG_LINES:]
        sections.extend(tail)

    # Diff preview
    if detail.diff_preview:
        sections.append("")
        sections.append("--- Diff Preview ---")
        sections.append(detail.diff_preview)

    # Quality gates
    if detail.quality_results:
        sections.append("")
        sections.append("--- Quality Gates ---")
        for gate, result in detail.quality_results.items():
            icon = "pass" if result == "pass" else "FAIL"
            sections.append(f"  [{icon}] {gate}")

    return "\n".join(sections)


class TaskDetailScreen(ModalScreen[None]):
    """Full-screen modal overlay for task detail view."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TaskDetailScreen {
        align: center middle;
    }
    TaskDetailScreen > Static {
        width: 90%;
        height: 90%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
        overflow-y: auto;
    }
    """

    def __init__(self, detail: TaskDetail) -> None:
        """Initialize with task detail data.

        Args:
            detail: Task detail to display.
        """
        super().__init__()
        self._detail = detail

    def compose(self) -> ComposeResult:
        """Build the overlay content."""
        yield Static(format_task_detail(self._detail), id="task-detail-content")

    async def action_dismiss(self, result: None = None) -> None:
        """Close the overlay."""
        self.dismiss()
