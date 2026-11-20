"""Backend tier survives real retry requests and explicit reopen persistence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bernstein.core.server import SSEBus, create_app
from bernstein.core.server.server_models import TaskReopenRequest
from bernstein.core.tasks import task_lifecycle as lifecycle
from bernstein.core.tasks.models import DispatchTier, Task, TaskStatus
from bernstein.core.tasks.task_store import TaskStore


@pytest.mark.parametrize("path", ["tick", "reap"])
@pytest.mark.parametrize("tier", list(DispatchTier))
@pytest.mark.parametrize("reason", ["unknown failure", "connection reset by peer", "insufficient capacity"])
def test_retry_post_preserves_tier(path: str, tier: DispatchTier, reason: str) -> None:
    task = Task(
        id="original",
        title="Synthetic",
        description="Local fixture",
        role="backend",
        batch_eligible=False,
        dispatch_tier=tier,
        result_summary=reason,
    )
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "retry"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if path == "tick":
            assert lifecycle.maybe_retry_task(
                task,
                retried_task_ids=set(),
                max_task_retries=2,
                client=client,
                server_url="http://fixture.invalid",
                quarantine=SimpleNamespace(),
            )
        else:
            lifecycle.retry_or_fail_task(
                task.id,
                reason,
                client=client,
                server_url="http://fixture.invalid",
                max_task_retries=2,
                retried_task_ids=set(),
                tasks_snapshot={"failed": [task]},
            )
    assert len(bodies) == 1
    assert bodies[0]["dispatch_tier"] == tier.value
    assert bodies[0]["model"] is None
    assert bodies[0]["metadata"]["retry_of"] == task.id


@pytest.mark.parametrize("value", [None, "invalid", 1, False, []])
def test_reopen_request_rejects_explicit_invalid_tier(value: Any) -> None:
    with pytest.raises(ValidationError):
        TaskReopenRequest.model_validate({"reason": "synthetic", "dispatch_tier": value})


def test_reopen_request_omission_preserves_compatibility() -> None:
    request = TaskReopenRequest(reason="synthetic")
    assert request.dispatch_tier is None
    assert "dispatch_tier" not in request.model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", list(DispatchTier))
async def test_reopen_tier_is_persisted_with_transition(tmp_path: Path, tier: DispatchTier) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
    task = Task(
        id="original",
        title="Synthetic",
        description="Local fixture",
        role="backend",
        batch_eligible=False,
        dispatch_tier=DispatchTier.EDGE,
        status=TaskStatus.DONE,
    )
    store._tasks[task.id] = task
    store._index_add(task)
    await store.reopen(task.id, "operator selected tier", dispatch_tier=tier)
    assert task.dispatch_tier is tier
    assert task.status is TaskStatus.OPEN
    assert task.metadata["dispatch_tier_transition"] == {
        "old": "edge",
        "new": tier.value,
        "reason": "operator selected tier",
    }
    record = json.loads((tmp_path / "tasks.jsonl").read_text().splitlines()[-1])
    assert record["dispatch_tier"] == tier.value
    assert record["metadata"]["dispatch_tier_transition"] == task.metadata["dispatch_tier_transition"]
    recovered = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
    recovered.replay_jsonl()
    replayed = recovered.get_task(task.id)
    assert replayed is not None
    assert replayed.dispatch_tier is tier
    assert replayed.status is TaskStatus.OPEN
    assert replayed.metadata["dispatch_tier_transition"] == task.metadata["dispatch_tier_transition"]


@pytest.mark.asyncio
async def test_reopen_omitted_tier_retains_existing_value(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
    task = Task(
        id="original",
        title="Synthetic",
        description="Local fixture",
        role="backend",
        batch_eligible=False,
        dispatch_tier=DispatchTier.RESEARCH,
        status=TaskStatus.DONE,
    )
    store._tasks[task.id] = task
    store._index_add(task)
    await store.reopen(task.id, "transport error")
    assert task.dispatch_tier is DispatchTier.RESEARCH
    assert "dispatch_tier_transition" not in task.metadata


@pytest.mark.asyncio
async def test_invalid_direct_reopen_does_not_mutate_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
    task = Task(
        id="original",
        title="Synthetic",
        description="Local fixture",
        role="backend",
        batch_eligible=False,
        dispatch_tier=DispatchTier.EDGE,
        status=TaskStatus.DONE,
    )
    store._tasks[task.id] = task
    store._index_add(task)
    before = task.to_dict()
    with pytest.raises(ValueError):
        await store.reopen(task.id, "invalid", dispatch_tier="unsupported")  # type: ignore[arg-type]
    assert task.to_dict() == before
    assert not (tmp_path / "tasks.jsonl").exists()


@pytest.fixture(scope="module")
def route_app(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return create_app(jsonl_path=tmp_path_factory.mktemp("reopen-server") / "tasks.jsonl")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, "research"),
        ({"dispatch_tier": "local"}, "local"),
        ({"dispatch_tier": "sandbox"}, "sandbox"),
        ({"dispatch_tier": None}, None),
        ({"dispatch_tier": "unsupported"}, None),
    ],
)
async def test_reopen_route_validates_and_applies_tier(
    route_app: Any, tmp_path: Path, payload: dict[str, Any], expected: str | None
) -> None:
    store = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
    route_app.state.store = store
    route_app.state.sse_bus = SSEBus()
    task = Task(
        id="original",
        title="Synthetic",
        description="Local fixture",
        role="backend",
        batch_eligible=False,
        dispatch_tier=DispatchTier.RESEARCH,
        status=TaskStatus.DONE,
    )
    store._tasks[task.id] = task
    store._index_add(task)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.post("/tasks/original/reopen", json={"reason": "operator selection", **payload})
    assert response.status_code == (200 if expected is not None else 422), response.text
    if expected is None:
        assert task.status is TaskStatus.DONE
        assert task.dispatch_tier is DispatchTier.RESEARCH
    else:
        assert response.json()["dispatch_tier"] == expected
        recovered = TaskStore(tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
        recovered.replay_jsonl()
        assert recovered.get_task(task.id).dispatch_tier.value == expected


def test_reopen_schema_describes_optional_nonnullable_tier() -> None:
    schema = TaskReopenRequest.model_json_schema()
    tier = schema["properties"]["dispatch_tier"]
    assert "dispatch_tier" not in schema.get("required", [])
    assert "anyOf" not in tier
    assert tier["$ref"].endswith("/DispatchTier")
