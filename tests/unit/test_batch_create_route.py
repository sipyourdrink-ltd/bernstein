"""Tests for POST /tasks/batch — atomic batch task creation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

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


def _task_payload(title: str, role: str = "backend") -> dict[str, str]:
    return {"title": title, "description": f"Do {title}", "role": role}


@pytest.mark.anyio
async def test_batch_create_all_succeed(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """POST /tasks/batch with 3 distinct tasks creates all of them."""
    # Patch create_batch on the store since it may not exist yet
    store = app.state.store

    async def _fake_create_batch(
        requests: list,  # type: ignore[type-arg]
        *,
        dedup_by_title: bool = True,
    ) -> tuple[list, list[str]]:  # type: ignore[type-arg]
        tasks = []
        for req in requests:
            task = await store.create(req)
            tasks.append(task)
        return tasks, []

    with patch.object(store, "create_batch", side_effect=_fake_create_batch, create=True):
        resp = await client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    _task_payload("Task A"),
                    _task_payload("Task B"),
                    _task_payload("Task C"),
                ]
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert len(data["created"]) == 3
    assert data["skipped_titles"] == []
    titles = {t["title"] for t in data["created"]}
    assert titles == {"Task A", "Task B", "Task C"}


@pytest.mark.anyio
async def test_batch_create_dedup_within_batch(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """POST /tasks/batch with duplicate titles: 1 created, 1 skipped."""
    store = app.state.store

    async def _fake_create_batch(
        requests: list,  # type: ignore[type-arg]
        *,
        dedup_by_title: bool = True,
    ) -> tuple[list, list[str]]:  # type: ignore[type-arg]
        seen_titles: set[str] = set()
        tasks = []
        skipped: list[str] = []
        for req in requests:
            if dedup_by_title and req.title in seen_titles:
                skipped.append(req.title)
                continue
            seen_titles.add(req.title)
            task = await store.create(req)
            tasks.append(task)
        return tasks, skipped

    with patch.object(store, "create_batch", side_effect=_fake_create_batch, create=True):
        resp = await client.post(
            "/tasks/batch",
            json={
                "tasks": [
                    _task_payload("Duplicate Task"),
                    _task_payload("Duplicate Task"),
                ]
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert len(data["created"]) == 1
    assert data["created"][0]["title"] == "Duplicate Task"
    assert data["skipped_titles"] == ["Duplicate Task"]


@pytest.mark.anyio
async def test_batch_create_empty_list(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """POST /tasks/batch with empty tasks list returns empty response."""
    store = app.state.store

    async def _fake_create_batch(
        requests: list,  # type: ignore[type-arg]
        *,
        dedup_by_title: bool = True,
    ) -> tuple[list, list[str]]:  # type: ignore[type-arg]
        return [], []

    with patch.object(store, "create_batch", side_effect=_fake_create_batch, create=True):
        resp = await client.post("/tasks/batch", json={"tasks": []})

    assert resp.status_code == 201
    data = resp.json()
    assert data["created"] == []
    assert data["skipped_titles"] == []


@pytest.mark.anyio
async def test_batch_create_while_draining(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """POST /tasks/batch returns 503 when the server is draining."""
    app.state.draining = True
    resp = await client.post(
        "/tasks/batch",
        json={"tasks": [_task_payload("Should fail")]},
    )
    assert resp.status_code == 503
