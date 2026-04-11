"""Tests for TUI-018: Task detail overlay with full context."""

from __future__ import annotations

import pytest

from bernstein.tui.task_detail_overlay import DetailTab, TaskDetail, format_task_detail


class TestTaskDetail:
    def test_creation(self) -> None:
        detail = TaskDetail(
            task_id="task-001",
            title="Fix bug",
            description="Fix the login bug",
            status="in-progress",
            role="backend",
            agent_id="agent-1",
            cost_usd=0.05,
            log_tail=["line1", "line2"],
            diff_preview="+ added line",
            quality_results={"lint": "pass", "tests": "pass"},
        )
        assert detail.task_id == "task-001"
        assert detail.cost_usd == pytest.approx(0.05)

    def test_format_includes_all_sections(self) -> None:
        detail = TaskDetail(
            task_id="task-001",
            title="Fix bug",
            description="Fix the login bug",
            status="done",
            role="backend",
            agent_id="agent-1",
            cost_usd=0.12,
            log_tail=["Building...", "Tests passed"],
            diff_preview="+new line",
            quality_results={"lint": "pass"},
        )
        # Summary tab (default) shows header + description
        text = format_task_detail(detail)
        assert "task-001" in text
        assert "Fix bug" in text
        assert "backend" in text
        assert "$0.12" in text
        # Logs and diff are on separate tabs
        logs_text = format_task_detail(detail, tab=DetailTab.LOGS)
        assert "Tests passed" in logs_text
        diff_text = format_task_detail(detail, tab=DetailTab.DIFF)
        assert "+new line" in diff_text

    def test_format_handles_missing_optional_fields(self) -> None:
        detail = TaskDetail(
            task_id="task-002",
            title="Simple task",
            description="",
            status="open",
            role="qa",
        )
        text = format_task_detail(detail)
        assert "task-002" in text
        assert "open" in text

    def test_format_truncates_long_log(self) -> None:
        long_log = [f"line {i}" for i in range(200)]
        detail = TaskDetail(
            task_id="t",
            title="t",
            description="",
            status="open",
            role="qa",
            log_tail=long_log,
        )
        text = format_task_detail(detail, tab=DetailTab.LOGS)
        # Should contain last line (truncated to _MAX_LOG_LINES)
        assert "line 199" in text
