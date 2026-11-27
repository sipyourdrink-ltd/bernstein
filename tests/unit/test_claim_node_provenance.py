"""Claim-time node provenance: ``claimed_by_node`` (issue #6611).

The server records on the task, with the claim itself, which registered
cluster node owns it. The janitor reads that field to tell a remote
completion from a local one, so it must be stamped by every claim route,
left empty for local claims, cleared when the claim is surrendered, and
survive the task store's JSONL replay and the wire format.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import ClusterConfig, Task
from fastapi.testclient import TestClient

from bernstein.core.server.server import create_app
from bernstein.core.tasks.task_store_core import TaskStore


@pytest.fixture()
def app(tmp_path: Path) -> Any:
    app = create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        cluster_config=ClusterConfig(enabled=True),
    )
    app.state.node_registry._nodes.clear()
    return app


def _register(client: TestClient, name: str = "worker-node") -> str:
    resp = client.post("/cluster/nodes", json={"name": name, "url": f"http://{name}:9000"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create(client: TestClient, title: str = "t") -> str:
    resp = client.post("/tasks", json={"title": title, "description": "d", "role": "backend"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _claim_next(client: TestClient, params: dict[str, str]) -> dict[str, Any]:
    resp = client.get("/tasks/next/backend", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _claim_by_id(client: TestClient, params: dict[str, str]) -> dict[str, Any]:
    resp = client.post(f"/tasks/{_create(client)}/claim", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _claim_batch(client: TestClient, params: dict[str, str]) -> dict[str, Any]:
    task_id = _create(client)
    body: dict[str, Any] = {"task_ids": [task_id], "agent_id": "agent-1"}
    if "claimed_by_session" in params:
        body["claimed_by_session"] = params["claimed_by_session"]
    resp = client.post("/tasks/claim-batch", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["claimed"] == [task_id]
    return client.get(f"/tasks/{task_id}").json()


_ROUTES = pytest.mark.parametrize(
    "claim",
    [_claim_next, _claim_by_id, _claim_batch],
    ids=["next", "by-id", "batch"],
)


@_ROUTES
def test_claim_owned_by_a_registered_node_stamps_it(app: Any, claim: Any) -> None:
    client = TestClient(app)
    node_id = _register(client)
    if claim is _claim_next:
        _create(client)

    task = claim(client, {"claimed_by_session": node_id})

    assert task["claimed_by_node"] == node_id
    # A cleanly exiting worker reports completion, then unregisters its node
    # (worker_cmd._unregister). The provenance outlives the registration.
    done = client.post(f"/tasks/{task['id']}/complete", json={"result_summary": "done"})
    assert done.status_code == 200, done.text
    assert client.delete(f"/cluster/nodes/{node_id}").status_code == 204
    after = client.get(f"/tasks/{task['id']}").json()
    assert after["status"] == "done"
    assert after["claimed_by_node"] == node_id


@_ROUTES
@pytest.mark.parametrize(
    "params",
    [{}, {"claimed_by_session": "orchestrator-session"}],
    ids=["no-owner", "unregistered-owner"],
)
def test_claim_by_a_non_node_leaves_it_empty(app: Any, claim: Any, params: dict[str, str]) -> None:
    client = TestClient(app)
    _register(client)
    if claim is _claim_next:
        _create(client)

    assert claim(client, params)["claimed_by_node"] is None


@pytest.mark.parametrize("claim", [_claim_next, _claim_by_id], ids=["next", "by-id"])
def test_orchestrator_naming_its_node_for_the_health_gate_is_local(app: Any, claim: Any) -> None:
    """An orchestrator sends its own node id as ``node_id`` (#7853); it still owns the claim locally."""
    client = TestClient(app)
    node_id = _register(client, name="orch-node")
    if claim is _claim_next:
        _create(client)

    task = claim(client, {"claimed_by_session": "orchestrator-session", "node_id": node_id})

    assert task["claimed_by_session"] == "orchestrator-session"
    assert task["claimed_by_node"] is None


def test_release_clears_it(app: Any) -> None:
    client = TestClient(app)
    node_id = _register(client)
    task = _claim_by_id(client, {"claimed_by_session": node_id})

    assert client.post(f"/tasks/{task['id']}/release", json={"reason": "spawn failed"}).status_code == 200

    assert client.get(f"/tasks/{task['id']}").json()["claimed_by_node"] is None


def test_task_dict_round_trip_keeps_it_and_old_records_default_to_none() -> None:
    task = Task(id="T-1", title="t", description="d", role="backend")
    task.claimed_by_node = "node-1"

    assert Task.from_dict(task.to_dict()).claimed_by_node == "node-1"
    old = task.to_dict()
    del old["claimed_by_node"]
    assert Task.from_dict(old).claimed_by_node is None


def _req() -> Any:
    return SimpleNamespace(
        title="T",
        description="D",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
    )


@pytest.mark.anyio
async def test_jsonl_replay_restores_it_and_a_release_clears_it(tmp_path: Path) -> None:
    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl, archive_path=tmp_path / "archive" / "tasks.jsonl")
    kept = await store.create(_req())
    released = await store.create(_req())
    await store.claim_by_id(kept.id, claimed_by_session="node-1", claimed_by_node="node-1")
    await store.claim_by_id(released.id, claimed_by_session="node-1", claimed_by_node="node-1")
    await store.release(released.id)
    await store.flush_buffer()

    replayed = TaskStore(jsonl, archive_path=tmp_path / "archive" / "tasks.jsonl")
    replayed.replay_jsonl()

    assert replayed.get_task(kept.id).claimed_by_node == "node-1"
    assert replayed.get_task(released.id).claimed_by_node is None
