"""Tests for POST /tasks/self-create — agent-initiated subtask creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_parent(client: AsyncClient) -> dict:  # type: ignore[type-arg]
    resp = await client.post(
        "/tasks",
        json={"title": "parent task", "description": "the parent", "role": "backend"},
    )
    assert resp.status_code == 201
    return dict(resp.json())


@pytest.mark.anyio
async def test_self_create_subtask_appears_in_backlog(client: AsyncClient) -> None:
    """Agent creates subtask via self-create and it appears in backlog with correct parent."""
    parent = await _create_parent(client)

    resp = await client.post(
        "/tasks/self-create",
        json={
            "parent_task_id": parent["id"],
            "title": "subtask one",
            "description": "do the sub-thing",
            "role": "backend",
        },
    )
    assert resp.status_code == 201
    subtask = resp.json()
    assert subtask["parent_task_id"] == parent["id"]
    assert subtask["title"] == "subtask one"
    assert subtask["status"] == "open"

    # Subtask appears in the full task list
    list_resp = await client.get("/tasks")
    assert list_resp.status_code == 200
    task_ids = [t["id"] for t in list_resp.json()]
    assert subtask["id"] in task_ids


@pytest.mark.anyio
async def test_self_create_transitions_parent_to_waiting(client: AsyncClient) -> None:
    """Creating a subtask transitions the parent to waiting_for_subtasks."""
    parent = await _create_parent(client)

    # Claim the parent first so it can transition
    await client.post(f"/tasks/{parent['id']}/claim")

    resp = await client.post(
        "/tasks/self-create",
        json={
            "parent_task_id": parent["id"],
            "title": "child work",
            "description": "decomposed piece",
            "role": "qa",
        },
    )
    assert resp.status_code == 201

    # Parent should now be waiting
    parent_resp = await client.get(f"/tasks/{parent['id']}")
    assert parent_resp.status_code == 200
    assert parent_resp.json()["status"] == "waiting_for_subtasks"


@pytest.mark.anyio
async def test_self_create_rejects_missing_parent(client: AsyncClient) -> None:
    """Self-create with nonexistent parent returns 404."""
    resp = await client.post(
        "/tasks/self-create",
        json={
            "parent_task_id": "nonexistent123",
            "title": "orphan",
            "description": "no parent",
            "role": "backend",
        },
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_self_create_multiple_subtasks(client: AsyncClient) -> None:
    """Agent can create multiple subtasks under the same parent."""
    parent = await _create_parent(client)
    await client.post(f"/tasks/{parent['id']}/claim")

    for i in range(3):
        resp = await client.post(
            "/tasks/self-create",
            json={
                "parent_task_id": parent["id"],
                "title": f"subtask {i}",
                "description": f"piece {i}",
                "role": "backend",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["parent_task_id"] == parent["id"]

    # All 3 subtasks + parent = 4 tasks total
    list_resp = await client.get("/tasks")
    assert len(list_resp.json()) == 4
