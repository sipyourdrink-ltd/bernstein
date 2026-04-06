"""WEB-008: Tests for data export endpoints."""

from __future__ import annotations

import csv
import io
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


class TestExportTasks:
    """Test GET /export/tasks endpoint."""

    @pytest.mark.anyio()
    async def test_export_tasks_json_empty(self, client: AsyncClient) -> None:
        """Export tasks as JSON when store is empty."""
        resp = await client.get("/export/tasks?format=json")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        data = json.loads(resp.text)
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.anyio()
    async def test_export_tasks_csv_empty(self, client: AsyncClient) -> None:
        """Export tasks as CSV when store is empty."""
        resp = await client.get("/export/tasks?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) == 0
        # Check CSV header is present
        assert "id" in (reader.fieldnames or [])

    @pytest.mark.anyio()
    async def test_export_tasks_json_with_data(self, client: AsyncClient) -> None:
        """Export tasks as JSON after creating a task."""
        # Create a task first
        create_resp = await client.post(
            "/tasks",
            json={"title": "Test task", "description": "For export", "role": "backend"},
        )
        assert create_resp.status_code == 201

        resp = await client.get("/export/tasks?format=json")
        assert resp.status_code == 200
        data = json.loads(resp.text)
        assert len(data) >= 1
        assert data[0]["title"] == "Test task"

    @pytest.mark.anyio()
    async def test_export_tasks_csv_with_data(self, client: AsyncClient) -> None:
        """Export tasks as CSV after creating a task."""
        await client.post(
            "/tasks",
            json={"title": "CSV task", "description": "For CSV export", "role": "qa"},
        )

        resp = await client.get("/export/tasks?format=csv")
        assert resp.status_code == 200
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) >= 1
        assert rows[0]["title"] == "CSV task"

    @pytest.mark.anyio()
    async def test_export_tasks_default_format_is_json(self, client: AsyncClient) -> None:
        """Default format should be JSON."""
        resp = await client.get("/export/tasks")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"


class TestExportAgents:
    """Test GET /export/agents endpoint."""

    @pytest.mark.anyio()
    async def test_export_agents_json_empty(self, client: AsyncClient) -> None:
        """Export agents as JSON when no agents file exists."""
        resp = await client.get("/export/agents?format=json")
        assert resp.status_code == 200
        data = json.loads(resp.text)
        assert isinstance(data, list)

    @pytest.mark.anyio()
    async def test_export_agents_csv_empty(self, client: AsyncClient) -> None:
        """Export agents as CSV with no agents file."""
        resp = await client.get("/export/agents?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    @pytest.mark.anyio()
    async def test_export_agents_with_snapshot(self, client: AsyncClient, app: FastAPI) -> None:
        """Export agents from a pre-populated agents.json snapshot."""
        sdd_dir = getattr(app.state, "sdd_dir", None)
        if sdd_dir is not None:
            runtime_dir = Path(str(sdd_dir)) / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            agents_data = {
                "agents": [
                    {"id": "agent-1", "role": "backend", "status": "working", "task_id": "t1", "started_at": 123.0}
                ]
            }
            (runtime_dir / "agents.json").write_text(json.dumps(agents_data))

            resp = await client.get("/export/agents?format=json")
            assert resp.status_code == 200
            data = json.loads(resp.text)
            assert len(data) == 1
            assert data[0]["id"] == "agent-1"
