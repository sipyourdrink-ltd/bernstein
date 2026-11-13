"""Tests for normalized UI sections in GET /status."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


@pytest.mark.anyio
async def test_status_contains_normalized_sections(app) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/tasks",
            json={"title": "Fix auth", "description": "x", "role": "backend", "priority": 1},
        )
        resp = await client.get("/status")

    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "tasks" in data
    assert "agents" in data
    assert "runtime" in data
    assert "costs" in data
    assert "alerts" in data
    assert "bandit" in data
    assert data["tasks"]["items"][0]["title"] == "Fix auth"


@pytest.mark.anyio
async def test_status_tasks_items_include_tui_fields(app) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={
                "title": "Wire search",
                "description": "x",
                "role": "backend",
                "priority": 2,
                "depends_on": [],
            },
        )
        task_id = created.json()["id"]
        resp = await client.get("/status")

    assert resp.status_code == 200
    items = resp.json()["tasks"]["items"]
    task_item = next(item for item in items if item["id"] == task_id)
    assert task_item["priority"] == 2
    assert "elapsed" in task_item
    assert "assigned_agent" in task_item
    assert "blocked_reason" in task_item
    assert "progress" in task_item


@pytest.mark.anyio
@pytest.mark.parametrize(("mode", "reason"), [(None, None), ("draining", "draining"), ("readonly", "readonly_mode")])
async def test_status_reports_coordinator_evidence_without_inferred_dispatch(
    app, mode: str | None, reason: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bernstein.core.routes.status_dashboard.__version__", "9.8.7", raising=False)
    if mode:
        setattr(app.state, mode, True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    evidence = response.json()["execution_readiness"]
    assert evidence["schema_version"] == 1
    assert evidence["coordinator"]["version"] == "9.8.7"
    assert evidence["coordinator"]["source"] == "task_server_status"
    assert evidence["coordinator"]["provenance"] == {"tenant_id": "default"}
    assert evidence["scheduler"]["state"] == "unknown"
    assert evidence["dispatch"]["state"] == ("blocked" if mode == "draining" else "unknown")
    assert evidence["dispatch"]["reason_codes"] == [reason or "dispatch_evidence_unavailable"]
    assert response.json()["summary"]["total"] == 0
