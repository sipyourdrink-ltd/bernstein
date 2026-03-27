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


# -- GET /tasks/archive -------------------------------------------------------

@pytest.mark.anyio
async def test_complete_task_writes_archive(client: AsyncClient, tmp_path: Path) -> None:
    """Completing a task appends a record to .sdd/archive/tasks.jsonl."""
    app_obj = client._transport.app  # type: ignore[attr-defined]
    store = app_obj.state.store
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store._archive_path = archive_path

    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "All done"},
    )

    assert archive_path.exists(), "archive file should be created on completion"
    lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["task_id"] == task_id
    assert record["status"] == "done"
    assert record["result_summary"] == "All done"
    assert record["role"] == "backend"
    assert "completed_at" in record
    assert "duration_seconds" in record


@pytest.mark.anyio
async def test_fail_task_writes_archive(client: AsyncClient, tmp_path: Path) -> None:
    """Failing a task appends a record to the archive."""
    app_obj = client._transport.app  # type: ignore[attr-defined]
    store = app_obj.state.store
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store._archive_path = archive_path

    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/fail", json={"reason": "Timed out"})

    lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "failed"
    assert record["result_summary"] == "Timed out"


@pytest.mark.anyio
async def test_archive_endpoint_returns_records(client: AsyncClient, tmp_path: Path) -> None:
    """GET /tasks/archive returns completed and failed task records."""
    app_obj = client._transport.app  # type: ignore[attr-defined]
    store = app_obj.state.store
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store._archive_path = archive_path

    r1 = await client.post("/tasks", json=TASK_PAYLOAD)
    r2 = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Task 2"})
    await client.post(f"/tasks/{r1.json()['id']}/complete", json={"result_summary": "ok"})
    await client.post(f"/tasks/{r2.json()['id']}/fail", json={"reason": "bad"})

    resp = await client.get("/tasks/archive")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    statuses = {r["status"] for r in data}
    assert statuses == {"done", "failed"}


