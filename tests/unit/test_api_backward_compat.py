"""API backward compatibility tests for the Bernstein task server.

Verifies that core HTTP contract guarantees hold:
- POST /tasks returns 201 with required fields
- GET /tasks?status= filters correctly
- POST /tasks/{id}/complete returns updated status
- POST /tasks/{id}/fail marks task as failed
- GET /tasks/{id} returns 404 for unknown ids
- POST /tasks/{id}/claim returns claimed task
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
    """Create a fresh in-memory task server for each test."""
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(
    client: AsyncClient,
    *,
    title: str = "test task",
    role: str = "backend",
    description: str = "task description",
) -> dict:  # type: ignore[type-arg]
    """Helper: create a task and return the response JSON."""
    resp = await client.post(
        "/tasks",
        json={"title": title, "description": description, "role": role},
    )
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    return dict(resp.json())


# ---------------------------------------------------------------------------
# POST /tasks — task creation contract
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_task_returns_201(client: AsyncClient) -> None:
    """POST /tasks returns HTTP 201 Created."""
    resp = await client.post(
        "/tasks",
        json={"title": "My task", "description": "do the thing", "role": "qa"},
    )
    assert resp.status_code == 201


@pytest.mark.anyio
async def test_create_task_response_has_required_fields(client: AsyncClient) -> None:
    """POST /tasks response includes id, title, status, and role."""
    data = await _create_task(client, title="field-check", role="backend")
    assert "id" in data
    assert data["title"] == "field-check"
    assert data["role"] == "backend"
    assert data["status"] == "open"


@pytest.mark.anyio
async def test_create_task_assigns_unique_ids(client: AsyncClient) -> None:
    """Each POST /tasks call returns a distinct task id."""
    t1 = await _create_task(client, title="first")
    t2 = await _create_task(client, title="second")
    assert t1["id"] != t2["id"]


# ---------------------------------------------------------------------------
# GET /tasks — list and filter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_tasks_returns_created_task(client: AsyncClient) -> None:
    """GET /tasks returns tasks that were created."""
    created = await _create_task(client, title="list-me")
    task_id = created["id"]

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert task_id in ids


@pytest.mark.anyio
async def test_list_tasks_filter_by_status_open(client: AsyncClient) -> None:
    """GET /tasks?status=open returns only open tasks."""
    await _create_task(client, title="open-task")

    resp = await client.get("/tasks", params={"status": "open"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 1
    for t in tasks:
        assert t["status"] == "open"


# ---------------------------------------------------------------------------
# GET /tasks/{id} — single task fetch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_task_by_id(client: AsyncClient) -> None:
    """GET /tasks/{id} returns the task with matching id."""
    created = await _create_task(client, title="get-me")
    task_id = created["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == task_id


@pytest.mark.anyio
async def test_get_task_returns_404_for_unknown_id(client: AsyncClient) -> None:
    """GET /tasks/{id} returns HTTP 404 for a non-existent task id."""
    resp = await client.get("/tasks/does-not-exist-abc123")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tasks/{id}/claim — task claiming
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_task_sets_status_to_claimed(client: AsyncClient) -> None:
    """POST /tasks/{id}/claim returns the task with status 'claimed'."""
    created = await _create_task(client, title="claim-me")
    task_id = created["id"]

    resp = await client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 200
    assert resp.json()["status"] == "claimed"


# ---------------------------------------------------------------------------
# POST /tasks/{id}/complete — task completion
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_complete_task_sets_status_done(client: AsyncClient) -> None:
    """POST /tasks/{id}/complete transitions status to 'done'."""
    created = await _create_task(client, title="complete-me")
    task_id = created["id"]

    # Must claim before completing
    await client.post(f"/tasks/{task_id}/claim")

    resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "done successfully"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


@pytest.mark.anyio
async def test_complete_task_stores_result_summary(client: AsyncClient) -> None:
    """POST /tasks/{id}/complete preserves the result_summary in the response."""
    created = await _create_task(client, title="result-check")
    task_id = created["id"]
    await client.post(f"/tasks/{task_id}/claim")

    summary = "All tests green, merged PR #42"
    resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": summary},
    )
    assert resp.status_code == 200
    assert resp.json()["result_summary"] == summary


# ---------------------------------------------------------------------------
# POST /tasks/{id}/fail — task failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fail_task_sets_status_failed(client: AsyncClient) -> None:
    """POST /tasks/{id}/fail transitions status to 'failed'."""
    created = await _create_task(client, title="fail-me")
    task_id = created["id"]
    await client.post(f"/tasks/{task_id}/claim")

    resp = await client.post(
        f"/tasks/{task_id}/fail",
        json={"reason": "timed out"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


# ---------------------------------------------------------------------------
# GET /status — server health
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_status_endpoint_returns_200(client: AsyncClient) -> None:
    """GET /status returns HTTP 200 and a JSON summary."""
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    # The response must include at least a task count field
    assert "open" in data or "tasks" in data or "total" in str(data)
