"""Routing declarations survive REST, later writes and JSONL restart (#6562)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bernstein.core.server import create_app
from bernstein.core.server.server_app import task_to_response
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.models import Task
from bernstein.core.tasks.task_body import build_task_body
from bernstein.core.tasks.task_store import TaskStore

PINS = {"model": "Opaque Model@REV/Do-Not-Alias", "effort": "Exact-XHIGH", "cli": "Official-Client@V2"}
REQUIRES = [" GPU ", "Python", "python", "Python"]


def _body(**updates: Any) -> dict[str, Any]:
    return {
        "title": "Preserve declarations",
        "description": "Local persistence test",
        "role": "backend",
        "priority": 1,
        **updates,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("pins", [PINS, {"model": "", "effort": "", "cli": ""}], ids=["opaque", "empty"])
async def test_rest_pins_survive_later_record_and_restart(tmp_path: Path, pins: dict[str, str]) -> None:
    path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        created = await client.post("/tasks", json=_body(**pins))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert {key: created.json()[key] for key in pins} == pins
        prioritized = await client.post(f"/tasks/{task_id}/prioritize")
        assert prioritized.status_code == 200, prioritized.text
    await app.state.store.flush_buffer()
    del app
    before = path.read_bytes()
    restarted = create_app(jsonl_path=path)
    restarted.state.store.replay_jsonl()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://server") as client:
        fetched = await client.get(f"/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "open"
        assert {key: fetched.json()[key] for key in pins} == pins
    assert path.read_bytes() == before
    records = [json.loads(line) for line in before.splitlines()]
    assert len(records) >= 2
    assert all({key: row[key] for key in pins} == pins for row in records)


@pytest.mark.asyncio
async def test_rest_requires_survives_response_later_record_and_restart(tmp_path: Path) -> None:
    path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        created = await client.post("/tasks", json=_body(requires=REQUIRES))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert created.json().get("requires") == REQUIRES
        prioritized = await client.post(f"/tasks/{task_id}/prioritize")
        assert prioritized.status_code == 200, prioritized.text
    await app.state.store.flush_buffer()
    del app
    restarted = create_app(jsonl_path=path)
    restarted.state.store.replay_jsonl()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://server") as client:
        fetched = await client.get(f"/tasks/{task_id}")
        assert fetched.json()["requires"] == REQUIRES
    assert all(json.loads(line)["requires"] == REQUIRES for line in path.read_text().splitlines())


@pytest.mark.asyncio
async def test_rest_batch_preserves_independent_hints_after_restart(tmp_path: Path) -> None:
    path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        result = await client.post(
            "/tasks/batch", json={"tasks": [_body(**PINS, requires=REQUIRES), _body(title="Defaults")]}
        )
        assert result.status_code == 201, result.text
        rows = result.json()["created"]
        assert len(rows) == 2
        assert rows[0].get("requires") == REQUIRES
        assert rows[1].get("requires") == []
    await app.state.store.flush_buffer()
    reopened = TaskStore(path)
    reopened.replay_jsonl()
    pinned, default = (reopened.get_task(row["id"]) for row in rows)
    assert pinned is not None and default is not None
    assert (pinned.model, pinned.effort, pinned.cli, pinned.requires) == (*PINS.values(), REQUIRES)
    assert (default.model, default.effort, default.cli, default.requires) == (None, None, None, [])


def test_replay_latest_explicit_hints_and_legacy_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "tasks.jsonl"
    legacy = Task(id="legacy", title="Old row", description="d", role="backend").to_dict()
    for key in [*PINS, "requires"]:
        legacy.pop(key, None)
    rows = [legacy, {**legacy, "id": "updated"}]
    updates = [
        {**PINS, "requires": REQUIRES},
        {"model": "Next-Exact-Pin", "requires": ["Next-Cap"]},
        {"model": None, "effort": None, "cli": None, "requires": []},
    ]
    expected = [
        (*PINS.values(), REQUIRES),
        ("Next-Exact-Pin", PINS["effort"], PINS["cli"], ["Next-Cap"]),
        (None, None, None, []),
    ]
    for update, values in zip(updates, expected, strict=True):
        rows.extend([{"id": "updated", **update}, {"id": "updated", "status": "open"}])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        before = path.read_bytes()
        store = TaskStore(path)
        store.replay_jsonl()
        task, old = store.get_task("updated"), store.get_task("legacy")
        assert task is not None and old is not None
        assert (task.model, task.effort, task.cli, task.requires) == values
        assert (old.model, old.effort, old.cli, old.requires) == (None, None, None, [])
        assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_requires_lists_are_detached_at_request_store_and_response(tmp_path: Path, batch: bool) -> None:
    supplied = REQUIRES.copy()
    req = TaskCreate(**_body(requires=supplied))
    supplied.append("external mutation")
    assert req.requires == REQUIRES
    store = TaskStore(tmp_path / "tasks.jsonl")
    if batch:
        tasks, skipped = await store.create_batch([req])
        assert skipped == []
        task = tasks[0]
    else:
        task = await store.create(req)
    req.requires.append("request mutation")
    assert task.requires == REQUIRES
    response = task_to_response(task)
    response.requires.append("response mutation")
    record = store._task_to_record(task)
    record["requires"].append("record mutation")
    assert task.requires == REQUIRES


@pytest.mark.parametrize("pins", [PINS, {"model": "", "effort": "", "cli": ""}], ids=["opaque", "empty"])
def test_shared_request_builder_copies_requires_and_preserves_pins(pins: dict[str, str]) -> None:
    task = Task(id="source", title="t", description="d", role="backend", requires=REQUIRES.copy(), **pins)
    body = build_task_body(task)
    assert {key: body.get(key) for key in pins} == pins
    assert body.get("requires") == REQUIRES
    body["requires"].append("body mutation")
    assert task.requires == REQUIRES


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "invalid", [None, "python", {}, 1, [1], [True], [None], [""], [" \t"], ["x" * 1001], ["x"] * 101]
)
async def test_invalid_requires_rejected_before_persistence(tmp_path: Path, batch: bool, invalid: Any) -> None:
    path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=path)
    body = _body(requires=invalid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://server") as client:
        result = await client.post("/tasks/batch" if batch else "/tasks", json={"tasks": [body]} if batch else body)
        assert result.status_code == 422, result.text
        assert any("requires" in error["loc"] for error in result.json()["detail"])
    assert app.state.store.list_tasks() == []
    assert not path.exists() or path.read_bytes() == b""
