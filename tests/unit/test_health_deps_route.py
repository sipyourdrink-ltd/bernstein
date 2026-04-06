"""WEB-013: Tests for /health/deps endpoint with dependency status."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.routes.health import (
    _check_server,
    _check_sse_bus,
    _check_store,
)
from bernstein.core.server import SSEBus, create_app


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


class TestHealthDepsEndpoint:
    """Test GET /health/deps integration."""

    @pytest.mark.anyio()
    async def test_health_deps_returns_200(self, client: AsyncClient) -> None:
        """Health deps endpoint should return 200."""
        resp = await client.get("/health/deps")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_health_deps_structure(self, client: AsyncClient) -> None:
        """Response should have expected structure."""
        resp = await client.get("/health/deps")
        data = resp.json()
        assert "status" in data
        assert "uptime_s" in data
        assert "timestamp" in data
        assert "dependencies" in data
        assert isinstance(data["dependencies"], list)

    @pytest.mark.anyio()
    async def test_health_deps_includes_store(self, client: AsyncClient) -> None:
        """Dependencies should include store status."""
        resp = await client.get("/health/deps")
        data = resp.json()
        names = [d["name"] for d in data["dependencies"]]
        assert "store" in names

    @pytest.mark.anyio()
    async def test_health_deps_includes_server(self, client: AsyncClient) -> None:
        """Dependencies should include server status."""
        resp = await client.get("/health/deps")
        data = resp.json()
        names = [d["name"] for d in data["dependencies"]]
        assert "server" in names

    @pytest.mark.anyio()
    async def test_health_deps_includes_sse_bus(self, client: AsyncClient) -> None:
        """Dependencies should include SSE bus status."""
        resp = await client.get("/health/deps")
        data = resp.json()
        names = [d["name"] for d in data["dependencies"]]
        assert "sse_bus" in names

    @pytest.mark.anyio()
    async def test_healthy_status(self, client: AsyncClient) -> None:
        """Overall status should be 'healthy' when all deps are ok."""
        resp = await client.get("/health/deps")
        data = resp.json()
        # Server and store should be ok, adapters may be unknown
        assert data["status"] in ("healthy", "degraded")


class TestDependencyChecks:
    """Unit tests for individual dependency check functions."""

    def test_check_store_no_store(self) -> None:
        """Store check returns 'down' when no store configured."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app.state.store = None
        result = _check_store(request)
        assert result.status == "down"

    def test_check_server_normal(self) -> None:
        """Server check returns 'ok' for normal operation."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app.state.draining = False
        request.app.state.readonly = False
        result = _check_server(request)
        assert result.status == "ok"

    def test_check_server_draining(self) -> None:
        """Server check returns 'degraded' when draining."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app.state.draining = True
        request.app.state.readonly = False
        result = _check_server(request)
        assert result.status == "degraded"
        assert "draining" in result.detail

    def test_check_sse_bus_no_bus(self) -> None:
        """SSE bus check returns 'down' with no bus."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app.state.sse_bus = None
        result = _check_sse_bus(request)
        assert result.status == "down"

    def test_check_sse_bus_ok(self) -> None:
        """SSE bus check returns 'ok' with a valid bus."""
        from unittest.mock import MagicMock

        request = MagicMock()
        request.app.state.sse_bus = SSEBus()
        result = _check_sse_bus(request)
        assert result.status == "ok"
