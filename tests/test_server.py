"""Tests for completion_signals API round-trip.

Covers:
- POST /tasks with completion_signals stores them correctly
- GET /tasks/{id} returns completion_signals in response
- All 6 signal types accepted
- Empty completion_signals (backward compatibility)
- Invalid signal type is rejected (422)
- Signals visible in GET /tasks list
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):  # type: ignore[no-untyped-def]
    """Create a fresh FastAPI app per test."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


BASE_TASK = {
    "title": "Test task",
    "description": "A task for testing completion signals",
    "role": "qa",
}


# -- POST /tasks with completion_signals ------------------------------------


@pytest.mark.anyio
async def test_post_task_with_path_exists_signal_stored(client: AsyncClient) -> None:
    """POST /tasks with path_exists signal stores it and returns it."""
    resp = await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [
                {"type": "path_exists", "value": "src/foo.py"},
            ],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["completion_signals"] == [{"type": "path_exists", "value": "src/foo.py"}]


@pytest.mark.anyio
async def test_post_task_multiple_signals_stored(client: AsyncClient) -> None:
    """POST /tasks stores multiple completion_signals in order."""
    signals = [
        {"type": "path_exists", "value": "output.txt"},
        {"type": "test_passes", "value": "pytest tests/ -x"},
    ]
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": signals})
    assert resp.status_code == 201
    assert resp.json()["completion_signals"] == signals


# -- GET /tasks/{id} returns completion_signals ----------------------------


@pytest.mark.anyio
async def test_get_task_returns_completion_signals(client: AsyncClient) -> None:
    """GET /tasks/{id} returns the stored completion_signals."""
    create_resp = await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [
                {"type": "test_passes", "value": "pytest tests/ -x"},
            ],
        },
    )
    assert create_resp.status_code == 201
    task_id = create_resp.json()["id"]

    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["completion_signals"] == [
        {"type": "test_passes", "value": "pytest tests/ -x"},
    ]


# -- All 6 signal types ----------------------------------------------------


@pytest.mark.anyio
async def test_all_six_signal_types_accepted(client: AsyncClient) -> None:
    """POST /tasks accepts all 6 signal types and returns them in order."""
    signals = [
        {"type": "path_exists", "value": "src/foo.py"},
        {"type": "glob_exists", "value": "src/**/*.py"},
        {"type": "test_passes", "value": "pytest tests/ -x"},
        {"type": "file_contains", "value": "TODO: done"},
        {"type": "llm_review", "value": "Review the implementation"},
        {"type": "llm_judge", "value": "Is the feature complete?"},
    ]
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": signals})
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["completion_signals"]) == 6
    returned_types = [s["type"] for s in data["completion_signals"]]
    assert returned_types == [
        "path_exists",
        "glob_exists",
        "test_passes",
        "file_contains",
        "llm_review",
        "llm_judge",
    ]


@pytest.mark.anyio
async def test_path_exists_signal_roundtrip(client: AsyncClient) -> None:
    """path_exists signal survives full POST → GET round-trip."""
    signal = {"type": "path_exists", "value": "dist/bundle.js"}
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": [signal]})
    task_id = resp.json()["id"]
    assert (await client.get(f"/tasks/{task_id}")).json()["completion_signals"] == [signal]


@pytest.mark.anyio
async def test_glob_exists_signal_roundtrip(client: AsyncClient) -> None:
    """glob_exists signal survives full POST → GET round-trip."""
    signal = {"type": "glob_exists", "value": "src/**/*.ts"}
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": [signal]})
    task_id = resp.json()["id"]
    assert (await client.get(f"/tasks/{task_id}")).json()["completion_signals"] == [signal]


@pytest.mark.anyio
async def test_file_contains_signal_roundtrip(client: AsyncClient) -> None:
    """file_contains signal survives full POST → GET round-trip."""
    signal = {"type": "file_contains", "value": "def my_function"}
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": [signal]})
    task_id = resp.json()["id"]
    assert (await client.get(f"/tasks/{task_id}")).json()["completion_signals"] == [signal]


@pytest.mark.anyio
async def test_llm_review_signal_roundtrip(client: AsyncClient) -> None:
    """llm_review signal survives full POST → GET round-trip."""
    signal = {"type": "llm_review", "value": "Verify the implementation is correct"}
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": [signal]})
    task_id = resp.json()["id"]
    assert (await client.get(f"/tasks/{task_id}")).json()["completion_signals"] == [signal]


@pytest.mark.anyio
async def test_llm_judge_signal_roundtrip(client: AsyncClient) -> None:
    """llm_judge signal survives full POST → GET round-trip."""
    signal = {"type": "llm_judge", "value": "Did the agent complete all acceptance criteria?"}
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": [signal]})
    task_id = resp.json()["id"]
    assert (await client.get(f"/tasks/{task_id}")).json()["completion_signals"] == [signal]


# -- Backward compatibility: empty signals ---------------------------------


@pytest.mark.anyio
async def test_empty_completion_signals_is_default(client: AsyncClient) -> None:
    """POST /tasks without completion_signals defaults to empty list."""
    resp = await client.post("/tasks", json=BASE_TASK)
    assert resp.status_code == 201
    assert resp.json()["completion_signals"] == []


@pytest.mark.anyio
async def test_explicit_empty_completion_signals(client: AsyncClient) -> None:
    """POST /tasks with completion_signals=[] is accepted and returns []."""
    resp = await client.post("/tasks", json={**BASE_TASK, "completion_signals": []})
    assert resp.status_code == 201
    assert resp.json()["completion_signals"] == []


@pytest.mark.anyio
async def test_get_task_with_no_signals_returns_empty_list(client: AsyncClient) -> None:
    """GET /tasks/{id} returns empty completion_signals list when none were set."""
    create_resp = await client.post("/tasks", json=BASE_TASK)
    task_id = create_resp.json()["id"]
    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["completion_signals"] == []


# -- Invalid signal type ---------------------------------------------------


@pytest.mark.anyio
async def test_invalid_signal_type_rejected_with_422(client: AsyncClient) -> None:
    """POST /tasks with an unknown signal type returns 422 Unprocessable Entity."""
    resp = await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [
                {"type": "invalid_type", "value": "something"},
            ],
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_missing_signal_value_rejected_with_422(client: AsyncClient) -> None:
    """POST /tasks with a signal missing 'value' returns 422."""
    resp = await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [
                {"type": "path_exists"},
            ],
        },
    )
    assert resp.status_code == 422


# -- Signals visible in list endpoint ---------------------------------------


@pytest.mark.anyio
async def test_list_tasks_includes_completion_signals(client: AsyncClient) -> None:
    """GET /tasks returns completion_signals for each task."""
    await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [{"type": "path_exists", "value": "output.txt"}],
        },
    )
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["completion_signals"] == [{"type": "path_exists", "value": "output.txt"}]


@pytest.mark.anyio
async def test_list_tasks_filtered_by_status_includes_signals(client: AsyncClient) -> None:
    """GET /tasks?status=open preserves completion_signals in filtered results."""
    await client.post(
        "/tasks",
        json={
            **BASE_TASK,
            "completion_signals": [{"type": "test_passes", "value": "make test"}],
        },
    )
    resp = await client.get("/tasks?status=open")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["completion_signals"] == [{"type": "test_passes", "value": "make test"}]
