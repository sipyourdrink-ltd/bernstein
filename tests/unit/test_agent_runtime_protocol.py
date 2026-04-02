"""Tests for runtime protocol status and abort metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.cli.ui import AgentInfo, AgentStatusTable
from bernstein.core.agent_lifecycle import classify_agent_abort_reason
from bernstein.core.models import AbortReason, AgentSession
from bernstein.core.server import create_app


def test_classify_agent_abort_reason_timeout() -> None:
    """Exit status 124 should classify as a timeout."""
    session = AgentSession(id="agent-1", role="backend", exit_code=124)
    reason, detail = classify_agent_abort_reason(session)

    assert reason is AbortReason.TIMEOUT
    assert "124" in detail


def test_agent_status_table_plain_renders_abort_reason() -> None:
    """Plain status output should expose canonical abort reasons."""
    agent = AgentInfo(
        agent_id="agent-1",
        role="backend",
        model="sonnet",
        status="dead",
        abort_reason="timeout",
    )

    rendered = AgentStatusTable().render_plain([agent])

    assert "dead (timeout)" in rendered


@pytest.mark.anyio
async def test_dashboard_data_includes_abort_reason_fields(tmp_path: Path) -> None:
    """Dashboard agent payload should preserve abort metadata from agents.json."""
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=jsonl_path)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "agents.json").write_text(
        json.dumps(
            {
                "ts": 1_000.0,
                "agents": [
                    {
                        "id": "agent-dead",
                        "role": "backend",
                        "status": "dead",
                        "model": "sonnet",
                        "pid": 321,
                        "task_ids": ["task-001"],
                        "transition_reason": "aborted",
                        "abort_reason": "timeout",
                        "abort_detail": "process exited with timeout status 124",
                        "finish_reason": "agent_exit",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/dashboard/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["agents"][0]["transition_reason"] == "aborted"
    assert payload["agents"][0]["abort_reason"] == "timeout"
    assert payload["agents"][0]["abort_detail"] == "process exited with timeout status 124"
    assert payload["agents"][0]["finish_reason"] == "agent_exit"
