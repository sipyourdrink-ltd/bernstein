"""Tests for /health component-level status."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "runtime").mkdir()
    return sdd


@pytest.fixture()
def app(jsonl_path: Path, sdd_dir: Path):
    a = create_app(jsonl_path=jsonl_path)
    a.state.sdd_dir = sdd_dir
    return a


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_includes_components(client: AsyncClient) -> None:
    """/health response includes a 'components' dict."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    comps = data["components"]
    assert isinstance(comps, dict)


@pytest.mark.anyio
async def test_health_component_keys(client: AsyncClient) -> None:
    """All four expected components are present."""
    resp = await client.get("/health")
    data = resp.json()
    comps = data["components"]
    for key in ("server", "spawner", "database", "agents"):
        assert key in comps, f"Missing component: {key}"


@pytest.mark.anyio
async def test_health_component_schema(client: AsyncClient) -> None:
    """Each component has a 'status' field with a valid value."""
    valid_statuses = {"ok", "degraded", "down", "unknown"}
    resp = await client.get("/health")
    comps = resp.json()["components"]
    for name, comp in comps.items():
        assert "status" in comp, f"Component {name} missing 'status'"
        assert comp["status"] in valid_statuses, f"Invalid status for {name}: {comp['status']}"


# ---------------------------------------------------------------------------
# Server component
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_server_component_always_ok(client: AsyncClient) -> None:
    """The 'server' component is always 'ok' when the endpoint responds."""
    resp = await client.get("/health")
    assert resp.json()["components"]["server"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Spawner component
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_spawner_unknown_when_no_pid_file(client: AsyncClient) -> None:
    """Spawner is 'unknown' when no pid file exists."""
    resp = await client.get("/health")
    comp = resp.json()["components"]["spawner"]
    assert comp["status"] == "unknown"


@pytest.mark.anyio
async def test_spawner_ok_when_pid_alive(app, sdd_dir: Path) -> None:
    """Spawner is 'ok' when pid file exists and process is alive."""
    pid_file = sdd_dir / "runtime" / "spawner.pid"
    pid_file.write_text(str(os.getpid()))  # current process is definitely alive

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    comp = resp.json()["components"]["spawner"]
    assert comp["status"] == "ok"
    assert str(os.getpid()) in comp["detail"]


@pytest.mark.anyio
async def test_spawner_down_when_pid_dead(app, sdd_dir: Path) -> None:
    """Spawner is 'down' when pid file contains a dead process."""
    pid_file = sdd_dir / "runtime" / "spawner.pid"
    # PID 0 is the kernel — os.kill(0, 0) sends to the process group, which
    # we don't want. Use a high PID that almost certainly doesn't exist.
    pid_file.write_text("9999999")

    with patch("os.kill", side_effect=ProcessLookupError):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
    comp = resp.json()["components"]["spawner"]
    assert comp["status"] == "down"


# ---------------------------------------------------------------------------
# Database component
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_database_ok_when_directory_accessible(client: AsyncClient) -> None:
    """Database is 'ok' when the task store directory is accessible."""
    resp = await client.get("/health")
    assert resp.json()["components"]["database"]["status"] == "ok"


@pytest.mark.anyio
async def test_database_down_when_mkdir_fails(app) -> None:
    """Database is 'down' when storage directory cannot be created."""
    with patch("pathlib.Path.mkdir", side_effect=OSError("permission denied")):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
    assert resp.json()["components"]["database"]["status"] == "down"


# ---------------------------------------------------------------------------
# Agents component
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agents_ok_when_no_agents(client: AsyncClient) -> None:
    """Agents component is 'ok' with 'no active agents' detail when idle."""
    resp = await client.get("/health")
    comp = resp.json()["components"]["agents"]
    assert comp["status"] == "ok"
    assert "no active agents" in comp["detail"]


@pytest.mark.anyio
async def test_agents_ok_with_active_agents(client: AsyncClient) -> None:
    """Agents component shows active count when agents are running."""
    await client.post("/agents/a1/heartbeat", json={"role": "backend"})

    resp = await client.get("/health")
    comp = resp.json()["components"]["agents"]
    assert comp["status"] == "ok"
    assert "1 active" in comp["detail"]
