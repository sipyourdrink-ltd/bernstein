"""Tests for the bernstein live command Rich display helpers.

These tests drive out the helper functions used by the 'live' command to build
the structured Rich display (agents table, events table, stats bar).
"""
from __future__ import annotations

from typing import Any

import pytest
from rich.table import Table
from rich.text import Text

from bernstein.cli.main import _build_agents_table, _build_events_table, _build_stats_bar


# --- Helpers ---


def _make_agent(
    *,
    id: str = "agent-001",
    role: str = "backend",
    status: str = "working",
    model: str = "sonnet",
    task_ids: list[str] | None = None,
    started_at: float | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "role": role,
        "status": status,
        "model": model,
        "task_ids": task_ids or [],
        "started_at": started_at,
    }


def _make_task(
    *,
    id: str = "task-001",
    title: str = "Implement feature",
    status: str = "in_progress",
    role: str = "backend",
) -> dict[str, Any]:
    return {"id": id, "title": title, "status": status, "role": role}


def _make_summary(
    *,
    total: int = 10,
    done: int = 5,
    failed: int = 1,
    in_progress: int = 2,
    cost_usd: float = 0.042,
    elapsed_seconds: int = 120,
) -> dict[str, Any]:
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "in_progress": in_progress,
        "cost_usd": cost_usd,
        "elapsed_seconds": elapsed_seconds,
    }


# --- Tests for _build_agents_table ---


class TestBuildAgentsTable:
    def test_returns_rich_table(self) -> None:
        table = _build_agents_table([])
        assert isinstance(table, Table)

    def test_empty_agents_shows_no_rows(self) -> None:
        table = _build_agents_table([])
        assert table.row_count == 0

    def test_single_agent_row(self) -> None:
        agents = [_make_agent(id="a-001", role="backend")]
        table = _build_agents_table(agents)
        assert table.row_count == 1

    def test_multiple_agents_all_shown(self) -> None:
        agents = [
            _make_agent(id=f"a-{i:03d}", role="backend")
            for i in range(5)
        ]
        table = _build_agents_table(agents)
        assert table.row_count == 5

    def test_table_has_agent_column(self) -> None:
        table = _build_agents_table([])
        col_names = [col.header for col in table.columns]
        assert any("agent" in name.lower() for name in col_names)

    def test_table_has_status_column(self) -> None:
        table = _build_agents_table([])
        col_names = [col.header for col in table.columns]
        assert any("status" in name.lower() for name in col_names)


# --- Tests for _build_events_table ---


class TestBuildEventsTable:
    def test_returns_rich_table(self) -> None:
        table = _build_events_table([])
        assert isinstance(table, Table)

    def test_empty_tasks_shows_no_rows(self) -> None:
        table = _build_events_table([])
        assert table.row_count == 0

    def test_single_task_row(self) -> None:
        tasks = [_make_task(id="t-001", status="done")]
        table = _build_events_table(tasks)
        assert table.row_count == 1

    def test_multiple_tasks_all_shown(self) -> None:
        tasks = [_make_task(id=f"t-{i:03d}") for i in range(8)]
        table = _build_events_table(tasks)
        assert table.row_count == 8

    def test_table_has_status_column(self) -> None:
        table = _build_events_table([])
        col_names = [col.header for col in table.columns]
        assert any("status" in name.lower() for name in col_names)

    def test_table_has_title_column(self) -> None:
        table = _build_events_table([])
        col_names = [col.header for col in table.columns]
        assert any("title" in name.lower() for name in col_names)


# --- Tests for _build_stats_bar ---


class TestBuildStatsBar:
    def test_returns_renderable(self) -> None:
        result = _build_stats_bar(_make_summary())
        assert result is not None

    def test_contains_total_count(self) -> None:
        result = _build_stats_bar(_make_summary(total=10))
        rendered = str(result)
        assert "10" in rendered

    def test_contains_done_count(self) -> None:
        result = _build_stats_bar(_make_summary(done=5))
        rendered = str(result)
        assert "5" in rendered

    def test_contains_failed_count(self) -> None:
        result = _build_stats_bar(_make_summary(failed=3))
        rendered = str(result)
        assert "3" in rendered

    def test_empty_summary_does_not_raise(self) -> None:
        result = _build_stats_bar({})
        assert result is not None


# --- Verify live command uses Rich Live ---


class TestLiveCommandUsesRichLive:
    def test_rich_live_imported_in_main(self) -> None:
        """Verify the live command module uses Rich Live for display."""
        import bernstein.cli.main as main_module
        import inspect

        source = inspect.getsource(main_module)
        assert "Live" in source

    def test_live_function_exists(self) -> None:
        from bernstein.cli.main import live
        assert callable(live)
