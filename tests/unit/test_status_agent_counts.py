"""Unit test verifying consistent live-agent counts across all status surfaces (#4360)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.cli.status import _extract_run_stats, render_status_plain
from bernstein.core.server import create_app
from bernstein.core.tasks.models import AgentSession


@pytest.fixture()
def app_with_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    store = app.state.store

    # Populate store with one dead manager and one working backend
    dead_manager = AgentSession(
        id="agent-mgr-1",
        role="manager",
        status="dead",
        exit_code=1,
    )
    working_backend = AgentSession(
        id="agent-backend-1",
        role="backend",
        status="working",
    )
    store._agents["agent-mgr-1"] = dead_manager
    store._agents["agent-backend-1"] = working_backend

    return app


@pytest.mark.anyio
async def test_live_agent_count_consistent_across_all_surfaces(app_with_store) -> None:  # type: ignore[no-untyped-def]
    """Verify summary.agents, agents.count, and human-rendered active agents all report 1."""
    transport = ASGITransport(app=app_with_store)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/status")

    assert resp.status_code == 200
    data = resp.json()

    # 1. status --json -> summary.agents
    assert data["summary"]["agents"] == 1

    # 2. status --json -> agents.count
    assert data["agents"]["count"] == 1

    # 3. status --json -> agents.items lists the live sessions only.
    # The human line renders len(items), so listing a reaped agent here would
    # make "Active agents: N" over-report the very count this test pins.
    # A reaped row is still preserved on the snapshot fallback path, which is
    # where issue #953 asks the producer never to drop one - see
    # tests/unit/test_run_summary_issue_953.py.
    assert len(data["agents"]["items"]) == 1
    assert data["agents"]["items"][0]["id"] == "agent-backend-1"

    # 4. Human-rendered CLI output -> Active agents: 1
    _tasks, _agents, stats, _per_role, _provider_status, _dependency_scan = _extract_run_stats(data)
    assert sum(1 for a in stats.agents if a.status != "dead") == 1
    plain = render_status_plain(data)
    assert "Active agents: 1" in plain
