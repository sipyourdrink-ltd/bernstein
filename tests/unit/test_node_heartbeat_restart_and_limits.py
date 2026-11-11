"""Node registry restart persistence, 410 for unknown heartbeats, heartbeat rate-limit bucket."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.auth_rate_limiter import (
    DEFAULT_HEARTBEAT_RPM,
    DEFAULT_READ_RPM,
    DEFAULT_WRITE_RPM,
    RequestRateLimitMiddleware,
)
from bernstein.core.models import ClusterConfig, NodeStatus
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

NODE_PAYLOAD = {
    "name": "worker-1",
    "url": "http://worker1:8052",
    "capacity": {"max_agents": 4, "available_slots": 4, "active_agents": 0},
}
_RPM_NAMES = ("WRITE", "READ", "HEARTBEAT")


def _cluster_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    # Runtime dir deliberately NOT named .sdd/runtime.
    return create_app(jsonl_path=tmp_path / "state" / "tasks.jsonl", cluster_config=ClusterConfig(enabled=True))


@pytest.mark.anyio
async def test_unknown_node_heartbeat_returns_410(tmp_path: Path) -> None:
    app = _cluster_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/cluster/nodes/ghost/heartbeat", json={})
        assert resp.status_code == 410
        assert resp.json()["detail"] == {
            "error": "node_unknown",
            "node_id": "ghost",
            "hint": "re-register via POST /cluster/nodes",
        }
        # Other node routes keep 404 semantics.
        assert (await c.delete("/cluster/nodes/ghost")).status_code == 404
    responses = app.openapi()["paths"]["/cluster/nodes/{node_id}/heartbeat"]["post"]["responses"]
    assert "410" in responses
    assert "404" not in responses


@pytest.mark.anyio
async def test_persist_env_override_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    persist = tmp_path / "persist" / "nodes.json"
    monkeypatch.setenv("BERNSTEIN_NODES_PERSIST_PATH", str(persist))

    app1 = _cluster_app(tmp_path / "a")
    async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://test") as c:
        node_id = (await c.post("/cluster/nodes", json=NODE_PAYLOAD)).json()["id"]
    assert persist.exists()

    app2 = _cluster_app(tmp_path / "b")
    restored = app2.state.node_registry.get(node_id)
    assert restored is not None
    assert restored.status == NodeStatus.OFFLINE
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as c:
        resp = await c.post(f"/cluster/nodes/{node_id}/heartbeat", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "online"
    assert app2.state.node_registry.get(node_id).status == NodeStatus.ONLINE


def _limited_app(**kwargs: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestRateLimitMiddleware, **kwargs)

    for route in (
        "/cluster/nodes/{rid}/heartbeat",
        "/api/v1/cluster/nodes/{rid}/heartbeat",
        "/agents/{rid}/heartbeat",
        "/api/v1/agents/{rid}/heartbeat",
    ):

        @app.post(route)
        def _hb(rid: str) -> dict[str, str]:
            return {"ok": rid}

    return app


def test_env_rpm_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_RATE_LIMIT_WRITE_RPM", "120")
    monkeypatch.setenv("BERNSTEIN_RATE_LIMIT_READ_RPM", "900")
    monkeypatch.setenv("BERNSTEIN_RATE_LIMIT_HEARTBEAT_RPM", "1200")
    mw = RequestRateLimitMiddleware(FastAPI())
    assert (mw._write_rpm, mw._read_rpm, mw._heartbeat_rpm) == (120, 900, 1200)


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
def test_env_rpm_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    for name in _RPM_NAMES:
        monkeypatch.setenv(f"BERNSTEIN_RATE_LIMIT_{name}_RPM", bad)
    mw = RequestRateLimitMiddleware(FastAPI())
    assert (mw._write_rpm, mw._read_rpm, mw._heartbeat_rpm) == (
        DEFAULT_WRITE_RPM,
        DEFAULT_READ_RPM,
        DEFAULT_HEARTBEAT_RPM,
    )


def test_env_unset_keeps_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RPM_NAMES:
        monkeypatch.delenv(f"BERNSTEIN_RATE_LIMIT_{name}_RPM", raising=False)
    mw = RequestRateLimitMiddleware(FastAPI())
    assert (mw._write_rpm, mw._read_rpm, mw._heartbeat_rpm) == (30, 300, 600)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "template",
    [
        "/cluster/nodes/{}/heartbeat",
        "/api/v1/cluster/nodes/{}/heartbeat",
        "/agents/{}/heartbeat",
        "/api/v1/agents/{}/heartbeat",
    ],
)
async def test_heartbeat_bucket_keyed_by_id(template: str) -> None:
    app = _limited_app(heartbeat_rpm=2, write_rpm=1)
    creds = {"Authorization": "Bearer test-token"}
    transport = ASGITransport(app=app, client=("198.51.100.50", 1))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Same id: throttled after heartbeat_rpm (independent of write_rpm=1).
        assert (await c.post(template.format("n1"), headers=creds)).status_code == 200
        assert (await c.post(template.format("n1"), headers=creds)).status_code == 200
        third = await c.post(template.format("n1"), headers=creds)
        assert third.status_code == 429
        assert third.json()["bucket"] == "heartbeat"
        # Different id, same IP: its own budget.
        assert (await c.post(template.format("n2"), headers=creds)).status_code == 200
        assert (await c.post(template.format("n2"), headers=creds)).status_code == 200


@pytest.mark.anyio
async def test_anonymous_heartbeat_keyed_by_ip() -> None:
    app = _limited_app(heartbeat_rpm=2)
    transport = ASGITransport(app=app, client=("198.51.100.51", 1))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/cluster/nodes/a/heartbeat")).status_code == 200
        assert (await c.post("/cluster/nodes/b/heartbeat")).status_code == 200
        # Rotating ids without credentials does not mint new buckets.
        assert (await c.post("/cluster/nodes/c/heartbeat")).status_code == 429


@pytest.mark.parametrize("status", [404, 410])
def test_heartbeat_client_reregisters_on_unknown_node(status: int) -> None:
    import httpx
    from bernstein.core.cluster import NodeHeartbeatClient

    hb = NodeHeartbeatClient(server_url="http://central")
    hb._node_id = "stale"
    hb._registered.set()
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": "x"}))
    with httpx.Client(transport=transport) as client:
        assert hb._send_heartbeat(client) is False
    assert hb.node_id is None
    assert not hb._registered.is_set()
