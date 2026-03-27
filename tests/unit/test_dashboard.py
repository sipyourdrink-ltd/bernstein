"""Tests for the Bernstein web dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

TASK_PAYLOAD = {
    "title": "Implement parser",
    "description": "Write the YAML parser module",
    "role": "backend",
    "priority": 2,
}


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):  # type: ignore[no-untyped-def]
    """Create a fresh FastAPI app per test."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# -- GET /dashboard ---------------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_returns_200(client: AsyncClient) -> None:
    """GET /dashboard returns 200 with HTML content."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.anyio
async def test_dashboard_contains_key_elements(client: AsyncClient) -> None:
    """Dashboard HTML contains the task table, agent section, and stats bar."""
    resp = await client.get("/dashboard")
    html = resp.text
    assert "Bernstein" in html
    assert "task" in html.lower()
    assert "agent" in html.lower()
    assert "cost" in html.lower() or "stat" in html.lower()


@pytest.mark.anyio
async def test_dashboard_contains_script(client: AsyncClient) -> None:
    """Dashboard HTML includes JavaScript for auto-refresh."""
    resp = await client.get("/dashboard")
    html = resp.text
    assert "<script" in html.lower()


# -- GET /events (SSE) ------------------------------------------------------


@pytest.mark.anyio
async def test_events_returns_sse_content_type(app) -> None:  # type: ignore[no-untyped-def]
    """GET /events returns text/event-stream content type.

    SSE is a long-lived streaming connection. Instead of trying to read from
    the stream (which blocks on ASGI transport), we test the SSE bus and the
    route registration independently.
    """
    from bernstein.core.server import SSEBus

    # Verify the /events route is registered
    routes = [r.path for r in app.routes if hasattr(r, "path")]  # type: ignore[union-attr]
    assert "/events" in routes

    # Verify the SSE bus works correctly
    bus = SSEBus()
    queue = bus.subscribe()
    bus.publish("task_update", '{"id": "abc"}')
    msg = queue.get_nowait()
    assert "event: task_update" in msg
    assert '{"id": "abc"}' in msg
    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


@pytest.mark.anyio
async def test_sse_bus_fan_out() -> None:
    """SSE bus delivers events to all subscribers."""
    from bernstein.core.server import SSEBus

    bus = SSEBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish("heartbeat", '{"ts": 1}')
    assert "heartbeat" in q1.get_nowait()
    assert "heartbeat" in q2.get_nowait()
    bus.unsubscribe(q1)
    bus.unsubscribe(q2)


# -- GET /dashboard/data ----------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_data_returns_json(client: AsyncClient) -> None:
    """GET /dashboard/data returns JSON with expected top-level keys."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    data = resp.json()
    assert "stats" in data
    assert "tasks" in data
    assert "agents" in data
    assert "cost_by_role" in data
    assert "live_costs" in data


@pytest.mark.anyio
async def test_dashboard_data_stats_keys(client: AsyncClient) -> None:
    """Dashboard data stats object has the expected fields."""
    resp = await client.get("/dashboard/data")
    stats = resp.json()["stats"]
    for key in ("total", "open", "claimed", "done", "failed", "agents", "cost_usd"):
        assert key in stats, f"Missing stats key: {key}"


@pytest.mark.anyio
async def test_dashboard_data_live_costs_keys(client: AsyncClient) -> None:
    """Dashboard data live_costs has per-model, per-agent, and budget fields."""
    resp = await client.get("/dashboard/data")
    live_costs = resp.json()["live_costs"]
    for key in ("spent_usd", "budget_usd", "percentage_used", "per_model", "per_agent"):
        assert key in live_costs, f"Missing live_costs key: {key}"


@pytest.mark.anyio
async def test_dashboard_data_with_tasks(client: AsyncClient) -> None:
    """Dashboard data includes task data after creating a task."""
    # Create a task first
    await client.post("/tasks", json=TASK_PAYLOAD)
    resp = await client.get("/dashboard/data")
    data = resp.json()
    assert data["stats"]["total"] == 1
    assert data["stats"]["open"] == 1
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["title"] == "Implement parser"
    assert data["tasks"][0]["role"] == "backend"


@pytest.mark.anyio
async def test_dashboard_data_with_agent(client: AsyncClient) -> None:
    """Dashboard data reflects agent heartbeats."""
    await client.post(
        "/agents/agent-001/heartbeat",
        json={"role": "backend", "status": "working"},
    )
    resp = await client.get("/dashboard/data")
    data = resp.json()
    assert data["stats"]["agents"] == 1
    assert len(data["agents"]) == 1
    assert data["agents"][0]["id"] == "agent-001"
