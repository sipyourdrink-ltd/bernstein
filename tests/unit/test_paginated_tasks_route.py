"""WEB-011: Tests for paginated task list with sorting/filtering."""

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


async def _create_tasks(client: AsyncClient, count: int) -> list[str]:
    """Create N tasks and return their IDs."""
    ids: list[str] = []
    for i in range(count):
        resp = await client.post(
            "/tasks",
            json={
                "title": f"Task {i}",
                "description": f"Description {i}",
                "role": "backend" if i % 2 == 0 else "qa",
                "priority": (i % 3) + 1,
            },
        )
        assert resp.status_code == 201
        ids.append(resp.json()["id"])
    return ids


class TestPaginatedTaskSearch:
    """Test GET /tasks/search endpoint."""

    @pytest.mark.anyio()
    async def test_empty_search(self, client: AsyncClient) -> None:
        """Search with no tasks returns empty page."""
        resp = await client.get("/tasks/search")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["tasks"] == []
        assert data["page"] == 1
        assert data["per_page"] == 20

    @pytest.mark.anyio()
    async def test_pagination(self, client: AsyncClient) -> None:
        """Pagination splits results correctly."""
        await _create_tasks(client, 5)

        resp = await client.get("/tasks/search?page=1&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["tasks"]) == 2
        assert data["page"] == 1
        assert data["total_pages"] == 3

    @pytest.mark.anyio()
    async def test_page_2(self, client: AsyncClient) -> None:
        """Second page should return next set of results."""
        await _create_tasks(client, 5)

        resp = await client.get("/tasks/search?page=2&per_page=2")
        data = resp.json()
        assert len(data["tasks"]) == 2
        assert data["page"] == 2

    @pytest.mark.anyio()
    async def test_filter_by_role(self, client: AsyncClient) -> None:
        """Filter by role should return only matching tasks."""
        await _create_tasks(client, 4)

        resp = await client.get("/tasks/search?role=backend")
        data = resp.json()
        for task in data["tasks"]:
            assert task["role"] == "backend"

    @pytest.mark.anyio()
    async def test_sort_by_priority_asc(self, client: AsyncClient) -> None:
        """Sort by priority ascending."""
        await _create_tasks(client, 4)

        resp = await client.get("/tasks/search?sort=priority&order=asc")
        data = resp.json()
        priorities = [t["priority"] for t in data["tasks"]]
        assert priorities == sorted(priorities)

    @pytest.mark.anyio()
    async def test_sort_by_created_at_desc(self, client: AsyncClient) -> None:
        """Default sort by created_at descending."""
        await _create_tasks(client, 3)

        resp = await client.get("/tasks/search?sort=created_at&order=desc")
        data = resp.json()
        timestamps = [t["created_at"] for t in data["tasks"]]
        assert timestamps == sorted(timestamps, reverse=True)

    @pytest.mark.anyio()
    async def test_per_page_clamped(self, client: AsyncClient) -> None:
        """per_page above 100 should be clamped."""
        resp = await client.get("/tasks/search?per_page=999")
        data = resp.json()
        assert data["per_page"] == 100

    @pytest.mark.anyio()
    async def test_invalid_sort_falls_back(self, client: AsyncClient) -> None:
        """Invalid sort field falls back to created_at."""
        resp = await client.get("/tasks/search?sort=nonexistent")
        data = resp.json()
        assert data["sort"] == "created_at"

    @pytest.mark.anyio()
    async def test_filters_in_response(self, client: AsyncClient) -> None:
        """Applied filters should appear in the response metadata."""
        resp = await client.get("/tasks/search?status=open&role=backend")
        data = resp.json()
        assert data["filters"]["status"] == "open"
        assert data["filters"]["role"] == "backend"
