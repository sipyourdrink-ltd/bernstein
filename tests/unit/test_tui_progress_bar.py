"""Tests for TUI-010: Task progress bar with completion percentage."""
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import pytest

from bernstein.tui.progress_bar import (
    TaskProgress,
    render_progress_bar,
    render_progress_bar_text,
    render_progress_summary,
    render_task_progress,
)


class TestTaskProgress:
    def test_percentage_from_steps(self) -> None:
        p = TaskProgress(task_id="t1", completed_steps=3, total_steps=10)
        assert p.percentage == pytest.approx(30.0)

    def test_percentage_from_tests(self) -> None:
        p = TaskProgress(task_id="t1", tests_passing=7, tests_total=10)
        assert p.percentage == pytest.approx(70.0)

    def test_percentage_custom(self) -> None:
        p = TaskProgress(task_id="t1", custom_pct=42.5)
        assert p.percentage == pytest.approx(42.5)

    def test_percentage_custom_priority(self) -> None:
        """custom_pct takes priority over step ratio."""
        p = TaskProgress(
            task_id="t1",
            completed_steps=1,
            total_steps=10,
            custom_pct=99.0,
        )
        assert p.percentage == pytest.approx(99.0)

    def test_percentage_zero_no_data(self) -> None:
        p = TaskProgress(task_id="t1")
        assert p.percentage == 0.0

    def test_percentage_clamped(self) -> None:
        p = TaskProgress(task_id="t1", custom_pct=150.0)
        assert p.percentage == 100.0

    def test_from_api_basic(self) -> None:
        raw = {
            "id": "task-123",
            "progress": {
                "completed_steps": 3,
                "total_steps": 5,
            },
        }
        p = TaskProgress.from_api(raw)
        assert p.task_id == "task-123"
        assert p.completed_steps == 3
        assert p.total_steps == 5

    def test_from_api_no_progress(self) -> None:
        raw = {
            "id": "task-456",
            "files_changed": 2,
            "tests_passing": 8,
            "tests_total": 10,
        }
        p = TaskProgress.from_api(raw)
        assert p.files_changed == 2
        assert p.tests_passing == 8

    def test_from_api_with_percentage(self) -> None:
        raw = {
            "id": "task-789",
            "progress": {
                "percentage": 65.0,
            },
        }
        p = TaskProgress.from_api(raw)
        assert p.percentage == pytest.approx(65.0)


class TestRenderProgressBar:
    def test_zero_percent(self) -> None:
        bar = render_progress_bar(0.0, width=10)
        assert "0%" in bar

    def test_fifty_percent(self) -> None:
        bar = render_progress_bar(50.0, width=10)
        assert "50%" in bar

    def test_hundred_percent(self) -> None:
        bar = render_progress_bar(100.0, width=10)
        assert "100%" in bar
        assert "green" in bar

    def test_no_pct_display(self) -> None:
        bar = render_progress_bar(50.0, width=10, show_pct=False)
        assert "%" not in bar

    def test_clamped_over_100(self) -> None:
        bar = render_progress_bar(150.0, width=10)
        assert "100%" in bar


class TestRenderProgressBarText:
    def test_returns_text(self) -> None:
        from rich.text import Text

        text = render_progress_bar_text(50.0, width=10)
        assert isinstance(text, Text)
        assert "50%" in text.plain

    def test_zero(self) -> None:
        text = render_progress_bar_text(0.0, width=10)
        assert "0%" in text.plain

    def test_full(self) -> None:
        text = render_progress_bar_text(100.0, width=10)
        assert "100%" in text.plain


class TestRenderTaskProgress:
    def test_basic_render(self) -> None:
        p = TaskProgress(task_id="t1", completed_steps=5, total_steps=10)
        text = render_task_progress(p)
        assert "50%" in text.plain

    def test_with_files(self) -> None:
        p = TaskProgress(task_id="t1", custom_pct=50.0, files_changed=3)
        text = render_task_progress(p)
        assert "3f" in text.plain

    def test_with_tests(self) -> None:
        p = TaskProgress(task_id="t1", custom_pct=50.0, tests_passing=8, tests_total=10)
        text = render_task_progress(p)
        assert "8/10t" in text.plain

    def test_compact_no_metadata(self) -> None:
        p = TaskProgress(task_id="t1", custom_pct=50.0, files_changed=3)
        text = render_task_progress(p, compact=True)
        assert "3f" not in text.plain


class TestRenderProgressSummary:
    def test_empty(self) -> None:
        text = render_progress_summary([])
        assert "No progress" in text.plain

    def test_multiple_tasks(self) -> None:
        progresses = [
            TaskProgress(task_id="t1", custom_pct=100.0),
            TaskProgress(task_id="t2", custom_pct=50.0),
            TaskProgress(task_id="t3", custom_pct=0.0),
        ]
        text = render_progress_summary(progresses)
        assert "1/3" in text.plain
        assert "done" in text.plain
