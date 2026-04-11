"""Tests for TUI Timeline widget."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, PropertyMock, patch

from rich.text import Text

from bernstein.tui.timeline import TaskTimeline, TimelineEntry


def test_timeline_render_empty() -> None:
    """Test timeline rendering when no data."""
    widget = TaskTimeline()
    text = widget.render()
    assert "No task timing data" in text.plain


def test_timeline_update_data() -> None:
    """Test timeline data update and basic rendering logic."""
    widget = TaskTimeline()
    now = time.time()

    entries = [
        TimelineEntry(task_id="t1", title="Task 1", start_time=now - 100, end_time=now - 50, status="done"),
        TimelineEntry(task_id="t2", title="Task 2", start_time=now - 40, end_time=None, status="in_progress"),
    ]

    widget.update_data(entries)
    assert len(widget._entries) == 2
    assert widget._start_ts == now - 100

    # Mock the size property which is read-only
    with patch("bernstein.tui.timeline.TaskTimeline.size", new_callable=PropertyMock) as mock_size:
        mock_size.return_value = MagicMock(width=100, height=10)
        text = widget.render()
        assert isinstance(text, Text)
        assert "Now" in text.plain
        assert "t1" in text.plain
        assert "t2" in text.plain


def test_timeline_compaction_entry_rendered() -> None:
    """Compaction entries render with a distinct marker (⚡, magenta)."""
    widget = TaskTimeline()
    now = time.time()

    entries = [
        TimelineEntry(task_id="t1", title="Task 1", start_time=now - 100, end_time=now - 50, status="done"),
        TimelineEntry(
            task_id="comp-1",
            title="compaction: 20k → 5k tokens",
            start_time=now - 70,
            end_time=now - 70,
            status="done",
            kind="compaction",
        ),
    ]
    widget.update_data(entries)

    with patch("bernstein.tui.timeline.TaskTimeline.size", new_callable=PropertyMock) as mock_size:
        mock_size.return_value = MagicMock(width=100, height=10)
        text = widget.render()
        plain = text.plain
        assert "⚡" in plain
        assert "compaction" in plain


def test_timeline_kind_defaults_to_task() -> None:
    """TimelineEntry defaults to 'task' kind for backward compatibility."""
    entry = TimelineEntry(task_id="t1", title="T1", start_time=0.0, end_time=1.0, status="done")
    assert entry.kind == "task"


def test_timeline_prefers_lane_label_when_present() -> None:
    """Lane labels make the timeline easier to scan than raw task IDs alone."""
    widget = TaskTimeline()
    now = time.time()
    widget.update_data(
        [
            TimelineEntry(
                task_id="task-12345678",
                title="Implement runtime health",
                start_time=now - 60,
                end_time=now - 10,
                status="done",
                lane="backend:t123",
            )
        ]
    )

    with patch("bernstein.tui.timeline.TaskTimeline.size", new_callable=PropertyMock) as mock_size:
        mock_size.return_value = MagicMock(width=100, height=10)
        text = widget.render()
        assert "backend:t123"[:12] in text.plain
