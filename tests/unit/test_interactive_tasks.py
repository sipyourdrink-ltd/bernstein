"""Tests for interactive task assignment endpoints (#350).

Covers POST /tasks/{id}/force-claim, POST /tasks/{id}/prioritize,
and PATCH /tasks/{id} with model field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

TASK_PAYLOAD = {
    "title": "Add auth middleware",
    "description": "Implement JWT auth middleware",
    "role": "backend",
    "priority": 2,
}


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


async def _create_task(client: AsyncClient, **overrides: object) -> dict:  # type: ignore[type-arg]
    payload = {**TASK_PAYLOAD, **overrides}
    resp = await client.post("/tasks", json=payload)
    assert resp.status_code == 201
    return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# POST /tasks/{id}/prioritize
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_prioritize_sets_priority_zero(client: AsyncClient) -> None:
    task = await _create_task(client, priority=3)
    assert task["priority"] == 3

    resp = await client.post(f"/tasks/{task['id']}/prioritize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["priority"] == 0
    assert data["id"] == task["id"]


@pytest.mark.anyio
async def test_prioritize_unknown_task_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/tasks/nonexistent/prioritize")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_prioritize_increments_version(client: AsyncClient) -> None:
    task = await _create_task(client)
    original_version = task["version"]

    resp = await client.post(f"/tasks/{task['id']}/prioritize")
    assert resp.status_code == 200
    assert resp.json()["version"] == original_version + 1


# ---------------------------------------------------------------------------
# POST /tasks/{id}/force-claim
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_force_claim_open_task_sets_priority_zero(client: AsyncClient) -> None:
    task = await _create_task(client, priority=5)

    resp = await client.post(f"/tasks/{task['id']}/force-claim")
    assert resp.status_code == 200
    data = resp.json()
    assert data["priority"] == 0
    assert data["status"] == "open"


@pytest.mark.anyio
async def test_force_claim_claimed_task_resets_to_open(client: AsyncClient) -> None:
    task = await _create_task(client)
    # Claim the task
    claim_resp = await client.post(f"/tasks/{task['id']}/claim")
    assert claim_resp.status_code == 200
    assert claim_resp.json()["status"] == "claimed"

    # Force-claim resets to open with priority 0
    resp = await client.post(f"/tasks/{task['id']}/force-claim")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "open"
    assert data["priority"] == 0


@pytest.mark.anyio
async def test_force_claim_done_task_returns_409(client: AsyncClient) -> None:
    task = await _create_task(client)
    await client.post(f"/tasks/{task['id']}/claim")
    await client.post(f"/tasks/{task['id']}/complete", json={"result_summary": "done"})

    resp = await client.post(f"/tasks/{task['id']}/force-claim")
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_force_claim_failed_task_returns_409(client: AsyncClient) -> None:
    task = await _create_task(client)
    await client.post(f"/tasks/{task['id']}/claim")
    await client.post(f"/tasks/{task['id']}/fail", json={"reason": "error"})

    resp = await client.post(f"/tasks/{task['id']}/force-claim")
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_force_claim_unknown_task_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/tasks/nonexistent/force-claim")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /tasks/{id} — model field
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_patch_task_model(client: AsyncClient) -> None:
    task = await _create_task(client)

    resp = await client.patch(f"/tasks/{task['id']}", json={"model": "opus"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "opus"
    assert data["id"] == task["id"]


@pytest.mark.anyio
async def test_patch_task_role_and_model(client: AsyncClient) -> None:
    task = await _create_task(client)

    resp = await client.patch(f"/tasks/{task['id']}", json={"role": "qa", "model": "haiku"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "qa"
    assert data["model"] == "haiku"


@pytest.mark.anyio
async def test_patch_task_priority(client: AsyncClient) -> None:
    task = await _create_task(client, priority=3)

    resp = await client.patch(f"/tasks/{task['id']}", json={"priority": 1})
    assert resp.status_code == 200
    assert resp.json()["priority"] == 1


@pytest.mark.anyio
async def test_patch_task_unknown_returns_404(client: AsyncClient) -> None:
    resp = await client.patch("/tasks/nonexistent", json={"model": "haiku"})
    assert resp.status_code == 404