@pytest.mark.anyio
async def test_archive_endpoint_limit(client: AsyncClient, tmp_path: Path) -> None:
    """GET /tasks/archive?limit=1 returns only the last 1 record."""
    app_obj = client._transport.app  # type: ignore[attr-defined]
    store = app_obj.state.store
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store._archive_path = archive_path

    for i in range(3):
        r = await client.post("/tasks", json={**TASK_PAYLOAD, "title": f"T{i}"})
        await client.post(f"/tasks/{r.json()['id']}/complete", json={"result_summary": "ok"})

    resp = await client.get("/tasks/archive", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.anyio
async def test_archive_endpoint_empty(client: AsyncClient, tmp_path: Path) -> None:
    """GET /tasks/archive returns empty list when no tasks have been archived."""
    app_obj = client._transport.app  # type: ignore[attr-defined]
    store = app_obj.state.store
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store._archive_path = archive_path

    resp = await client.get("/tasks/archive")
    assert resp.status_code == 200
    assert resp.json() == []


# -- GET /status cost fields ------------------------------------------------


@pytest.fixture()
def metrics_jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary metrics JSONL path."""
    return tmp_path / "metrics" / "tasks.jsonl"


@pytest.fixture()
def app_with_metrics(jsonl_path: Path, metrics_jsonl_path: Path):
    """App wired to a specific metrics JSONL path."""
    return create_app(jsonl_path=jsonl_path, metrics_jsonl_path=metrics_jsonl_path)


@pytest.fixture()
async def client_with_metrics(app_with_metrics) -> AsyncClient:
    """Async HTTP client wired to the metrics-aware test app."""
    transport = ASGITransport(app=app_with_metrics)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_status_cost_zero_when_no_metrics(client_with_metrics: AsyncClient) -> None:
    """GET /status returns total_cost_usd=0.0 when no metrics file exists."""
    resp = await client_with_metrics.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_cost_usd" in data
    assert data["total_cost_usd"] == 0.0


@pytest.mark.anyio
async def test_status_returns_total_cost_from_metrics(
    client_with_metrics: AsyncClient, metrics_jsonl_path: Path
) -> None:
    """GET /status sums cost_usd from metrics JSONL and returns total."""
    metrics_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"task_id": "abc", "role": "backend", "cost_usd": 0.50},
        {"task_id": "def", "role": "qa", "cost_usd": 0.25},
        {"task_id": "ghi", "role": "backend", "cost_usd": 0.10},
    ]
    metrics_jsonl_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    resp = await client_with_metrics.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert abs(data["total_cost_usd"] - 0.85) < 1e-9


@pytest.mark.anyio
async def test_status_cost_per_role_breakdown(
    client_with_metrics: AsyncClient, metrics_jsonl_path: Path
) -> None:
    """GET /status returns per-role cost breakdown in per_role list."""
    metrics_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"task_id": "abc", "role": "backend", "cost_usd": 0.40},
        {"task_id": "def", "role": "qa", "cost_usd": 0.30},
        {"task_id": "ghi", "role": "backend", "cost_usd": 0.20},
    ]
    metrics_jsonl_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    # Also create tasks so per_role is populated
    await client_with_metrics.post("/tasks", json={**TASK_PAYLOAD, "role": "backend"})
    await client_with_metrics.post("/tasks", json={**TASK_PAYLOAD, "role": "qa"})

    resp = await client_with_metrics.get("/status")
    data = resp.json()
    roles_by_name = {r["role"]: r for r in data["per_role"]}
    assert "cost_usd" in roles_by_name["backend"]
    assert abs(roles_by_name["backend"]["cost_usd"] - 0.60) < 1e-9
    assert abs(roles_by_name["qa"]["cost_usd"] - 0.30) < 1e-9


# -- upgrade task creation -------------------------------------------------

@pytest.mark.anyio
async def test_create_upgrade_task(client: AsyncClient) -> None:
    """POST /tasks with task_type=upgrade_proposal stores upgrade_details."""
    upgrade_details = {
        "current_state": "old impl",
        "proposed_change": "new impl",
        "benefits": ["faster", "safer"],
        "risk_assessment": {"level": "low", "breaking_changes": False, "affected_components": [], "mitigation": ""},
        "rollback_plan": {"steps": ["revert commit"], "revert_commit": None, "data_migration": "", "estimated_rollback_minutes": 30},
        "cost_estimate_usd": 0.5,
        "performance_impact": "minor",
    }
    resp = await client.post("/tasks", json={
        **TASK_PAYLOAD,
        "task_type": "upgrade_proposal",
        "upgrade_details": upgrade_details,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["task_type"] == "upgrade_proposal"
    assert data["upgrade_details"] is not None
    assert data["upgrade_details"]["current_state"] == "old impl"
    assert data["upgrade_details"]["proposed_change"] == "new impl"
    assert data["upgrade_details"]["benefits"] == ["faster", "safer"]


@pytest.mark.anyio
async def test_create_task_with_model_effort(client: AsyncClient) -> None:
    """POST /tasks with model and effort stores both fields."""
    resp = await client.post("/tasks", json={
        **TASK_PAYLOAD,
        "model": "opus",
        "effort": "max",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "opus"
    assert data["effort"] == "max"


@pytest.mark.anyio
async def test_create_task_with_depends_on(client: AsyncClient) -> None:
    """POST /tasks with depends_on stores the dependency list."""
    resp = await client.post("/tasks", json={
        **TASK_PAYLOAD,
        "depends_on": ["T-other"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["depends_on"] == ["T-other"]


# -- JSONL replay edge cases -----------------------------------------------

@pytest.mark.anyio
async def test_replay_handles_empty_lines(jsonl_path: Path) -> None:
    """replay_jsonl skips blank lines between records without error."""
    record = {
        "id": "t1",
        "title": "Task one",
        "description": "d",
        "role": "backend",
        "status": "open",
    }
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("\n" + json.dumps(record) + "\n\n")

    store = TaskStore(jsonl_path)
    store.replay_jsonl()

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "t1"


@pytest.mark.anyio
async def test_replay_handles_malformed_json(jsonl_path: Path) -> None:
    """replay_jsonl skips corrupt lines and continues replaying the rest."""
    good = {"id": "t2", "title": "Good", "description": "d", "role": "backend", "status": "open"}
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(
        "not-valid-json\n"
        + json.dumps(good) + "\n"
        + "{broken\n"
    )

    store = TaskStore(jsonl_path)
    store.replay_jsonl()

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "t2"


@pytest.mark.anyio
async def test_replay_last_write_wins(jsonl_path: Path) -> None:
    """replay_jsonl applies later records for the same task id (last-write-wins)."""
    base = {"id": "t3", "title": "Task", "description": "d", "role": "backend", "status": "open"}
    update = {"id": "t3", "status": "done", "result_summary": "finished"}
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(json.dumps(base) + "\n" + json.dumps(update) + "\n")

    store = TaskStore(jsonl_path)
    store.replay_jsonl()

    task = store.get_task("t3")
    assert task is not None
    assert task.status.value == "done"
    assert task.result_summary == "finished"


@pytest.mark.anyio
async def test_replay_nonexistent_file(tmp_path: Path) -> None:
    """replay_jsonl is a no-op when the JSONL file does not exist."""
    missing_path = tmp_path / "nonexistent.jsonl"
    store = TaskStore(missing_path)
    store.replay_jsonl()  # must not raise

    assert store.list_tasks() == []


@pytest.mark.anyio
async def test_status_cost_skips_malformed_metrics_lines(
    client_with_metrics: AsyncClient, metrics_jsonl_path: Path
) -> None:
    """GET /status silently skips malformed lines in metrics JSONL."""
    metrics_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_jsonl_path.write_text(
        '{"task_id": "a", "role": "backend", "cost_usd": 1.0}\n'
        "not-json-at-all\n"
        '{"task_id": "b", "role": "backend"}\n'  # no cost_usd key
    )

    resp = await client_with_metrics.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert abs(data["total_cost_usd"] - 1.0) < 1e-9


# -- POST /bulletin ---------------------------------------------------------

@pytest.mark.anyio
async def test_post_bulletin_creates_message(client: AsyncClient) -> None:
    """POST /bulletin returns 201 with correct fields."""
    resp = await client.post("/bulletin", json={
        "agent_id": "agent-42",
        "type": "finding",
        "content": "Found a bug in the parser",
        "cell_id": "cell-1",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] == "agent-42"
    assert data["type"] == "finding"
    assert data["content"] == "Found a bug in the parser"
    assert data["cell_id"] == "cell-1"
    assert data["timestamp"] > 0


@pytest.mark.anyio
async def test_get_bulletin_since_filters(client: AsyncClient) -> None:
    """GET /bulletin?since=X only returns messages newer than X."""
    r1 = await client.post("/bulletin", json={
        "agent_id": "agent-1",
        "type": "status",
        "content": "First message",
    })
    ts_first = r1.json()["timestamp"]

    await client.post("/bulletin", json={
        "agent_id": "agent-2",
        "type": "status",
        "content": "Second message",
    })

    resp = await client.get("/bulletin", params={"since": ts_first})
    assert resp.status_code == 200
    messages = resp.json()
    contents = [m["content"] for m in messages]
    assert "Second message" in contents
    assert "First message" not in contents


@pytest.mark.anyio
async def test_get_bulletin_empty(client: AsyncClient) -> None:
    """GET /bulletin returns empty list when no messages exist."""
    resp = await client.get("/bulletin")
    assert resp.status_code == 200
    assert resp.json() == []


# -- POST /tasks/{id}/claim -------------------------------------------------

@pytest.mark.anyio
async def test_claim_by_id_sets_status(client: AsyncClient) -> None:
    """POST /tasks/{id}/claim changes status to claimed."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["status"] == "claimed"


@pytest.mark.anyio
async def test_claim_by_id_unknown_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/claim returns 404 for nonexistent task."""
    resp = await client.post("/tasks/nonexistent-id/claim")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_claim_by_id_already_claimed(client: AsyncClient) -> None:
    """Claiming an already-claimed task still returns the task."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
    resp = await client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["status"] == "claimed"


# -- GET /tasks/{id} --------------------------------------------------------

@pytest.mark.anyio
async def test_get_task_by_id(client: AsyncClient) -> None:
    """GET /tasks/{id} returns the task."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["title"] == TASK_PAYLOAD["title"]
    assert data["role"] == TASK_PAYLOAD["role"]


@pytest.mark.anyio
async def test_get_task_unknown(client: AsyncClient) -> None:
    """GET /tasks/{id} returns 404 for nonexistent task."""
    resp = await client.get("/tasks/no-such-task")
    assert resp.status_code == 404
