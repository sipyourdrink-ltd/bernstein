"""TUI-010: Task progress bar with completion percentage.

Renders per-task progress bars showing completion percentage based on
reported progress data (files changed, tests passing, subtasks done).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from rich.text import Text


@dataclass(frozen=True)
class TaskProgress:
    """Progress data for a single task.

    Attributes:
        task_id: Task identifier.
        completed_steps: Number of completed steps/subtasks.
        total_steps: Total number of steps/subtasks.
        files_changed: Number of files modified.
        tests_passing: Number of tests currently passing.
        tests_total: Total number of tests.
        custom_pct: Optional explicit percentage override (0-100).
    """

    task_id: str
    completed_steps: int = 0
    total_steps: int = 0
    files_changed: int = 0
    tests_passing: int = 0
    tests_total: int = 0
    custom_pct: float | None = None

    @property
    def percentage(self) -> float:
        """Compute completion percentage (0.0-100.0).

        Priority: custom_pct > step ratio > test ratio > 0.

        Returns:
            Completion percentage.
        """
        if self.custom_pct is not None:
            return max(0.0, min(100.0, self.custom_pct))
        if self.total_steps > 0:
            return (self.completed_steps / self.total_steps) * 100.0
        if self.tests_total > 0:
            return (self.tests_passing / self.tests_total) * 100.0
        return 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> TaskProgress:
        """Parse progress from a task-server API response.

        Args:
            raw: Dictionary with progress fields.

        Returns:
            TaskProgress instance.
        """
        progress_raw: object = raw.get("progress", {})
        if isinstance(progress_raw, dict):
            progress = cast("dict[str, Any]", progress_raw)
            pct_raw: object = progress.get("percentage")
            custom_pct: float | None = float(cast("float", pct_raw)) if pct_raw is not None else None
            return cls(
                task_id=str(raw.get("id", raw.get("task_id", ""))),
                completed_steps=int(progress.get("completed_steps", 0)),
                total_steps=int(progress.get("total_steps", 0)),
                files_changed=int(progress.get("files_changed", raw.get("files_changed", 0))),
                tests_passing=int(progress.get("tests_passing", raw.get("tests_passing", 0))),
                tests_total=int(progress.get("tests_total", raw.get("tests_total", 0))),
                custom_pct=custom_pct,
            )
        return cls(
            task_id=str(raw.get("id", raw.get("task_id", ""))),
            files_changed=int(raw.get("files_changed", 0)),
            tests_passing=int(raw.get("tests_passing", 0)),
            tests_total=int(raw.get("tests_total", 0)),
        )


# Bar characters for progress rendering
_BAR_FILLED = "\u2588"  # Full block
_BAR_EMPTY = "\u2591"  # Light shade
_BAR_PARTIAL = "\u2592"  # Medium shade


def render_progress_bar(
    percentage: float,
    *,
    width: int = 20,
    show_pct: bool = True,
) -> str:
    """Render a progress bar as a plain string with Rich markup.

    Args:
        percentage: Completion percentage (0-100).
        width: Bar width in characters.
        show_pct: Whether to append percentage number.

    Returns:
        Rich markup string with colored progress bar.
    """
    pct = max(0.0, min(100.0, percentage))
    filled = int((pct / 100.0) * width)
    empty = width - filled

    # Color based on completion
    if pct >= 100.0:
        color = "green"
    elif pct >= 60.0:
        color = "cyan"
    elif pct >= 30.0:
        color = "yellow"
    else:
        color = "dim"

    bar = _BAR_FILLED * filled + _BAR_EMPTY * empty
    result = f"[{color}]{bar}[/{color}]"
    if show_pct:
        result += f" {int(pct):>3}%"
    return result


def render_progress_bar_text(
    percentage: float,
    *,
    width: int = 20,
    show_pct: bool = True,
) -> Text:
    """Render a progress bar as a Rich Text object.

    Args:
        percentage: Completion percentage (0-100).
        width: Bar width in characters.
        show_pct: Whether to append percentage number.

    Returns:
        Rich Text with colored progress bar.
    """
    pct = max(0.0, min(100.0, percentage))
    filled = int((pct / 100.0) * width)
    empty = width - filled

    if pct >= 100.0:
        color = "green"
    elif pct >= 60.0:
        color = "cyan"
    elif pct >= 30.0:
        color = "yellow"
    else:
        color = "dim"

    text = Text()
    text.append(_BAR_FILLED * filled, style=color)
    text.append(_BAR_EMPTY * empty, style="dim")
    if show_pct:
        text.append(f" {int(pct):>3}%", style=color)
    return text


def render_task_progress(
    progress: TaskProgress,
    *,
    width: int = 15,
    compact: bool = False,
) -> Text:
    """Render task progress with bar and metadata.

    Args:
        progress: TaskProgress data.
        width: Bar width.
        compact: If True, shows only bar and percentage.

    Returns:
        Rich Text with progress display.
    """
    text = render_progress_bar_text(progress.percentage, width=width)
    if not compact and progress.files_changed > 0:
        text.append(f" {progress.files_changed}f", style="dim")
    if not compact and progress.tests_total > 0:
        text.append(f" {progress.tests_passing}/{progress.tests_total}t", style="dim")
    return text


def render_progress_summary(progresses: list[TaskProgress]) -> Text:
    """Render an aggregate progress summary for multiple tasks.

    Args:
        progresses: List of task progress entries.

    Returns:
        Rich Text with overall progress bar and counts.
    """
    if not progresses:
        return Text("No progress data", style="dim")

    total_pct = sum(p.percentage for p in progresses)
    avg_pct = total_pct / len(progresses) if progresses else 0.0
    completed = sum(1 for p in progresses if p.percentage >= 100.0)

    text = render_progress_bar_text(avg_pct, width=20)
    text.append(f"  {completed}/{len(progresses)} tasks done", style="dim")
    return text
