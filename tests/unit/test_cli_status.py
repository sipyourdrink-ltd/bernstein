"""Tests for CLI status rendering with normalized /status payloads."""

from __future__ import annotations

from rich.console import Console

from bernstein.cli.status import _extract_run_stats, _select_urgent_tasks, render_status, render_status_plain
from bernstein.core.view_mode import ViewMode, get_view_config


def test_extract_run_stats_accepts_normalized_status_sections() -> None:
    """CLI status parsing accepts dict-based task and agent sections."""
    payload = {
        "summary": {"total": 2, "done": 1, "open": 1, "failed": 0, "claimed": 0},
        "tasks": {
            "count": 2,
            "items": [
                {"id": "t1", "title": "Fix auth", "role": "backend", "status": "done", "priority": 1},
                {"id": "t2", "title": "Triage queue", "role": "manager", "status": "open", "priority": 2},
            ],
        },
        "agents": {
            "count": 1,
            "items": [
                {
                    "id": "agent-1",
                    "role": "backend",
                    "status": "working",
                    "model": "sonnet",
                    "task_ids": ["t2"],
                    "runtime_s": 12,
                }
            ],
        },
        "costs": {"spent_usd": 1.25},
    }

    tasks, agents, stats, per_role, provider_status, dependency_scan = _extract_run_stats(payload)

    assert len(tasks) == 2
    assert len(agents) == 1
    assert stats.summary.total == 2
    assert stats.total_cost_usd == 1.25
    assert per_role == []
    assert provider_status == {}
    assert dependency_scan == {}


def test_render_status_plain_accepts_normalized_status_sections() -> None:
    """Plain status output works with the new normalized payload shape."""
    payload = {
        "summary": {"total": 1, "done": 0, "open": 1, "failed": 0, "claimed": 0},
        "tasks": {
            "count": 1,
            "items": [{"id": "t1", "title": "Fix auth", "role": "backend", "status": "open", "priority": 1}],
        },
        "agents": {"count": 0, "items": []},
        "costs": {"spent_usd": 0.5},
    }

    plain = render_status_plain(payload)

    assert "Total tasks: 1" in plain
    assert "Done:        0" in plain
    assert "Active agents: 0" in plain
    assert "Total cost:    $0.5000" in plain


def test_select_urgent_tasks_prioritizes_failed_blocked_and_p1() -> None:
    """Urgent task selection surfaces the highest-signal rows first."""
    tasks = [
        {"id": "t4", "title": "done", "status": "done", "priority": 2},
        {"id": "t3", "title": "blocked", "status": "blocked", "priority": 2},
        {"id": "t2", "title": "failed", "status": "failed", "priority": 3},
        {"id": "t1", "title": "p1 open", "status": "open", "priority": 1},
    ]

    selected = _select_urgent_tasks(tasks)

    assert [task["id"] for task in selected[:3]] == ["t2", "t3", "t1"]


def test_render_status_shows_urgent_tasks_section_for_standard_mode() -> None:
    """Standard CLI status rendering leads with the urgent-task section."""
    payload = {
        "summary": {"total": 2, "done": 0, "open": 1, "failed": 1, "claimed": 0},
        "tasks": {
            "count": 2,
            "items": [
                {"id": "t1", "title": "Fix auth", "role": "backend", "status": "failed", "priority": 2},
                {"id": "t2", "title": "Write docs", "role": "docs", "status": "open", "priority": 3},
            ],
        },
        "agents": {"count": 0, "items": []},
        "alerts": [{"level": "error", "message": "1 task failed", "detail": "Fix auth"}],
    }
    console = Console(record=True, force_terminal=True, width=120)

    render_status(payload, console=console, view_config=get_view_config(ViewMode.STANDARD))

    output = console.export_text()
    assert "Urgent Tasks" in output
    assert "1 task failed" in output
