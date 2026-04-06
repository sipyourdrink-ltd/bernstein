"""WEB-007: Tests for API versioning under /api/v1/."""

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


class TestAPIVersioning:
    """Test /api/v1/ route prefix."""

    @pytest.mark.anyio()
    async def test_v1_tasks_endpoint(self, client: AsyncClient) -> None:
        """GET /api/v1/tasks should work alongside GET /tasks."""
        resp = await client.get("/api/v1/tasks")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_legacy_tasks_still_works(self, client: AsyncClient) -> None:
        """Legacy GET /tasks should still respond."""
        resp = await client.get("/tasks")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_health_deps(self, client: AsyncClient) -> None:
        """GET /api/v1/health/deps should return health info."""
        resp = await client.get("/api/v1/health/deps")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "dependencies" in data

    @pytest.mark.anyio()
    async def test_v1_status_endpoint(self, client: AsyncClient) -> None:
        """GET /api/v1/status should return status data."""
        resp = await client.get("/api/v1/status")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_grafana_dashboard(self, client: AsyncClient) -> None:
        """GET /api/v1/grafana/dashboard should return dashboard JSON."""
        resp = await client.get("/api/v1/grafana/dashboard")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_export_tasks(self, client: AsyncClient) -> None:
        """GET /api/v1/export/tasks should work."""
        resp = await client.get("/api/v1/export/tasks")
        assert resp.status_code == 200
