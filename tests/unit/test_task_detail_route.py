"""WEB-012: Tests for dashboard task detail view and log streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestTaskDetail:
    """Test GET /dashboard/tasks/{task_id}."""

    @pytest.mark.anyio()
    async def test_detail_not_found(self, client: AsyncClient) -> None:
        """Non-existent task should return 404."""
        resp = await client.get("/dashboard/tasks/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.anyio()
    async def test_detail_exists(self, client: AsyncClient) -> None:
        """Created task should be viewable in detail."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Detail test", "description": "Testing detail view", "role": "backend"},
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/dashboard/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"]["id"] == task_id
        assert data["task"]["title"] == "Detail test"
        assert "log_tail" in data
        assert "log_size" in data

    @pytest.mark.anyio()
    async def test_detail_includes_progress(self, client: AsyncClient) -> None:
        """Detail response should include progress entries."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Progress test", "description": "With progress", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/dashboard/tasks/{task_id}")
        data = resp.json()
        assert "progress_entries" in data
        assert isinstance(data["progress_entries"], list)


class TestTaskLogStream:
    """Test GET /dashboard/tasks/{task_id}/logs/stream."""

    @pytest.mark.anyio()
    async def test_stream_not_found(self, client: AsyncClient) -> None:
        """Non-existent task should return 404."""
        resp = await client.get("/dashboard/tasks/nonexistent/logs/stream")
        assert resp.status_code == 404

    @pytest.mark.anyio()
    async def test_stream_returns_sse(self, client: AsyncClient) -> None:
        """Stream endpoint should return text/event-stream."""
        create_resp = await client.post(
            "/tasks",
            json={"title": "Stream test", "description": "SSE stream", "role": "backend"},
        )
        task_id = create_resp.json()["id"]

        # Complete the task so the stream ends quickly
        await client.post(f"/tasks/{task_id}/claim", json={"agent_id": "test-agent"})
        await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"})

        resp = await client.get(f"/dashboard/tasks/{task_id}/logs/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
