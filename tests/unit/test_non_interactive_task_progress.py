"""Tests for non-interactive task state transition progress and status columns (#5338)."""

from __future__ import annotations

import re
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.view_mode import ViewMode, get_view_config
from rich.console import Console

from bernstein.cli.run_bootstrap import _await_first_spawn_outcome
from bernstein.cli.run_preflight import (
    TaskStateProgressTracker,
    _finalize_run_output,
)
from bernstein.cli.status import _build_task_table, render_status
from bernstein.core.routes.status_dashboard import _status_task_items
from bernstein.core.server.server_app import task_to_response
from bernstein.core.tasks.models import Task

_NON_TTY_CAPS = MagicMock(supports_textual=False, is_tty=False)


def test_status_table_includes_adapter_and_model_columns() -> None:
    """_build_task_table includes Adapter and Model columns and populates them."""
    tasks = [
        {
            "id": "t-1",
            "title": "Build feature",
            "role": "backend",
            "status": "in_progress",
            "priority": 1,
            "adapter": "fake-adapter",
            "model": "fake-model-route",
            "assigned_agent": "agent-1",
        },
        {
            "id": "t-2",
            "title": "Write tests",
            "role": "qa",
            "status": "open",
            "priority": 2,
            "cli": "claude",
            "model": "claude-3-7-sonnet",
        },
    ]

    table = _build_task_table(tasks)
    col_names = [col.header for col in table.columns]

    assert "Adapter" in col_names
    assert "Model" in col_names

    console = Console(record=True, force_terminal=True, width=140)
    console.print(table)
    output = console.export_text()

    assert "fake-adapter" in output
    assert "fake-model-route" in output
    assert "claude" in output
    assert "claude-3-7-sonnet" in output


def test_tracker_prints_markup_title_verbatim_without_killing_stream() -> None:
    """A title with rich markup must print verbatim and not suppress later tasks."""
    con = Console(record=True, force_terminal=True, width=160)
    tracker = TaskStateProgressTracker(console=con)

    tasks = [
        {"id": "t-1", "title": "[/api/v1] roll the API", "status": "open", "adapter": "codex", "model": "o3"},
        {"id": "t-2", "title": "Second task", "status": "open", "adapter": "codex", "model": "o3"},
    ]
    lines = tracker.update_tasks(tasks)

    assert 'task t-1 planned adapter=codex model=o3 title="[/api/v1] roll the API"' in lines
    assert any(line.startswith("task t-2 ") for line in lines)
    assert "t-1" in tracker.seen_states
    assert "t-2" in tracker.seen_states


def test_tracker_emits_unknown_for_unrecorded_routing() -> None:
    """Missing adapter/model must render as unknown, not fabricated defaults."""
    con = Console(record=True, force_terminal=True, width=160)
    tracker = TaskStateProgressTracker(console=con)

    lines = tracker.update_tasks([{"id": "t-1", "title": "No routing", "status": "open"}])

    assert 'task t-1 planned adapter=unknown model=unknown title="No routing"' in lines


def test_tracker_does_not_emit_planned_for_terminal_first_state() -> None:
    """A task already terminal at first observation must not claim it was planned this run."""
    con = Console(record=True, force_terminal=True, width=160)
    tracker = TaskStateProgressTracker(console=con)

    lines = tracker.update_tasks([{"id": "t-1", "title": "Already done", "status": "done"}])

    assert lines == []
    assert tracker.seen_states["t-1"] == "done"


def test_on_poll_hook_is_invoked_across_polls() -> None:
    """The on_poll hook must fire on each poll until the first spawn is confirmed."""
    calls: list[int] = []
    agent_count = {"value": 0}

    def stub(path: str) -> Any:
        if path == "/health":
            count = agent_count["value"]
            agent_count["value"] += 1
            return {"agent_count": count}
        return {"tasks": [], "total": 0, "limit": 50, "offset": 0}

    with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
        outcome, _reason = _await_first_spawn_outcome(
            timeout_s=5.0,
            poll_interval_s=0.01,
            on_poll=lambda: calls.append(1),
        )

    assert outcome == "spawned"
    assert len(calls) >= 2


