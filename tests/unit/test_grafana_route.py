"""WEB-009: Tests for Grafana dashboard endpoint."""

from __future__ import annotations

import json
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


class TestGrafanaRoute:
    """Test GET /grafana/dashboard endpoint."""

    @pytest.mark.anyio()
    async def test_grafana_dashboard_returns_json(self, client: AsyncClient) -> None:
        """Endpoint should return valid Grafana dashboard JSON."""
        resp = await client.get("/grafana/dashboard")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"

        data = json.loads(resp.text)
        assert "dashboard" in data
        assert data["dashboard"]["title"] == "Bernstein Orchestration Metrics"

    @pytest.mark.anyio()
    async def test_grafana_custom_datasource(self, client: AsyncClient) -> None:
        """Custom datasource should be accepted."""
        resp = await client.get("/grafana/dashboard?datasource=MyProm")
        assert resp.status_code == 200
        data = json.loads(resp.text)
        # Datasource should appear in templating
        template_list = data["dashboard"]["templating"]["list"]
        assert any(t["datasource"] == "MyProm" for t in template_list)

    @pytest.mark.anyio()
    async def test_grafana_has_panels(self, client: AsyncClient) -> None:
        """Dashboard should have panels."""
        resp = await client.get("/grafana/dashboard")
        data = json.loads(resp.text)
        panels = data["dashboard"]["panels"]
        assert len(panels) >= 4
        # Verify panel IDs are unique
        ids = [p["id"] for p in panels]
        assert len(ids) == len(set(ids))

    @pytest.mark.anyio()
    async def test_grafana_content_disposition(self, client: AsyncClient) -> None:
        """Response should have a download-friendly content-disposition."""
        resp = await client.get("/grafana/dashboard")
        assert "content-disposition" in resp.headers
        assert "bernstein-dashboard.json" in resp.headers["content-disposition"]
