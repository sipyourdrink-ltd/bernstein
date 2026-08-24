"""Regression test for issue #4360: the three /status live-agent surfaces.

The supervisor loop waits on ``summary.agents``, automation reads
``agents.count`` and the human ``bernstein status`` line renders
``len(agents.items)``. Before the fix these could all disagree for the same
run because the "is this agent alive?" test was written two different ways
(``str(status) != "dead"`` vs ``status != "dead"``) and ``agents.count`` had
a snapshot fallback. All three surfaces must now report the same count of
live agent sessions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import AgentSession
from httpx import ASGITransport, AsyncClient

from bernstein.cli.status import render_status_plain
from bernstein.core.server import create_app


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


def _seed_one_working_one_dead(app) -> None:  # type: ignore[no-untyped-def]
    """Seed the app store with one working and one dead agent session."""
    store = app.state.store
    store.agents["sess-live"] = AgentSession(id="sess-live", role="backend", status="working")
    store.agents["sess-dead"] = AgentSession(id="sess-dead", role="manager", status="dead")


@pytest.mark.anyio
async def test_status_surfaces_agree_on_live_agent_count(app) -> None:  # type: ignore[no-untyped-def]
    """All three / status surfaces report the same alive-agent count."""
    _seed_one_working_one_dead(app)

    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/status")

    assert resp.status_code == 200
    data = resp.json()

    assert data["summary"]["agents"] == 1
    assert data["agents"]["count"] == 1
    assert len(data["agents"]["items"]) == 1

    plain = render_status_plain(data)
    assert "Active agents: 1" in plain


@pytest.mark.anyio
async def test_status_surfaces_agree_when_only_live_only_dead(app) -> None:  # type: ignore[no-untyped-def]
    """Two live agents and a run with no dead ones still agree."""
    store = app.state.store
    store.agents["sess-a"] = AgentSession(id="sess-a", role="backend", status="starting")
    store.agents["sess-b"] = AgentSession(id="sess-b", role="qa", status="working")
    store.agents["sess-c"] = AgentSession(id="sess-c", role="manager", status="dead")

    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/status")).json()

    assert data["summary"]["agents"] == 2
    assert data["agents"]["count"] == 2
    assert len(data["agents"]["items"]) == 2
    plain = render_status_plain(data)
    assert "Active agents: 2" in plain