def test_status_render_recorded_runtime_state_has_adapter_column() -> None:
    """bernstein status output on recorded runtime state includes an Adapter column."""
    recorded_runtime_payload = {
        "summary": {"total": 2, "done": 1, "open": 1, "failed": 0, "claimed": 0},
        "tasks": {
            "count": 2,
            "items": [
                {
                    "id": "task-alpha",
                    "title": "Implement authentication gate",
                    "role": "backend",
                    "status": "done",
                    "priority": 1,
                    "adapter": "codex",
                    "model": "o3-mini",
                },
                {
                    "id": "task-beta",
                    "title": "Audit security policies",
                    "role": "security",
                    "status": "open",
                    "priority": 1,
                    "cli": "opencode",
                    "model": "deepseek-r1",
                },
            ],
        },
        "agents": {"count": 0, "items": []},
        "costs": {"spent_usd": 0.42},
    }

    console = Console(record=True, force_terminal=True, width=140)
    render_status(recorded_runtime_payload, console=console, view_config=get_view_config(ViewMode.STANDARD))
    output = console.export_text()

    assert "Adapter" in output
    assert "Model" in output
    assert "codex" in output
    assert "opencode" in output


def test_status_without_tty_renders_plain_summary_not_table() -> None:
    """Non-TTY status output uses the plain summary; the table is TTY-only."""
    recorded_runtime_payload = {
        "summary": {"total": 2, "done": 1, "open": 1, "failed": 0, "claimed": 0},
        "tasks": {
            "count": 2,
            "items": [
                {"id": "task-alpha", "title": "Implement auth", "role": "backend", "status": "done", "priority": 1},
                {"id": "task-beta", "title": "Audit policies", "role": "security", "status": "open", "priority": 1},
            ],
        },
        "agents": {"count": 0, "items": []},
        "costs": {"spent_usd": 0.42},
    }

    console = Console(record=True, force_terminal=False, width=140)
    render_status(recorded_runtime_payload, console=console, view_config=get_view_config(ViewMode.STANDARD))
    output = console.export_text()

    assert "Total tasks: 2" in output
    assert "Adapter" not in output


def test_status_items_report_unset_routing_as_empty_not_fabricated() -> None:
    """A real Task with no recorded routing must surface empty fields, not defaults."""
    task = Task(id="t-1", title="No routing", description="Write it.", role="backend", batch_eligible=False)

    rows = _status_task_items([task], now=time.time())

    assert rows[0]["adapter"] == ""
    assert rows[0]["cli"] == ""
    assert rows[0]["model"] == ""


def test_tracker_reports_unknown_through_real_task_response_path() -> None:
    """The /tasks payload for a Task with no recorded routing must render as unknown."""
    task = Task(id="t-1", title="No routing", description="Write it.", role="backend", batch_eligible=False)
    payload = task_to_response(task).model_dump()

    con = Console(record=True, force_terminal=True, width=160)
    tracker = TaskStateProgressTracker(console=con)
    lines = tracker.update_tasks([payload])

    assert 'task t-1 planned adapter=unknown model=unknown title="No routing"' in lines


def test_task_state_progress_tracker_format_and_no_paths_or_timestamps() -> None:
    """Line formatter conforms to schema: task <id> <state> adapter=<name> model=<route> title="<60 chars>"."""
    tracker = TaskStateProgressTracker()
    line = tracker.format_line(
        task_id="task-42",
        state="in_progress",
        adapter="fake-adapter",
        model="fake-route",
        title="Very long title that exceeds sixty characters and therefore must be safely truncated to sixty",
    )

    expected_prefix = 'task task-42 in_progress adapter=fake-adapter model=fake-route title="'
    assert line.startswith(expected_prefix)
    title_match = re.search(r'title="([^"]*)"', line)
    assert title_match is not None
    assert len(title_match.group(1)) <= 60

    # No local machine timestamps or filesystem paths
    assert "/" not in line
    assert "\\" not in line
    assert not re.search(r"\d{4}-\d{2}-\d{2}", line)


def test_task_state_progress_tracker_escapes_double_quotes_in_title() -> None:
    """Double quotes inside task title are converted to single quotes so log line format is preserved."""
    tracker = TaskStateProgressTracker()
    line = tracker.format_line(
        task_id="task-43",
        state="open",
        adapter="codex",
        model="o3-mini",
        title='Fix the "critical" issue',
    )
    assert line == "task task-43 open adapter=codex model=o3-mini title=\"Fix the 'critical' issue\""


