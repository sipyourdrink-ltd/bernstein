"""Class-level guard: every TaskCreate field is forwarded or deliberately not."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from bernstein.core.models import CompletionSignal, Task

from bernstein.core.orchestration import manager
from bernstein.core.planning import planner
from bernstein.core.quality.janitor import evaluate_signal
from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.backlog_parser import ParsedBacklogTask
from bernstein.core.tasks.models import DispatchTier
from bernstein.core.tasks.task_body import build_task_body
from bernstein.core.tasks.task_store import TaskStore

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def test_every_task_create_field_is_classified() -> None:
    from bernstein.core.tasks.task_body import FORWARDED_FIELDS, NOT_FORWARDED

    fields = set(TaskCreate.model_fields)
    unclassified = fields - set(FORWARDED_FIELDS) - set(NOT_FORWARDED)
    assert not unclassified, f"decide forward-or-not (with a reason) for: {sorted(unclassified)}"
    assert not set(FORWARDED_FIELDS) & set(NOT_FORWARDED)
    assert not (set(FORWARDED_FIELDS) | set(NOT_FORWARDED)) - fields, "stale entry"
    assert all(NOT_FORWARDED.values()), "every non-forwarded field needs a reason"


def test_body_validates_as_task_create_and_carries_signals() -> None:
    from bernstein.core.tasks.task_body import build_task_body

    task = Task(
        id="t1",
        title="t",
        description="d",
        role="backend",
        completion_signals=[CompletionSignal(type="file_contains", value="x")],
        metadata={"context_files": ["a.py"], "administrative_run_id": "admin-" + "a" * 32},
    )
    body = build_task_body(task)
    parsed = TaskCreate(**body)
    assert [(s.type, s.value) for s in parsed.completion_signals] == [("file_contains", "x")]
    assert parsed.metadata == {"context_files": ["a.py"]}
    assert "run_id" not in body
    assert parsed.run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", [planner, manager], ids=["planner", "manager"])
async def test_declared_witnesses_and_context_survive_task_creation(tmp_path: Path, producer: ModuleType) -> None:
    task = Task(
        id="t1",
        title="Check output",
        description="Keep the declared completion witness",
        role="backend",
        completion_signals=[CompletionSignal(type="file_contains", value="result.txt :: accepted")],
        metadata={
            "context_files": ["contract.md"],
            "dispatch_hints": {"estimated_changed_lines": True, "requires_shell": False, "unknown": "not forwarded"},
            "loader_internal": "not forwarded",
            "administrative_run_id": "admin-" + "a" * 32,
        },
    )
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        task_id = await producer._post_task_to_server(client, "http://server", task)
    stored = app.state.store.get_task(task_id)
    assert stored is not None
    assert [(signal.type, signal.value) for signal in stored.completion_signals] == [
        ("file_contains", "result.txt :: accepted")
    ]
    assert stored.metadata == {"context_files": ["contract.md"], "dispatch_hints": {"requires_shell": False}}
    (tmp_path / "result.txt").write_text("rejected", encoding="utf-8")
    assert not evaluate_signal(stored.completion_signals[0], tmp_path)[0]
    (tmp_path / "result.txt").write_text("accepted", encoding="utf-8")
    assert evaluate_signal(stored.completion_signals[0], tmp_path)[0]


def test_backlog_payload_emits_janitor_signals() -> None:
    bt = ParsedBacklogTask(
        title="t",
        description="d",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        source_file="b.yaml",
        janitor_signals=({"type": "path_exists", "value": "x.py"},),
    )
    payload = bt.to_task_payload()
    assert payload["completion_signals"] == [{"type": "path_exists", "value": "x.py"}]
    TaskCreate(**payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["local", "edge", "search", "research", "sandbox", "omitted"])
async def test_dispatch_tier_round_trip(tmp_path: Path, tier: str) -> None:
    expected = DispatchTier.LOCAL if tier == "omitted" else DispatchTier(tier)
    body = build_task_body(Task(id="source", title="tier", description="d", role="backend", dispatch_tier=expected))
    assert body.get("dispatch_tier") == expected.value
    assert "tier" not in body
    if tier == "omitted":
        del body["dispatch_tier"]
    path = tmp_path / "tasks.jsonl"
    app = create_app(jsonl_path=path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        response = await client.post("/tasks", json=body)
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["dispatch_tier"] == expected.value
        assert "tier" not in data
        task_id = data["id"]
        assert (await client.get(f"/tasks/{task_id}")).json()["dispatch_tier"] == expected.value
        for invalid in [None, 1, 1.5, True, [], {}, "unknown", "LOCAL", ""]:
            rejected = await client.post("/tasks", json={**body, "dispatch_tier": invalid})
            assert rejected.status_code == 422, rejected.text
            assert any(error["loc"][-1] == "dispatch_tier" for error in rejected.json()["detail"])

    store = app.state.store
    assert store.get_task(task_id).dispatch_tier is expected
    single = await store.create(TaskCreate(**{**body, "title": "single"}))
    batch, skipped = await store.create_batch([TaskCreate(**{**body, "title": "batch"})])
    assert skipped == []
    assert single.dispatch_tier is expected
    assert batch[0].dispatch_tier is expected
    original = path.read_bytes()
    records = [json.loads(line) for line in original.splitlines()]
    assert all(record["dispatch_tier"] == expected.value for record in records)
    restarted = TaskStore(path)
    restarted.replay_jsonl()
    for task in [store.get_task(task_id), single, batch[0]]:
        assert restarted.get_task(task.id).dispatch_tier is expected
    assert path.read_bytes() == original

    legacy = {key: value for key, value in records[0].items() if key != "dispatch_tier"}
    legacy["id"] = "legacy"
    with path.open("a") as stream:
        stream.write(json.dumps(legacy) + "\n")
        for recorded in ["edge", "research", expected.value]:
            stream.write(json.dumps({"id": task_id, "dispatch_tier": recorded}) + "\n")
            stream.write(json.dumps({"id": task_id, "status": "open"}) + "\n")
            stream.flush()
            replayed = TaskStore(path)
            replayed.replay_jsonl()
            assert replayed.get_task(task_id).dispatch_tier is DispatchTier(recorded)
            assert replayed.get_task("legacy").dispatch_tier is DispatchTier.LOCAL
