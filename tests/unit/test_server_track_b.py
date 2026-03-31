"""Track B server-route tests for runtime metadata endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


def _make_app(tmp_path: Path) -> FastAPI:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path)


@pytest.mark.anyio
async def test_health_reports_restart_count_and_memory(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    runtime_dir = Path(app_state.sdd_dir) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "supervisor_state.json").write_text(
        json.dumps(
            {
                "started_at": 1.0,
                "restart_count": 3,
                "current_pid": 1234,
                "last_restart_at": 2.0,
            }
        ),
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["restart_count"] == 3
    assert "memory_mb" in payload


@pytest.mark.anyio
async def test_status_includes_runtime_summary_block(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        json.dumps(
            {
                "task_id": "task-123",
                "title": "Fix auth flow",
                "role": "backend",
                "status": "done",
                "created_at": 1.0,
                "completed_at": 2.0,
                "duration_seconds": 1.0,
                "result_summary": "done",
                "cost_usd": None,
                "assigned_agent": "sess-123",
                "owned_files": ["src/auth.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    app_state.store._archive_path = archive_path  # pyright: ignore[reportPrivateUsage]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert "active_worktrees" in runtime
    assert "disk_usage_mb" in runtime
    assert runtime["last_completed"]["task_id"] == "task-123"


@pytest.mark.anyio
async def test_routing_bandit_invalid_state_returns_generic_error(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    routing_dir = Path(app_state.store.jsonl_path).parent.parent / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / "bandit_state.json").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/routing/bandit")

    assert response.status_code == 500
    assert response.json() == {
        "mode": "bandit",
        "active": True,
        "error": "Failed to read routing bandit state",
    }


@pytest.mark.anyio
async def test_cache_stats_invalid_manifest_hides_parse_details(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    caching_dir = Path(app_state.sdd_dir) / "caching"
    caching_dir.mkdir(parents=True, exist_ok=True)
    (caching_dir / "manifest.jsonl").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/cache-stats")

    assert response.status_code == 500
    assert response.json() == {"error": "Failed to read cache manifest"}


@pytest.mark.anyio
async def test_memory_audit_invalid_json_hides_parse_details(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    memory_dir = Path(app_state.sdd_dir) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "lessons.jsonl").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/memory/audit")

    assert response.status_code == 200
    assert response.json()["errors"] == ["Line 1: invalid JSON entry"]
