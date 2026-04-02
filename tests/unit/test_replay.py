"""Tests for task-trace replay debugging."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.advanced_cmd import replay_cmd
from bernstein.core.traces import AgentTrace, build_replay_task_request, render_replay_diff


def _trace() -> AgentTrace:
    return AgentTrace(
        trace_id="trace-1",
        session_id="sess-1",
        task_ids=["task-1"],
        agent_role="backend",
        model="sonnet",
        effort="high",
        spawn_ts=1.0,
        task_snapshots=[
            {
                "id": "task-1",
                "title": "Fix login flow",
                "description": "Investigate the auth redirect bug.",
                "role": "backend",
                "priority": 2,
                "scope": "medium",
                "complexity": "medium",
                "result_summary": "Original result",
            }
        ],
    )


def test_build_replay_task_request_applies_model_override_and_context() -> None:
    request = build_replay_task_request(
        _trace(),
        task_id="task-1",
        override_model="opus",
        extra_context="hint: inspect the OAuth callback",
    )

    assert request.model == "opus"
    assert request.title == "[replay] Fix login flow"
    assert "hint: inspect the OAuth callback" in request.description


def test_render_replay_diff_contains_unified_diff_markers() -> None:
    diff = render_replay_diff("line one\nline two", "line one\nline three")

    assert "--- original" in diff
    assert "+++ replay" in diff
    assert "-line two" in diff
    assert "+line three" in diff


def test_replay_cli_replays_task_trace_and_renders_diff(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("bernstein.cli.advanced_cmd.TraceStore.latest_for_task", return_value=_trace()),
        patch("bernstein.cli.advanced_cmd.server_post", return_value={"id": "replay-1"}),
        patch(
            "bernstein.cli.advanced_cmd.server_get",
            return_value={"id": "replay-1", "status": "done", "result_summary": "Replay result"},
        ),
    ):
        result = runner.invoke(
            replay_cmd,
            ["task-1", "--sdd-dir", str(tmp_path / ".sdd"), "--model", "opus", "--extra-context", "hint"],
        )

    assert result.exit_code == 0
    assert "Replay task created" in result.output
    assert "original" in result.output
    assert "replay" in result.output