def test_two_task_plan_with_fake_adapter_captures_planned_and_later_states() -> None:
    """With a fake adapter and a plan of two tasks, tracker captures planned and later state lines."""
    con = Console(record=True)
    tracker = TaskStateProgressTracker(console=con)

    initial_tasks = [
        {"id": "t-1", "title": "First task", "status": "open", "adapter": "fake-adapter", "model": "route-a"},
        {"id": "t-2", "title": "Second task", "status": "open", "adapter": "fake-adapter", "model": "route-b"},
    ]

    lines_round_1 = tracker.update_tasks(initial_tasks)

    # For each task, must capture planned line and later state (open) line
    assert 'task t-1 planned adapter=fake-adapter model=route-a title="First task"' in lines_round_1
    assert 'task t-1 open adapter=fake-adapter model=route-a title="First task"' in lines_round_1
    assert 'task t-2 planned adapter=fake-adapter model=route-b title="Second task"' in lines_round_1
    assert 'task t-2 open adapter=fake-adapter model=route-b title="Second task"' in lines_round_1

    # Transition task 1 to in_progress, task 2 remains open
    updated_tasks = [
        {"id": "t-1", "title": "First task", "status": "in_progress", "adapter": "fake-adapter", "model": "route-a"},
        {"id": "t-2", "title": "Second task", "status": "open", "adapter": "fake-adapter", "model": "route-b"},
    ]
    lines_round_2 = tracker.update_tasks(updated_tasks)

    assert len(lines_round_2) == 1
    assert 'task t-1 in_progress adapter=fake-adapter model=route-a title="First task"' in lines_round_2

    # Task 1 transitions to done
    final_tasks = [
        {"id": "t-1", "title": "First task", "status": "done", "adapter": "fake-adapter", "model": "route-a"},
        {"id": "t-2", "title": "Second task", "status": "in_progress", "adapter": "fake-adapter", "model": "route-b"},
    ]
    lines_round_3 = tracker.update_tasks(final_tasks)

    assert 'task t-1 done adapter=fake-adapter model=route-a title="First task"' in lines_round_3
    assert 'task t-2 in_progress adapter=fake-adapter model=route-b title="Second task"' in lines_round_3


def test_finalize_run_output_non_tty_captures_task_transitions_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_finalize_run_output in non-TTY mode with fake adapter and two tasks captures planned + state lines."""
    two_task_plan = [
        {
            "id": "task-101",
            "title": "Run code migration",
            "status": "in_progress",
            "adapter": "fake",
            "model": "m-fast",
        },
        {"id": "task-102", "title": "Validate build output", "status": "open", "cli": "fake", "model": "m-thorough"},
    ]

    def mock_server_get(path: str) -> Any:
        if path == "/tasks":
            return two_task_plan
        if path == "/health":
            return {"agent_count": 1}
        if path == "/status":
            return {"total": 2, "open": 1, "in_progress": 1, "done": 0, "failed": 0}
        return None

    with (
        patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
        patch("bernstein.cli.helpers.server_get", side_effect=mock_server_get),
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=mock_server_get),
        patch("bernstein.cli.run_preflight._show_run_summary"),
        patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        patch("bernstein.cli.run_bootstrap._poll_no_plan_after_spawn", return_value=None),
        patch("bernstein.cli.run_bootstrap._poll_quiescent_status", return_value=None),
    ):
        _finalize_run_output(quiet=False)

    out = capsys.readouterr().out

    # Planned lines
    assert 'task task-101 planned adapter=fake model=m-fast title="Run code migration"' in out
    assert 'task task-102 planned adapter=fake model=m-thorough title="Validate build output"' in out

    # Later state lines
    assert 'task task-101 in_progress adapter=fake model=m-fast title="Run code migration"' in out
    assert 'task task-102 open adapter=fake model=m-thorough title="Validate build output"' in out

    # Standard detach notice still printed
    assert "Run continues in the background (check: bernstein status)." in out

    # No timestamps or absolute paths in the captured task lines
    for line in out.splitlines():
        if line.startswith("task task-"):
            assert "/" not in line
            assert "\\" not in line
            assert not re.search(r"\d{4}-\d{2}-\d{2}", line)
