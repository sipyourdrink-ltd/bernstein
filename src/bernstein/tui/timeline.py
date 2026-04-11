"""Timeline view of task execution for the Bernstein TUI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.widgets import Static

from bernstein.tui.widgets import STATUS_COLORS


@dataclass
class TimelineEntry:
    """A single task's timing data for the timeline.

    Attributes:
        task_id: Task or event identifier.
        title: Human-readable label.
        start_time: Unix timestamp when the entry started.
        end_time: Unix timestamp when the entry ended (None = in-progress).
        status: Task status string ("done", "failed", etc.).
        kind: Entry type — "task" or "compaction" for distinct markers.
    """

    task_id: str
    title: str
    start_time: float
    end_time: float | None
    status: str
    kind: str = "task"
    lane: str = ""


class TaskTimeline(Static):
    """Gantt-like horizontal bars showing task durations and event markers."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entries: list[TimelineEntry] = []
        self._start_ts: float = time.time()
        self._end_ts: float = time.time()

    def update_data(self, entries: list[TimelineEntry]) -> None:
        """Update timeline data and refresh view.

        Args:
            entries: List of task timing and event entries.
        """
        self._entries = sorted(entries, key=lambda x: x.start_time)
        if not self._entries:
            self._start_ts = time.time() - 3600
            self._end_ts = time.time()
        else:
            self._start_ts = min(e.start_time for e in self._entries)
            self._end_ts = max((e.end_time or time.time()) for e in self._entries)
            # Add some padding
            duration = self._end_ts - self._start_ts
            self._end_ts += duration * 0.05

        self.refresh()

    def render(self) -> Text:
        """Render the timeline as a Rich text object."""
        if not self._entries:
            return Text("No task timing data available.", style="dim")

        text = Text()
        width = self.size.width - 24  # Reserve space for lane labels
        if width < 12:
            return Text("Window too narrow.")

        duration = self._end_ts - self._start_ts
        if duration <= 0:
            duration = 1.0

        now_ts = min(max(time.time(), self._start_ts), self._end_ts)
        now_off = min(width - 1, max(0, int(((now_ts - self._start_ts) / duration) * width)))
        ruler = ["─"] * width
        ruler[now_off] = "▲"
        text.append("Now".ljust(12), style="bold cyan")
        text.append(" ")
        text.append("".join(ruler), style="dim cyan")
        text.append("\n")

        for entry in self._entries:
            lane_label = (entry.lane or entry.task_id[:8] or "task")[:12]
            # Label
            text.append(f"{lane_label:<12} ", style="cyan")

            # Entry kind: compaction events render as distinct markers
            if entry.kind == "compaction":
                start_off = int(((entry.start_time - self._start_ts) / duration) * width)
                start_off = max(0, start_off)
                text.append(" " * start_off)
                text.append("⚡", style="magenta")
                text.append(f" {entry.title[:32]}", style="magenta dim")
                text.append("\n")
                continue

            # Calculate bar position and width
            start_off = int(((entry.start_time - self._start_ts) / duration) * width)
            end_ts = entry.end_time or time.time()
            bar_width = int(((end_ts - entry.start_time) / duration) * width)
            bar_width = max(1, bar_width)

            # Draw bar
            color = STATUS_COLORS.get(entry.status, "white")
            text.append(" " * start_off)
            bar_char = "░" if entry.status == "blocked" else "█"
            text.append(bar_char * bar_width, style=color)

            # Status tag
            text.append(" ", style="dim")
            text.append(entry.title[:20], style="dim")
            if entry.end_time:
                dur_s = int(entry.end_time - entry.start_time)
                text.append(f" {dur_s}s", style="dim")
            else:
                text.append(" (running)", style="yellow dim")

            text.append("\n")

        return text


# ---------------------------------------------------------------------------
# Compaction event indicators in TUI timeline (T563)
# ---------------------------------------------------------------------------


def add_compaction_marker(
    timeline: TaskTimeline,
    timestamp: float,
    *,
    title: str = "Compaction",
    duration_seconds: float = 0.0,
) -> None:
    """Add a compaction event marker to the timeline (T563).

    Args:
        timeline: TaskTimeline instance.
        timestamp: Unix timestamp when compaction started.
        title: Display title for the compaction event.
        duration_seconds: Duration of compaction in seconds.
    """
    entry = TimelineEntry(
        task_id=f"compaction_{int(timestamp)}",
        title=title,
        start_time=timestamp,
        end_time=timestamp + duration_seconds if duration_seconds > 0 else None,
        status="compaction",
        kind="compaction",
    )
    timeline.update_data([entry])
