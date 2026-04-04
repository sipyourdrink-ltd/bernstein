"""Tests for task claiming scoped by parent_session_id.

Verifies that workers bound to a coordinator session only claim tasks that
belong to that coordinator's namespace, never stealing from other sessions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task_with_session(
    client: AsyncClient,
    *,
    title: str,
    role: str = "backend",
    parent_session_id: str | None = None,
) -> dict:  # type: ignore[type-arg]
    body: dict = {"title": title, "description": "desc", "role": role}
    if parent_session_id is not None:
        body["parent_session_id"] = parent_session_id
    resp = await client.post("/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# ---------------------------------------------------------------------------
# Task creation preserves parent_session_id
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_task_stores_parent_session_id(client: AsyncClient) -> None:
    """parent_session_id is persisted on the created task."""
    task = await _create_task_with_session(client, title="t1", parent_session_id="coord-abc")
    assert task["parent_session_id"] == "coord-abc"


@pytest.mark.anyio
async def test_create_task_without_parent_session_id(client: AsyncClient) -> None:
    """Tasks without parent_session_id have it as None."""
    task = await _create_task_with_session(client, title="t1")
    assert task.get("parent_session_id") is None


# ---------------------------------------------------------------------------
# GET /tasks?parent_session_id= filtering
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_tasks_filters_by_parent_session_id(client: AsyncClient) -> None:
    """GET /tasks?parent_session_id= returns only matching tasks."""
    await _create_task_with_session(client, title="coord-A-task", parent_session_id="coord-A")
    await _create_task_with_session(client, title="coord-B-task", parent_session_id="coord-B")
    await _create_task_with_session(client, title="unscoped")

    resp = await client.get("/tasks?parent_session_id=coord-A")
    assert resp.status_code == 200
    tasks = resp.json()
    titles = [t["title"] for t in tasks]
    assert "coord-A-task" in titles
    assert "coord-B-task" not in titles
    assert "unscoped" not in titles


@pytest.mark.anyio
async def test_list_tasks_without_filter_returns_all(client: AsyncClient) -> None:
    """GET /tasks without parent_session_id filter returns all tasks."""
    await _create_task_with_session(client, title="coord-A-task", parent_session_id="coord-A")
    await _create_task_with_session(client, title="unscoped")

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2


# ---------------------------------------------------------------------------
# GET /tasks/next/{role}?parent_session_id= — scoped claiming
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_next_task_scoped_by_parent_session_id(client: AsyncClient) -> None:
    """Workers with parent_session_id only claim matching tasks."""
    await _create_task_with_session(client, title="coord-A-task", role="backend", parent_session_id="coord-A")
    await _create_task_with_session(client, title="coord-B-task", role="backend", parent_session_id="coord-B")

    # coord-A worker should claim coord-A task, not coord-B
    resp = await client.get("/tasks/next/backend?parent_session_id=coord-A")
    assert resp.status_code == 200
    claimed = resp.json()
    assert claimed["title"] == "coord-A-task"
    assert claimed["status"] == "claimed"


@pytest.mark.anyio
async def test_next_task_scoped_returns_404_when_no_match(client: AsyncClient) -> None:
    """GET /tasks/next/{role}?parent_session_id= returns 404 when no matching task exists."""
    await _create_task_with_session(client, title="coord-B-task", role="backend", parent_session_id="coord-B")

    resp = await client.get("/tasks/next/backend?parent_session_id=coord-A")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_next_task_without_scope_claims_any_open(client: AsyncClient) -> None:
    """GET /tasks/next/{role} without parent_session_id claims any open task."""
    await _create_task_with_session(client, title="scoped", role="backend", parent_session_id="coord-X")
    await _create_task_with_session(client, title="unscoped", role="backend")

    resp = await client.get("/tasks/next/backend")
    assert resp.status_code == 200
    claimed = resp.json()
    # Without scope filter, can claim either task; just verify one was claimed
    assert claimed["status"] == "claimed"


@pytest.mark.anyio
async def test_workers_from_different_coordinators_dont_steal(client: AsyncClient) -> None:
    """Two workers with different coordinator IDs cannot steal each other's tasks."""
    await _create_task_with_session(client, title="coord-A-task-1", role="backend", parent_session_id="coord-A")
    await _create_task_with_session(client, title="coord-A-task-2", role="backend", parent_session_id="coord-A")
    await _create_task_with_session(client, title="coord-B-task", role="backend", parent_session_id="coord-B")

    # coord-B worker claims from coord-B namespace
    resp_b = await client.get("/tasks/next/backend?parent_session_id=coord-B")
    assert resp_b.status_code == 200
    assert resp_b.json()["title"] == "coord-B-task"

    # coord-A worker claims from coord-A namespace — coord-B task is not touched
    resp_a = await client.get("/tasks/next/backend?parent_session_id=coord-A")
    assert resp_a.status_code == 200
    assert resp_a.json()["title"] in ("coord-A-task-1", "coord-A-task-2")
