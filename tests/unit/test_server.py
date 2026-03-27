"""Tests for the Bernstein task server."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import TaskStore, create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    """Create a fresh FastAPI app per test."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# -- helpers ----------------------------------------------------------------

TASK_PAYLOAD = {
    "title": "Implement parser",
    "description": "Write the YAML parser module",
    "role": "backend",
    "priority": 2,
}


# -- POST /tasks -----------------------------------------------------------

@pytest.mark.anyio
async def test_create_task(client: AsyncClient) -> None:
    """POST /tasks creates a task and returns 201."""
    resp = await client.post("/tasks", json=TASK_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Implement parser"
    assert data["role"] == "backend"
    assert data["status"] == "open"
    assert data["id"]  # non-empty


@pytest.mark.anyio
async def test_create_task_defaults(client: AsyncClient) -> None:
    """POST /tasks applies correct defaults for optional fields."""
    resp = await client.post("/tasks", json={
        "title": "Minimal",
        "description": "A bare-minimum task",
        "role": "qa",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["priority"] == 2
    assert data["scope"] == "medium"
    assert data["complexity"] == "medium"
    assert data["estimated_minutes"] == 30
    assert data["depends_on"] == []


# -- GET /tasks/next/{role} -------------------------------------------------

@pytest.mark.anyio
async def test_claim_next_task(client: AsyncClient) -> None:
    """GET /tasks/next/{role} returns and claims the highest-priority task."""
    # Create two tasks — priority 1 (critical) and priority 3.
    await client.post("/tasks", json={**TASK_PAYLOAD, "priority": 3})
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Critical fix", "priority": 1})

    resp = await client.get("/tasks/next/backend")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Critical fix"
    assert data["status"] == "claimed"


@pytest.mark.anyio
async def test_claim_next_no_tasks(client: AsyncClient) -> None:
    """GET /tasks/next/{role} returns 404 when no open tasks exist."""
    resp = await client.get("/tasks/next/backend")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_claim_next_role_filter(client: AsyncClient) -> None:
    """GET /tasks/next/{role} only returns tasks matching the role."""
    await client.post("/tasks", json={**TASK_PAYLOAD, "role": "frontend"})

    resp = await client.get("/tasks/next/backend")
    assert resp.status_code == 404

    resp = await client.get("/tasks/next/frontend")
    assert resp.status_code == 200
    assert resp.json()["role"] == "frontend"


@pytest.mark.anyio
async def test_claim_does_not_double_claim(client: AsyncClient) -> None:
    """A claimed task is not returned on subsequent claims."""
    await client.post("/tasks", json=TASK_PAYLOAD)

    resp1 = await client.get("/tasks/next/backend")
    assert resp1.status_code == 200

    resp2 = await client.get("/tasks/next/backend")
    assert resp2.status_code == 404


# -- POST /tasks/{task_id}/complete -----------------------------------------

@pytest.mark.anyio
async def test_complete_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/complete marks task as done."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "All good"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["result_summary"] == "All good"


@pytest.mark.anyio
async def test_complete_unknown_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/complete returns 404 for unknown id."""
    resp = await client.post(
        "/tasks/nonexistent/complete",
        json={"result_summary": "done"},
    )
    assert resp.status_code == 404


# -- POST /tasks/{task_id}/fail ---------------------------------------------

@pytest.mark.anyio
async def test_fail_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/fail marks task as failed."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/tasks/{task_id}/fail",
        json={"reason": "Timed out"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["result_summary"] == "Timed out"


@pytest.mark.anyio
async def test_fail_unknown_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/fail returns 404 for unknown id."""
    resp = await client.post(
        "/tasks/nonexistent/fail",
        json={"reason": "gone"},
    )
    assert resp.status_code == 404


# -- GET /tasks -------------------------------------------------------------

@pytest.mark.anyio
async def test_list_all_tasks(client: AsyncClient) -> None:
    """GET /tasks returns all tasks."""
    await client.post("/tasks", json=TASK_PAYLOAD)
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Second"})

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.anyio
async def test_list_tasks_with_status_filter(client: AsyncClient) -> None:
    """GET /tasks?status=open filters correctly."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]
    await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "ok"})

    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Still open"})

    resp = await client.get("/tasks", params={"status": "open"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Still open"


# -- GET /status ------------------------------------------------------------

@pytest.mark.anyio
async def test_status_empty(client: AsyncClient) -> None:
    """GET /status returns zeroes when no tasks exist."""
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["open"] == 0
    assert data["per_role"] == []


@pytest.mark.anyio
async def test_status_counts(client: AsyncClient) -> None:
    """GET /status returns correct counts after mixed operations."""
    # Create 3 tasks
    r1 = await client.post("/tasks", json=TASK_PAYLOAD)
    r2 = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "T2"})
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "T3", "role": "qa"})

    # Complete one, fail another
    await client.post(f"/tasks/{r1.json()['id']}/complete", json={"result_summary": "ok"})
    await client.post(f"/tasks/{r2.json()['id']}/fail", json={"reason": "bad"})

    resp = await client.get("/status")
    data = resp.json()
    assert data["total"] == 3
    assert data["done"] == 1
    assert data["failed"] == 1
    assert data["open"] == 1

    # Per-role checks
    roles_by_name = {r["role"]: r for r in data["per_role"]}
    assert roles_by_name["backend"]["done"] == 1
    assert roles_by_name["backend"]["failed"] == 1
    assert roles_by_name["qa"]["open"] == 1


# -- POST /agents/{agent_id}/heartbeat -------------------------------------

@pytest.mark.anyio
async def test_heartbeat(client: AsyncClient) -> None:
    """POST /agents/{id}/heartbeat returns acknowledged response."""
    resp = await client.post(
        "/agents/agent-1/heartbeat",
        json={"role": "backend", "status": "working"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "agent-1"
    assert data["acknowledged"] is True
    assert data["server_ts"] > 0


@pytest.mark.anyio
async def test_heartbeat_updates_existing(client: AsyncClient) -> None:
    """Subsequent heartbeats update the timestamp."""
    await client.post("/agents/agent-1/heartbeat", json={"role": "backend"})
    resp = await client.post("/agents/agent-1/heartbeat", json={"role": "backend"})
    assert resp.status_code == 200


# -- GET /health ------------------------------------------------------------

@pytest.mark.anyio
async def test_health(client: AsyncClient) -> None:
    """GET /health returns ok status."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["uptime_s"] >= 0
    assert data["task_count"] == 0
    assert data["agent_count"] == 0


@pytest.mark.anyio
async def test_health_reflects_counts(client: AsyncClient) -> None:
    """GET /health task_count and agent_count update live."""
    await client.post("/tasks", json=TASK_PAYLOAD)
    await client.post("/agents/a1/heartbeat", json={"role": "backend"})

    resp = await client.get("/health")
    data = resp.json()
    assert data["task_count"] == 1
    assert data["agent_count"] == 1


# -- JSONL persistence ------------------------------------------------------

@pytest.mark.anyio
async def test_jsonl_written(client: AsyncClient, jsonl_path: Path) -> None:
    """Creating a task writes a JSONL line to disk."""
    await client.post("/tasks", json=TASK_PAYLOAD)
    assert jsonl_path.exists()
    lines = [l for l in jsonl_path.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["title"] == "Implement parser"


@pytest.mark.anyio
async def test_jsonl_replay(jsonl_path: Path) -> None:
    """TaskStore.replay_jsonl restores tasks from disk."""
    # Write a fake JSONL record
    record = {
        "id": "abc123",
        "title": "Replayed task",
        "description": "From disk",
        "role": "backend",
        "priority": 1,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 15,
        "status": "open",
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
    }
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(json.dumps(record) + "\n")

    store = TaskStore(jsonl_path)
    store.replay_jsonl()

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "abc123"
    assert tasks[0].title == "Replayed task"


@pytest.mark.anyio
async def test_jsonl_replay_status_update(jsonl_path: Path) -> None:
    """Replay applies status updates from later JSONL lines."""
    base = {
        "id": "xyz789",
        "title": "Will complete",
        "description": "d",
        "role": "qa",
        "status": "open",
    }
    update = {
        "id": "xyz789",
        "status": "done",
        "result_summary": "Passed all tests",
    }
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(json.dumps(base) + "\n" + json.dumps(update) + "\n")

    store = TaskStore(jsonl_path)
    store.replay_jsonl()

    task = store.get_task("xyz789")
    assert task is not None
    assert task.status.value == "done"
    assert task.result_summary == "Passed all tests"


# -- stale agent detection --------------------------------------------------

def test_stale_agent_detection(tmp_path: Path) -> None:
    """Agents are marked dead after heartbeat timeout."""
    store = TaskStore(tmp_path / "tasks.jsonl")
    # Record heartbeat in the past
    store._agents["old-agent"] = __import__(
        "bernstein.core.models", fromlist=["AgentSession"]
    ).AgentSession(
        id="old-agent",
        role="backend",
        heartbeat_ts=0.0,  # epoch — definitely stale
        status="working",
    )
    count = store.mark_stale_dead()
    assert count == 1
    assert store._agents["old-agent"].status == "dead"
