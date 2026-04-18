"""Tests for the Bernstein task server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bernstein.core.auth_rate_limiter import RequestRateLimitMiddleware
from bernstein.core.bulletin import BulletinBoard, MessageBoard
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import SSEBus, TaskStore, create_app


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):
    """Single FastAPI app shared across all tests in this module.

    ``create_app()`` is expensive (imports 15+ route modules, registers
    middleware stacks). Creating one per test exhausted the 2 GB
    RLIMIT_AS on CI runners.  Reusing a single instance keeps memory
    flat.
    """
    jsonl_path = tmp_path_factory.mktemp("server") / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


def _reset_app_state(app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Swap in fresh per-test state objects on a shared app instance."""
    jsonl_path = tmp_path / "tasks.jsonl"
    app.state.store = TaskStore(jsonl_path)
    app.state.bulletin = BulletinBoard()
    app.state.message_board = MessageBoard()
    app.state.sse_bus = SSEBus()
    app.state.draining = False
    app.state.sdd_dir = tmp_path
    app.state.runtime_dir = tmp_path
    # Clear rate limiter hit counters without rebuilding the middleware stack.
    # Rebuilding (middleware_stack = None) forces FastAPI to re-register all
    # routes + recreate pydantic validators, which leaks memory and eventually
    # hits the 2 GB RLIMIT_AS on CI.
    mw = app.middleware_stack
    while mw is not None:
        if isinstance(mw, RequestRateLimitMiddleware):
            mw._limiter._hits.clear()
            break
        mw = getattr(mw, "app", None)


@pytest.fixture()
def app(_module_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Per-test fixture: reuses the shared app but swaps in fresh state."""
    _reset_app_state(_module_app, tmp_path)
    return _module_app


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
    resp = await client.post(
        "/tasks",
        json={
            "title": "Minimal",
            "description": "A bare-minimum task",
            "role": "qa",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["priority"] == 2
    assert data["scope"] == "medium"
    assert data["complexity"] == "medium"
    # "A bare-minimum task" is trivial -> 10 mins
    assert data["estimated_minutes"] == 10
    assert data["depends_on"] == []


@pytest.mark.anyio
async def test_create_task_auto_estimate(client: AsyncClient) -> None:
    """POST /tasks auto-estimates difficulty from description."""
    # Complex task description
    resp = await client.post(
        "/tasks",
        json={
            "title": "Complex",
            "description": "We need to refactor the security module and architect a new database migration. func() func() func()",
            "role": "backend",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    # keywords: refactor, architect, security, database, migrate = 5 * 2 = 10
    # 3 func calls = 3
    # raw = 13+ -> high or critical
    assert data["estimated_minutes"] >= 90


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


@pytest.mark.anyio
async def test_status_includes_provider_status_snapshot(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    runtime_dir = app.state.sdd_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "provider_status.json").write_text(
        json.dumps(
            {
                "generated_at": 123.0,
                "providers": {
                    "codex": {
                        "health": "healthy",
                        "available": True,
                        "tier": "free",
                        "model": "gpt-5.4-mini",
                        "quota_snapshot": {"requests_per_minute": 120},
                    }
                },
            }
        )
    )

    resp = await client.get("/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["provider_status"]["providers"]["codex"]["quota_snapshot"]["requests_per_minute"] == 120


# -- Dependency validation --------------------------------------------------


@pytest.mark.anyio
async def test_depends_on_nonexistent_task(client: AsyncClient) -> None:
    """POST /tasks returns 422 when depends_on references a non-existent task."""
    resp = await client.post("/tasks", json={**TASK_PAYLOAD, "depends_on": ["deadbeef0000"]})
    assert resp.status_code == 422
    assert "non-existent" in resp.json()["detail"]


@pytest.mark.anyio
async def test_depends_on_valid_chain(client: AsyncClient) -> None:
    """POST /tasks succeeds for a valid A -> B dependency chain (no cycle)."""
    a = (await client.post("/tasks", json=TASK_PAYLOAD)).json()["id"]
    b_resp = await client.post("/tasks", json={**TASK_PAYLOAD, "depends_on": [a]})
    assert b_resp.status_code == 201
    assert b_resp.json()["depends_on"] == [a]


@pytest.mark.anyio
async def test_simple_cycle_rejected(client: AsyncClient) -> None:
    """POST /tasks returns 422 when a simple A -> B -> A cycle is detected."""
    # Create A with no deps, then B depending on A, then try to create A's twin
    # that depends on B — simulated by creating two independent tasks first,
    # then attempting a task that would close a cycle.
    a = (await client.post("/tasks", json=TASK_PAYLOAD)).json()["id"]
    (await client.post("/tasks", json={**TASK_PAYLOAD, "depends_on": [a]})).json()["id"]
    # Now attempt a task that depends on B and on A — not a cycle by itself.
    # To get a real cycle we need to create task C that depends on B,
    # then a task D that depends on C and C depends back on D — but tasks are
    # immutable after creation.  The real cycle case arises if task A was
    # somehow given B in its depends_on.  We test the direct cycle path via
    # a fresh pair: X depends on Y, Y depends on X (impossible via HTTP since Y
    # doesn't exist when X is created, but we can test X -> Y -> X transitively).
    # Create X, then Y depends on X, then Z depends on Y and X — valid chain.
    # Cycle: create P, Q depends on P, then attempt R that P itself depends on Q.
    # Since tasks are immutable we simulate with _detect_cycle directly.
    from bernstein.core.models import Task, TaskStatus, TaskType

    from bernstein.core.server import TaskStore

    p_id = "p" * 12
    q_id = "q" * 12

    def _make(tid: str, deps: list[str]) -> Task:
        return Task(
            id=tid,
            title="t",
            description="d",
            role="r",
            depends_on=deps,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
        )

    p = _make(p_id, [q_id])  # P depends on Q
    q = _make(q_id, [p_id])  # Q depends on P  — cycle!
    existing = {p_id: p}
    cycle = TaskStore._detect_cycle(existing, q)
    assert cycle is not None
    assert p_id in cycle and q_id in cycle


@pytest.mark.anyio
async def test_transitive_cycle_rejected(client: AsyncClient) -> None:
    """_detect_cycle finds A -> B -> C -> A transitive cycles."""
    from bernstein.core.models import Task, TaskStatus, TaskType

    from bernstein.core.server import TaskStore

    a_id, b_id, c_id = "a" * 12, "b" * 12, "c" * 12

    def _make(tid: str, deps: list[str]) -> Task:
        return Task(
            id=tid,
            title="t",
            description="d",
            role="r",
            depends_on=deps,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
        )

    a = _make(a_id, [c_id])  # A -> C
    b = _make(b_id, [a_id])  # B -> A
    c = _make(c_id, [b_id])  # C -> B  => A -> C -> B -> A

    existing = {a_id: a, b_id: b}
    cycle = TaskStore._detect_cycle(existing, c)
    assert cycle is not None
    # All three IDs must appear in the cycle path
    assert a_id in cycle and b_id in cycle and c_id in cycle


@pytest.mark.anyio
async def test_no_cycle_for_valid_chain(client: AsyncClient) -> None:
    """_detect_cycle returns None for a simple linear chain."""
    from bernstein.core.models import Task, TaskStatus, TaskType

    from bernstein.core.server import TaskStore

    a_id, b_id, c_id = "aa" * 6, "bb" * 6, "cc" * 6

    def _make(tid: str, deps: list[str]) -> Task:
        return Task(
            id=tid,
            title="t",
            description="d",
            role="r",
            depends_on=deps,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
        )

    a = _make(a_id, [])
    b = _make(b_id, [a_id])
    c = _make(c_id, [b_id])

    existing = {a_id: a, b_id: b}
    assert TaskStore._detect_cycle(existing, c) is None


# -- POST /tasks/{task_id}/complete -----------------------------------------


@pytest.mark.anyio
async def test_complete_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/complete marks task as done."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
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


@pytest.mark.anyio
async def test_complete_empty_summary_auto_fails_task(client: AsyncClient) -> None:
    """audit-028: POST /tasks/{id}/complete with empty summary auto-fails the task.

    Instead of leaving the task stuck in CLAIMED (old behaviour) the route
    must transition the task to FAILED so the slot is released, then
    surface a 422 describing what happened.
    """
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]
    await client.post(f"/tasks/{task_id}/claim")

    resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": ""},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "empty_result_summary"
    assert detail["task_id"] == task_id
    assert detail["reason"] == "completion missing summary"
    assert detail["status"] == "failed"

    # Confirm the task is actually failed on the server side so a fresh
    # agent cannot re-claim the already-committed work.
    follow = await client.get(f"/tasks/{task_id}")
    assert follow.status_code == 200
    assert follow.json()["status"] == "failed"
    assert follow.json()["result_summary"] == "completion missing summary"


# -- POST /tasks/{task_id}/fail ---------------------------------------------


@pytest.mark.anyio
async def test_fail_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/fail marks task as failed."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
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


# -- POST /tasks/{task_id}/cancel ------------------------------------------


@pytest.mark.anyio
async def test_cancel_open_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel cancels an open task."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/tasks/{task_id}/cancel",
        json={"reason": "no longer needed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["result_summary"] == "no longer needed"


@pytest.mark.anyio
async def test_cancel_claimed_task(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel cancels a claimed task."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]
    await client.get("/tasks/next/backend")  # claim it

    resp = await client.post(f"/tasks/{task_id}/cancel", json={"reason": "abort"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.anyio
async def test_cancel_done_task_returns_409(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel returns 409 if task is already done."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]
    await client.post(f"/tasks/{task_id}/claim")
    await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"})

    resp = await client.post(f"/tasks/{task_id}/cancel", json={"reason": "too late"})
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_cancel_unknown_task_returns_404(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel returns 404 for unknown task id."""
    resp = await client.post("/tasks/nonexistent/cancel", json={"reason": "nope"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_cancel_no_reason(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel works without a reason body."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.anyio
async def test_cancel_task_cascade_cancels_children(client: AsyncClient) -> None:
    """POST /tasks/{id}/cancel cascades to open subtasks (audit-021)."""
    parent_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    assert parent_resp.status_code == 201
    parent_id = parent_resp.json()["id"]

    # Create two subtasks via the self-create endpoint (links parent_task_id).
    child_ids: list[str] = []
    for i in range(2):
        sub_resp = await client.post(
            "/tasks/self-create",
            json={
                "parent_task_id": parent_id,
                "title": f"Subtask {i}",
                "description": f"Child task {i}",
                "role": "backend",
                "priority": 2,
            },
        )
        assert sub_resp.status_code == 201
        child_ids.append(sub_resp.json()["id"])

    # Cancel the parent and assert every child is now cancelled too.
    cancel_resp = await client.post(
        f"/tasks/{parent_id}/cancel",
        json={"reason": "project scrapped"},
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"
    assert cancel_resp.json()["id"] == parent_id

    for child_id in child_ids:
        child_resp = await client.get(f"/tasks/{child_id}")
        assert child_resp.status_code == 200
        assert child_resp.json()["status"] == "cancelled", (
            f"child {child_id} still has status {child_resp.json()['status']}"
        )


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
    await client.post(f"/tasks/{task_id}/claim")
    await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "ok"})

    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Still open"})

    resp = await client.get("/tasks", params={"status": "open"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Still open"


# -- GET /tasks (pagination) -----------------------------------------------


@pytest.mark.anyio
async def test_list_tasks_paginated(client: AsyncClient) -> None:
    """GET /tasks?limit=1&offset=0 returns paginated envelope."""
    await client.post("/tasks", json=TASK_PAYLOAD)
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Second"})
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Third"})

    resp = await client.get("/tasks", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert "tasks" in data
    assert "total" in data
    assert data["total"] == 3
    assert len(data["tasks"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


@pytest.mark.anyio
async def test_list_tasks_paginated_offset(client: AsyncClient) -> None:
    """GET /tasks?limit=2&offset=2 returns the remaining tasks."""
    for i in range(5):
        await client.post("/tasks", json={**TASK_PAYLOAD, "title": f"Task {i}"})

    resp = await client.get("/tasks", params={"limit": 2, "offset": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["tasks"]) == 2
    assert data["offset"] == 2


@pytest.mark.anyio
async def test_list_tasks_paginated_beyond_end(client: AsyncClient) -> None:
    """GET /tasks?limit=10&offset=100 returns empty page when offset exceeds total."""
    await client.post("/tasks", json=TASK_PAYLOAD)

    resp = await client.get("/tasks", params={"limit": 10, "offset": 100})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["tasks"]) == 0


@pytest.mark.anyio
async def test_list_tasks_paginated_with_status_filter(client: AsyncClient) -> None:
    """Pagination works together with status filter."""
    for i in range(3):
        await client.post("/tasks", json={**TASK_PAYLOAD, "title": f"Task {i}"})
    # Complete one task so it's not 'open' anymore
    create_resp = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "done"})
    tid = create_resp.json()["id"]
    await client.post(f"/tasks/{tid}/claim")
    await client.post(f"/tasks/{tid}/complete", json={"result_summary": "ok"})

    resp = await client.get("/tasks", params={"status": "open", "limit": 2, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3  # only open tasks
    assert len(data["tasks"]) == 2


@pytest.mark.anyio
async def test_list_tasks_legacy_format_without_pagination(client: AsyncClient) -> None:
    """GET /tasks without limit/offset returns a flat list (backward compat)."""
    await client.post("/tasks", json=TASK_PAYLOAD)
    await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Second"})

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    data = resp.json()
    # Legacy: plain list, not a dict with "tasks" key
    assert isinstance(data, list)
    assert len(data) == 2


@pytest.mark.anyio
async def test_list_tasks_limit_clamped_to_max(client: AsyncClient) -> None:
    """GET /tasks?limit=9999 is clamped to 500."""
    await client.post("/tasks", json=TASK_PAYLOAD)

    resp = await client.get("/tasks", params={"limit": 9999})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 500


# -- GET /tasks/counts ------------------------------------------------------


@pytest.mark.anyio
async def test_task_counts_empty(client: AsyncClient) -> None:
    """GET /tasks/counts returns all zeros when no tasks exist."""
    resp = await client.get("/tasks/counts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["open"] == 0
    assert data["claimed"] == 0
    assert data["done"] == 0
    assert data["failed"] == 0
    assert data["total"] == 0


@pytest.mark.anyio
async def test_task_counts_reflects_statuses(client: AsyncClient) -> None:
    """GET /tasks/counts accurately reflects task status distribution."""
    # Create 3 open tasks
    for i in range(3):
        await client.post("/tasks", json={**TASK_PAYLOAD, "title": f"Task {i}"})

    # Claim and complete one
    create_resp = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "complete-me"})
    tid = create_resp.json()["id"]
    await client.post(f"/tasks/{tid}/claim")
    await client.post(f"/tasks/{tid}/complete", json={"result_summary": "ok"})

    # Claim and fail one
    create_resp2 = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "fail-me"})
    tid2 = create_resp2.json()["id"]
    await client.post(f"/tasks/{tid2}/claim")
    await client.post(f"/tasks/{tid2}/fail", json={"reason": "boom"})

    resp = await client.get("/tasks/counts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["open"] == 3
    assert data["done"] == 1
    assert data["failed"] == 1
    assert data["total"] == 5


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
    await client.post(f"/tasks/{r1.json()['id']}/claim")
    await client.post(f"/tasks/{r1.json()['id']}/complete", json={"result_summary": "ok"})
    await client.post(f"/tasks/{r2.json()['id']}/claim")
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


@pytest.mark.anyio
async def test_health_includes_component_statuses(client: AsyncClient) -> None:
    """GET /health exposes component-level status details."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    assert set(data["components"].keys()) == {"server", "spawner", "database", "agents"}


@pytest.mark.anyio
async def test_ready_returns_200_when_accepting_claims(client: AsyncClient) -> None:
    """/ready returns 200 when not draining/read-only."""
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.anyio
async def test_ready_returns_503_when_draining(app, client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """/ready returns 503 when the server is draining."""
    app.state.draining = True  # pyright: ignore[reportUnknownMemberType]
    resp = await client.get("/ready")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["status"] == "not_ready"
    assert payload["reason"] == "draining"


@pytest.mark.anyio
async def test_alive_returns_200(client: AsyncClient) -> None:
    """GET /alive returns process liveness."""
    resp = await client.get("/alive")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


# -- JSONL persistence ------------------------------------------------------


@pytest.mark.anyio
async def test_jsonl_written(jsonl_path: Path) -> None:
    """Task records land on disk after flush_buffer()."""
    from bernstein.core.server import TaskCreate

    store = TaskStore(jsonl_path=jsonl_path)
    req = TaskCreate(
        title="Implement parser",
        description="Write the YAML parser module",
        role="backend",
        priority=2,
    )
    await store.create(req)
    await store.flush_buffer()
    assert jsonl_path.exists()
    lines = [ln for ln in jsonl_path.read_text().splitlines() if ln.strip()]
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
    store._agents["old-agent"] = __import__("bernstein.core.models", fromlist=["AgentSession"]).AgentSession(
        id="old-agent",
        role="backend",
        heartbeat_ts=0.0,  # epoch — definitely stale
        status="working",
    )
    count = store.mark_stale_dead()
    assert count == 1
    assert store._agents["old-agent"].status == "dead"


# -- completion_signals API round-trip ----------------------------------------


@pytest.mark.anyio
async def test_create_task_with_completion_signals_stored(client: AsyncClient) -> None:
    """POST /tasks with completion_signals stores them and returns them."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [
            {"type": "path_exists", "value": "src/foo.py"},
            {"type": "file_contains", "value": "def main"},
        ],
    }
    resp = await client.post("/tasks", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    signals = data["completion_signals"]
    assert len(signals) == 2
    assert signals[0] == {"type": "path_exists", "value": "src/foo.py"}
    assert signals[1] == {"type": "file_contains", "value": "def main"}


@pytest.mark.anyio
async def test_get_task_returns_completion_signals(client: AsyncClient) -> None:
    """GET /tasks/{id} returns completion_signals that were set on creation."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [
            {"type": "glob_exists", "value": "tests/**/*.py"},
        ],
    }
    create_resp = await client.post("/tasks", json=payload)
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["completion_signals"] == [{"type": "glob_exists", "value": "tests/**/*.py"}]


@pytest.mark.anyio
async def test_all_six_signal_types_accepted(client: AsyncClient) -> None:
    """POST /tasks accepts all 6 completion signal types."""
    all_signals = [
        {"type": "path_exists", "value": "src/main.py"},
        {"type": "glob_exists", "value": "dist/**/*.js"},
        {"type": "test_passes", "value": "uv run pytest tests/ -x -q"},
        {"type": "file_contains", "value": "class MyClass"},
        {"type": "llm_review", "value": "Verify the implementation is correct"},
        {"type": "llm_judge", "value": "Does the output satisfy the requirements?"},
    ]
    payload = {**TASK_PAYLOAD, "completion_signals": all_signals}
    resp = await client.post("/tasks", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    returned = data["completion_signals"]
    assert len(returned) == 6
    returned_by_type = {s["type"]: s["value"] for s in returned}
    assert returned_by_type["path_exists"] == "src/main.py"
    assert returned_by_type["glob_exists"] == "dist/**/*.js"
    assert returned_by_type["test_passes"] == "uv run pytest tests/ -x -q"
    assert returned_by_type["file_contains"] == "class MyClass"
    assert returned_by_type["llm_review"] == "Verify the implementation is correct"
    assert returned_by_type["llm_judge"] == "Does the output satisfy the requirements?"


@pytest.mark.anyio
async def test_empty_completion_signals_backward_compat(client: AsyncClient) -> None:
    """POST /tasks without completion_signals defaults to empty list."""
    resp = await client.post("/tasks", json=TASK_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["completion_signals"] == []


@pytest.mark.anyio
async def test_invalid_signal_type_rejected(client: AsyncClient) -> None:
    """POST /tasks with an invalid signal type returns 422."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [{"type": "not_a_real_signal", "value": "whatever"}],
    }
    resp = await client.post("/tasks", json=payload)
    assert resp.status_code == 422


# -- POST /tasks/{id}/progress -------------------------------------------------


@pytest.mark.anyio
async def test_progress_updates_stored_and_retrievable(client: AsyncClient) -> None:
    """POST /tasks/{id}/progress stores entries that appear in GET /tasks/{id}."""
    # Create task
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    assert create_resp.status_code == 201
    task_id = create_resp.json()["id"]

    # Post first progress update
    resp1 = await client.post(f"/tasks/{task_id}/progress", json={"message": "Parsing started", "percent": 10})
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert len(data1["progress_log"]) == 1
    assert data1["progress_log"][0]["message"] == "Parsing started"
    assert data1["progress_log"][0]["percent"] == 10
    assert "timestamp" in data1["progress_log"][0]

    # Post second progress update
    resp2 = await client.post(f"/tasks/{task_id}/progress", json={"message": "Half done", "percent": 50})
    assert resp2.status_code == 200
    assert len(resp2.json()["progress_log"]) == 2

    # Verify via GET /tasks/{id}
    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    log = get_resp.json()["progress_log"]
    assert len(log) == 2
    assert log[0]["message"] == "Parsing started"
    assert log[0]["percent"] == 10
    assert log[1]["message"] == "Half done"
    assert log[1]["percent"] == 50

    # Also appears in GET /tasks list
    list_resp = await client.get("/tasks")
    tasks = {t["id"]: t for t in list_resp.json()}
    assert len(tasks[task_id]["progress_log"]) == 2


@pytest.mark.anyio
async def test_progress_on_missing_task_returns_404(client: AsyncClient) -> None:
    """POST /tasks/{id}/progress returns 404 for unknown task id."""
    resp = await client.post("/tasks/nonexistent/progress", json={"message": "nope", "percent": 0})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_completion_signals_preserved_after_complete(client: AsyncClient) -> None:
    """Completing a task does not discard completion_signals."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [{"type": "test_passes", "value": "pytest"}],
    }
    create_resp = await client.post("/tasks", json=payload)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
    complete_resp = await client.post(
        f"/tasks/{task_id}/complete",
        json={"result_summary": "done"},
    )
    assert complete_resp.status_code == 200
    assert complete_resp.json()["completion_signals"] == [{"type": "test_passes", "value": "pytest"}]


@pytest.mark.anyio
async def test_list_tasks_returns_completion_signals(client: AsyncClient) -> None:
    """GET /tasks includes completion_signals for each task in the list."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [{"type": "path_exists", "value": "README.md"}],
    }
    await client.post("/tasks", json=payload)

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["completion_signals"] == [{"type": "path_exists", "value": "README.md"}]


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

    await client.post(f"/tasks/{task_id}/claim")
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

    await client.post(f"/tasks/{task_id}/claim")
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
    await client.post(f"/tasks/{r1.json()['id']}/claim")
    await client.post(f"/tasks/{r1.json()['id']}/complete", json={"result_summary": "ok"})
    await client.post(f"/tasks/{r2.json()['id']}/claim")
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
        await client.post(f"/tasks/{r.json()['id']}/claim")
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
    assert data["total_cost_usd"] == pytest.approx(0.0)


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
async def test_status_cost_per_role_breakdown(client_with_metrics: AsyncClient, metrics_jsonl_path: Path) -> None:
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
        "rollback_plan": {
            "steps": ["revert commit"],
            "revert_commit": None,
            "data_migration": "",
            "estimated_rollback_minutes": 30,
        },
        "cost_estimate_usd": 0.5,
        "performance_impact": "minor",
    }
    resp = await client.post(
        "/tasks",
        json={
            **TASK_PAYLOAD,
            "task_type": "upgrade_proposal",
            "upgrade_details": upgrade_details,
        },
    )
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
    resp = await client.post(
        "/tasks",
        json={
            **TASK_PAYLOAD,
            "model": "opus",
            "effort": "max",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "opus"
    assert data["effort"] == "max"


@pytest.mark.anyio
async def test_create_task_with_depends_on(client: AsyncClient) -> None:
    """POST /tasks with depends_on stores the dependency list."""
    dep = (await client.post("/tasks", json=TASK_PAYLOAD)).json()["id"]
    resp = await client.post(
        "/tasks",
        json={
            **TASK_PAYLOAD,
            "depends_on": [dep],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["depends_on"] == [dep]


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
    jsonl_path.write_text("not-valid-json\n" + json.dumps(good) + "\n" + "{broken\n")

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


# -- read_archive reverse-seek ------------------------------------------------


def _make_archive(path: Path, count: int) -> list[dict[str, Any]]:
    """Write *count* archive records to *path*, return the list."""
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "task_id": f"task-{i:04d}",
            "title": f"Task {i}",
            "role": "backend",
            "status": "done",
            "result_summary": f"result {i}",
            "completed_at": time.time() + i,
            "duration_seconds": float(i),
        }
        for i in range(count)
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return records


def test_read_archive_large_file_returns_limit(tmp_path: Path) -> None:
    """read_archive(limit=10) on a 500-entry file returns exactly 10 records."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    _make_archive(archive_path, 500)

    store = TaskStore(tmp_path / "tasks.jsonl")
    store._archive_path = archive_path

    result = store.read_archive(limit=10)

    assert len(result) == 10
    # Should be the last 10 (oldest-first ordering preserved)
    expected_ids = [f"task-{i:04d}" for i in range(490, 500)]
    assert [r["task_id"] for r in result] == expected_ids


def test_read_archive_empty_file(tmp_path: Path) -> None:
    """read_archive on an empty file returns []."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text("")

    store = TaskStore(tmp_path / "tasks.jsonl")
    store._archive_path = archive_path

    assert store.read_archive(limit=10) == []


def test_read_archive_fewer_lines_than_limit(tmp_path: Path) -> None:
    """read_archive returns all lines when fewer than limit exist."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    _make_archive(archive_path, 5)

    store = TaskStore(tmp_path / "tasks.jsonl")
    store._archive_path = archive_path

    result = store.read_archive(limit=50)
    assert len(result) == 5


def test_read_archive_malformed_lines_skipped(tmp_path: Path) -> None:
    """read_archive skips malformed JSON lines and continues."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"task_id": "t1", "status": "done"})
    archive_path.write_text(f"{good}\nnot-json\n{good}\n")

    store = TaskStore(tmp_path / "tasks.jsonl")
    store._archive_path = archive_path

    result = store.read_archive(limit=10)
    assert len(result) == 2
    assert all(r["task_id"] == "t1" for r in result)


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
    resp = await client.post(
        "/bulletin",
        json={
            "agent_id": "agent-42",
            "type": "finding",
            "content": "Found a bug in the parser",
            "cell_id": "cell-1",
        },
    )
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
    r1 = await client.post(
        "/bulletin",
        json={
            "agent_id": "agent-1",
            "type": "status",
            "content": "First message",
        },
    )
    ts_first = r1.json()["timestamp"]

    await client.post(
        "/bulletin",
        json={
            "agent_id": "agent-2",
            "type": "status",
            "content": "Second message",
        },
    )

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
    """Claiming an already-claimed task returns 409 Conflict (audit-014).

    Previously the server silently re-returned the unchanged task,
    enabling double-claim — two agents both believed they owned the
    same task. The second claim must now fail with 409.
    """
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    first = await client.post(f"/tasks/{task_id}/claim")
    assert first.status_code == 200
    assert first.json()["status"] == "claimed"

    resp = await client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 409
    assert "not open" in resp.json()["detail"]


# -- POST /tasks/claim-batch ------------------------------------------------


@pytest.mark.anyio
async def test_claim_batch_all_succeed(client: AsyncClient) -> None:
    """POST /tasks/claim-batch claims all listed open tasks."""
    r1 = await client.post("/tasks", json=TASK_PAYLOAD)
    r2 = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Task B"})
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]

    resp = await client.post(
        "/tasks/claim-batch",
        json={"task_ids": [id1, id2], "agent_id": "agent-42"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert sorted(data["claimed"]) == sorted([id1, id2])
    assert data["failed"] == []


@pytest.mark.anyio
async def test_claim_batch_partial_failure(client: AsyncClient) -> None:
    """POST /tasks/claim-batch skips already-claimed tasks."""
    r1 = await client.post("/tasks", json=TASK_PAYLOAD)
    r2 = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Task B"})
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]

    # Pre-claim id2
    await client.post(f"/tasks/{id2}/claim")

    resp = await client.post(
        "/tasks/claim-batch",
        json={"task_ids": [id1, id2], "agent_id": "agent-42"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["claimed"] == [id1]
    assert data["failed"] == [id2]


@pytest.mark.anyio
async def test_claim_batch_unknown_ids(client: AsyncClient) -> None:
    """POST /tasks/claim-batch reports unknown IDs as failed."""
    resp = await client.post(
        "/tasks/claim-batch",
        json={"task_ids": ["no-such-id"], "agent_id": "agent-99"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["claimed"] == []
    assert data["failed"] == ["no-such-id"]


@pytest.mark.anyio
async def test_claim_batch_sets_agent_id(client: AsyncClient) -> None:
    """POST /tasks/claim-batch stores the agent_id on claimed tasks."""
    r1 = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = r1.json()["id"]

    await client.post(
        "/tasks/claim-batch",
        json={"task_ids": [task_id], "agent_id": "agent-xyz"},
    )

    task_resp = await client.get(f"/tasks/{task_id}")
    assert task_resp.json()["assigned_agent"] == "agent-xyz"


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


# -- invalid payload handling -----------------------------------------------


@pytest.mark.anyio
async def test_create_task_with_invalid_scope(client: AsyncClient) -> None:
    """POST /tasks with scope='bogus' should not succeed (raises ValueError or returns non-201)."""
    try:
        resp = await client.post("/tasks", json={**TASK_PAYLOAD, "scope": "bogus"})
    except ValueError:
        pass  # ValueError propagated through ASGI transport — server rejected input
    else:
        assert resp.status_code != 201, f"Expected rejection for invalid scope, got {resp.status_code}"


@pytest.mark.anyio
async def test_create_task_with_invalid_complexity(client: AsyncClient) -> None:
    """POST /tasks with complexity='bogus' should not succeed (raises ValueError or returns non-201)."""
    try:
        resp = await client.post("/tasks", json={**TASK_PAYLOAD, "complexity": "bogus"})
    except ValueError:
        pass  # ValueError propagated through ASGI transport — server rejected input
    else:
        assert resp.status_code != 201, f"Expected rejection for invalid complexity, got {resp.status_code}"


@pytest.mark.anyio
async def test_complete_already_completed_task(client: AsyncClient) -> None:
    """Completing an already-done task returns 409 — DONE->DONE is illegal."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
    await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "First"})
    resp = await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "Second"})

    assert resp.status_code == 409


@pytest.mark.anyio
async def test_fail_already_failed_task(client: AsyncClient) -> None:
    """Failing an already-failed task returns 409 — FAILED->FAILED is illegal."""
    create_resp = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = create_resp.json()["id"]

    await client.post(f"/tasks/{task_id}/claim")
    await client.post(f"/tasks/{task_id}/fail", json={"reason": "First"})
    resp = await client.post(f"/tasks/{task_id}/fail", json={"reason": "Second"})

    assert resp.status_code == 409


@pytest.mark.anyio
async def test_claim_next_skips_tasks_with_unmet_deps(client: AsyncClient) -> None:
    """Tasks with unmet deps are hidden from the open listing and become claimable once deps are done.

    This verifies that:
    1. A dep-blocked task is absent from GET /tasks?status=open while its dep is open.
    2. After the dep is completed, GET /tasks/next/{role} can claim the previously blocked task.
    """
    # Create the dependency task (qa role — stays open, different from backend)
    dep_resp = await client.post("/tasks", json={**TASK_PAYLOAD, "role": "qa", "title": "Dep task"})
    dep_id = dep_resp.json()["id"]

    # Create a backend task that depends on the qa dep
    blocked_resp = await client.post(
        "/tasks",
        json={
            **TASK_PAYLOAD,
            "title": "Blocked task",
            "depends_on": [dep_id],
        },
    )
    blocked_id = blocked_resp.json()["id"]

    # Before dep is done: blocked task should NOT appear in the open listing
    open_resp = await client.get("/tasks", params={"status": "open"})
    open_ids = {t["id"] for t in open_resp.json()}
    assert blocked_id not in open_ids, "dep-blocked task should be hidden while dep is open"

    # Complete the dep — now blocked task's dep is satisfied
    await client.post(f"/tasks/{dep_id}/claim")
    await client.post(f"/tasks/{dep_id}/complete", json={"result_summary": "done"})

    # After dep is done: task should be claimable via next-task endpoint
    claim_resp = await client.get("/tasks/next/backend")
    assert claim_resp.status_code == 200
    assert claim_resp.json()["id"] == blocked_id


@pytest.mark.anyio
async def test_create_task_with_completion_signals(client: AsyncClient) -> None:
    """POST /tasks with completion_signals field is accepted and task is created."""
    payload = {
        **TASK_PAYLOAD,
        "completion_signals": [
            {"type": "path_exists", "value": "src/foo.py"},
            {"type": "test_passes", "value": "uv run pytest tests/ -q"},
        ],
    }
    resp = await client.post("/tasks", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == TASK_PAYLOAD["title"]
    assert data["status"] == "open"


@pytest.mark.anyio
async def test_heartbeat_unknown_agent(client: AsyncClient) -> None:
    """POST /agents/{id}/heartbeat for a never-seen agent should not crash."""
    resp = await client.post(
        "/agents/brand-new-agent/heartbeat",
        json={"role": "backend", "status": "starting"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "brand-new-agent"
    assert data["acknowledged"] is True


# -- dependency filtering ---------------------------------------------------


@pytest.mark.anyio
async def test_dependency_blocks_open_listing(client: AsyncClient) -> None:
    """Task B with depends_on=[A.id] is hidden from GET /tasks?status=open until A is done."""
    # Create task A
    resp_a = await client.post("/tasks", json={**TASK_PAYLOAD, "title": "Task A"})
    assert resp_a.status_code == 201
    task_a_id = resp_a.json()["id"]

    # Create task B that depends on A
    resp_b = await client.post(
        "/tasks",
        json={
            **TASK_PAYLOAD,
            "title": "Task B",
            "depends_on": [task_a_id],
        },
    )
    assert resp_b.status_code == 201
    task_b_id = resp_b.json()["id"]

    # B should NOT appear in open tasks while A is not done
    resp = await client.get("/tasks", params={"status": "open"})
    open_ids = {t["id"] for t in resp.json()}
    assert task_a_id in open_ids
    assert task_b_id not in open_ids

    # Mark A as done
    await client.post(f"/tasks/{task_a_id}/claim")
    complete_resp = await client.post(
        f"/tasks/{task_a_id}/complete",
        json={"result_summary": "done"},
    )
    assert complete_resp.status_code == 200

    # B should now appear in open tasks
    resp2 = await client.get("/tasks", params={"status": "open"})
    open_ids2 = {t["id"] for t in resp2.json()}
    assert task_b_id in open_ids2


# -- Incremental metrics parsing (byte offset) --------------------------------


def test_read_cost_by_role_incremental_offset(tmp_path: Path) -> None:
    """_read_cost_by_role() only parses new lines on the second call.

    We verify seek-position behaviour by inspecting _cost_cache_offset:
    after each call the offset must equal the cumulative byte length of all
    records seen so far, proving only new bytes were consumed.
    """
    import os
    import pathlib
    from unittest.mock import patch

    jsonl = tmp_path / "tasks.jsonl"
    jsonl.touch()
    metrics_jsonl = tmp_path / "metrics.jsonl"
    metrics_jsonl.touch()

    store = TaskStore(jsonl_path=jsonl, metrics_jsonl_path=metrics_jsonl)

    # First append: one record for 'backend'
    record1 = json.dumps({"role": "backend", "cost_usd": 0.10}) + "\n"
    metrics_jsonl.write_bytes(record1.encode())

    mtime1 = metrics_jsonl.stat().st_mtime + 1
    os.utime(metrics_jsonl, (mtime1, mtime1))
    store._cost_cache_mtime = 0.0  # force miss

    result1 = store._read_cost_by_role()
    assert result1 == {"backend": pytest.approx(0.10)}
    offset_after_first = store._cost_cache_offset
    assert offset_after_first == len(record1.encode())

    # Second append: another record for 'qa'
    record2 = json.dumps({"role": "qa", "cost_usd": 0.05}) + "\n"
    with metrics_jsonl.open("ab") as fh:
        fh.write(record2.encode())

    mtime2 = metrics_jsonl.stat().st_mtime + 1
    os.utime(metrics_jsonl, (mtime2, mtime2))
    store._cost_cache_mtime = mtime1  # simulate prior cached mtime

    # Spy on Path.open to capture the seek offset used on the second call
    _real_path_open = pathlib.Path.open
    seek_positions: list[int] = []

    def _spy_path_open(self: pathlib.Path, mode: str = "r", **kwargs):  # type: ignore[no-untyped-def]
        fh = _real_path_open(self, mode, **kwargs)
        if "b" in mode and self == metrics_jsonl:
            _real_seek = fh.seek

            def _tracking_seek(pos: int, *args: object) -> int:  # type: ignore[no-untyped-def]
                seek_positions.append(pos)
                return _real_seek(pos, *args)

            fh.seek = _tracking_seek  # type: ignore[method-assign]
        return fh

    with patch.object(pathlib.Path, "open", _spy_path_open):
        result2 = store._read_cost_by_role()

    # Both roles should now be in the merged cache
    assert result2["backend"] == pytest.approx(0.10)
    assert result2["qa"] == pytest.approx(0.05)

    # The offset must have advanced by exactly len(record2)
    expected_final_offset = len(record1.encode()) + len(record2.encode())
    assert store._cost_cache_offset == expected_final_offset

    # The spy must have captured a seek to the post-first-record offset,
    # proving we did NOT re-read from the beginning.
    assert seek_positions, "expected Path.open().seek() to be called"
    assert seek_positions[0] == offset_after_first


def test_read_cost_by_role_truncation_reset(tmp_path: Path) -> None:
    """When the metrics file is truncated, offset resets and cache clears."""
    import os

    jsonl = tmp_path / "tasks.jsonl"
    jsonl.touch()
    metrics_jsonl = tmp_path / "metrics.jsonl"

    store = TaskStore(jsonl_path=jsonl, metrics_jsonl_path=metrics_jsonl)

    # Write initial data and prime the cache
    record = json.dumps({"role": "backend", "cost_usd": 1.00}) + "\n"
    metrics_jsonl.write_bytes(record.encode())

    mtime1 = metrics_jsonl.stat().st_mtime + 1
    os.utime(metrics_jsonl, (mtime1, mtime1))
    store._cost_cache_mtime = 0.0

    store._read_cost_by_role()
    assert store._cost_cache_offset > 0

    # Simulate truncation: offset is now beyond file size
    store._cost_cache_offset = 99999
    store._cost_cache_mtime = 0.0  # force re-read

    # Write a smaller replacement file
    new_record = json.dumps({"role": "qa", "cost_usd": 0.25}) + "\n"
    metrics_jsonl.write_bytes(new_record.encode())
    mtime2 = metrics_jsonl.stat().st_mtime + 2
    os.utime(metrics_jsonl, (mtime2, mtime2))

    result = store._read_cost_by_role()
    # Cache should have been reset; old 'backend' cost gone, only new 'qa'
    assert "backend" not in result
    assert result.get("qa") == pytest.approx(0.25)
    assert store._cost_cache_offset == len(new_record.encode())


# -- write buffering -----------------------------------------------------------


@pytest.mark.anyio
async def test_jsonl_write_buffering(tmp_path: Path) -> None:
    """Every mutation must flush to disk immediately (_BUFFER_MAX=1).

    With immediate flushing, 20 creates produce exactly 20 file-open
    operations — no data is left in the buffer that could be lost on crash.
    """
    jsonl = tmp_path / "tasks.jsonl"
    store = TaskStore(jsonl_path=jsonl)

    open_count = 0
    real_open = Path.open

    def counting_open(self: Path, mode: str = "r", **kwargs):  # type: ignore[override]
        nonlocal open_count
        if self == jsonl and "a" in mode:
            open_count += 1
        return real_open(self, mode, **kwargs)

    with patch.object(Path, "open", counting_open):
        from bernstein.core.server import TaskCreate

        for i in range(20):
            req = TaskCreate(title=f"Task {i}", description="d", role="backend")
            await store.create(req)
        await store.flush_buffer()

    assert open_count == 20, f"Expected exactly 20 file opens (immediate flush), got {open_count}"
    # All 20 records must be on disk after the flush
    lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
    assert len(lines) == 20


# ---------------------------------------------------------------------------
# Cluster API endpoint tests
# ---------------------------------------------------------------------------

from bernstein.core.models import ClusterConfig


@pytest.fixture(scope="module")
def _module_cluster_app(tmp_path_factory: pytest.TempPathFactory):
    """Single cluster-mode app shared across cluster tests."""
    jsonl_path = tmp_path_factory.mktemp("cluster") / "tasks.jsonl"
    return create_app(
        jsonl_path=jsonl_path,
        cluster_config=ClusterConfig(enabled=True),
    )


@pytest.fixture()
def cluster_app(_module_cluster_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Per-test: fresh store for cluster app."""
    _reset_app_state(_module_cluster_app, tmp_path)
    _module_cluster_app.state.node_registry._nodes.clear()
    return _module_cluster_app


@pytest.fixture()
async def cluster_client(cluster_app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=cluster_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


NODE_PAYLOAD = {
    "name": "worker-1",
    "url": "http://worker1:8052",
    "capacity": {
        "max_agents": 4,
        "available_slots": 4,
        "active_agents": 0,
        "gpu_available": False,
        "supported_models": ["sonnet", "opus"],
    },
    "labels": {"region": "us-east"},
    "cell_ids": [],
}


@pytest.mark.anyio
async def test_register_node(cluster_client: AsyncClient) -> None:
    """POST /cluster/nodes registers a node and returns 201."""
    resp = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "worker-1"
    assert data["status"] == "online"
    assert data["id"]


@pytest.mark.anyio
async def test_list_nodes_empty(cluster_client: AsyncClient) -> None:
    """GET /cluster/nodes returns [] when no nodes registered."""
    resp = await cluster_client.get("/cluster/nodes")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_list_nodes_after_register(cluster_client: AsyncClient) -> None:
    """GET /cluster/nodes returns registered nodes."""
    await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    await cluster_client.post("/cluster/nodes", json={**NODE_PAYLOAD, "name": "worker-2", "url": "http://w2:8052"})
    resp = await cluster_client.get("/cluster/nodes")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.anyio
async def test_node_heartbeat(cluster_client: AsyncClient) -> None:
    """POST /cluster/nodes/{id}/heartbeat updates capacity."""
    reg = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    node_id = reg.json()["id"]

    hb_payload = {"capacity": {**NODE_PAYLOAD["capacity"], "available_slots": 2, "active_agents": 2}}
    resp = await cluster_client.post(f"/cluster/nodes/{node_id}/heartbeat", json=hb_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["capacity"]["available_slots"] == 2
    assert data["capacity"]["active_agents"] == 2


@pytest.mark.anyio
async def test_node_heartbeat_unknown(cluster_client: AsyncClient) -> None:
    """POST heartbeat for unknown node returns 404."""
    resp = await cluster_client.post("/cluster/nodes/no-such-node/heartbeat", json={})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unregister_node(cluster_client: AsyncClient) -> None:
    """DELETE /cluster/nodes/{id} removes the node."""
    reg = await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    node_id = reg.json()["id"]

    del_resp = await cluster_client.delete(f"/cluster/nodes/{node_id}")
    assert del_resp.status_code == 204

    list_resp = await cluster_client.get("/cluster/nodes")
    assert list_resp.json() == []


@pytest.mark.anyio
async def test_unregister_unknown_node(cluster_client: AsyncClient) -> None:
    """DELETE unknown node returns 404."""
    resp = await cluster_client.delete("/cluster/nodes/ghost")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_cluster_status(cluster_client: AsyncClient) -> None:
    """GET /cluster/status returns topology summary."""
    await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    resp = await cluster_client.get("/cluster/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["topology"] == "star"
    assert data["total_nodes"] == 1
    assert data["online_nodes"] == 1
    assert data["offline_nodes"] == 0
    assert data["available_slots"] == 4
    assert len(data["nodes"]) == 1


@pytest.mark.anyio
async def test_cluster_status_empty(cluster_client: AsyncClient) -> None:
    """GET /cluster/status with no nodes returns all-zero summary."""
    resp = await cluster_client.get("/cluster/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_nodes"] == 0
    assert data["online_nodes"] == 0


@pytest.mark.anyio
async def test_list_nodes_filter_by_status(cluster_client: AsyncClient) -> None:
    """GET /cluster/nodes?status=online filters correctly."""
    await cluster_client.post("/cluster/nodes", json=NODE_PAYLOAD)
    resp = await cluster_client.get("/cluster/nodes?status=online")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp2 = await cluster_client.get("/cluster/nodes?status=offline")
    assert resp2.status_code == 200
    assert resp2.json() == []


@pytest.mark.anyio
async def test_list_nodes_invalid_status(cluster_client: AsyncClient) -> None:
    """GET /cluster/nodes?status=bogus returns 400."""
    resp = await cluster_client.get("/cluster/nodes?status=bogus")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Bearer auth middleware tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Per-test auth-mode app.

    audit-113: auth is enabled by default; the autouse
    ``_disable_auth_for_tests`` fixture sets ``BERNSTEIN_AUTH_DISABLED=1``
    for the rest of the suite.  Tests marked ``auth_enabled`` want the
    production-like behaviour, so we clear the opt-out and build a fresh
    app (middleware reads the env at ``__init__`` time, so we can't
    reuse a module-scoped instance here).
    """
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    jsonl_path = tmp_path / "tasks.jsonl"
    app = create_app(
        jsonl_path=jsonl_path,
        auth_token="secret-token-123",
    )
    _reset_app_state(app, tmp_path)
    return app


@pytest.fixture()
async def auth_client(auth_app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_auth_missing_header_returns_401(auth_client: AsyncClient) -> None:
    """Requests without Authorization header are rejected with 401."""
    resp = await auth_client.get("/status")
    assert resp.status_code == 401


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_auth_wrong_token_returns_401(auth_client: AsyncClient) -> None:
    """Requests with a bearer that no strategy accepts are rejected 401.

    Per audit-113 the middleware returns 401 when every strategy (SSO JWT,
    agent JWT, legacy static token) rejects the header — there is no
    "authenticated-but-forbidden" state, so 403 is never produced here.
    """
    resp = await auth_client.get("/status", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_auth_correct_token_succeeds(auth_client: AsyncClient) -> None:
    """Requests with the correct token are allowed through."""
    resp = await auth_client.get("/status", headers={"Authorization": "Bearer secret-token-123"})
    assert resp.status_code == 200


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_auth_public_paths_bypass_auth(auth_client: AsyncClient) -> None:
    """/health is accessible without auth."""
    resp = await auth_client.get("/health")
    assert resp.status_code == 200

    ready = await auth_client.get("/ready")
    assert ready.status_code == 200

    alive = await auth_client.get("/alive")
    assert alive.status_code == 200


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_auth_agent_json_bypass(auth_client: AsyncClient) -> None:
    """/.well-known/agent.json bypasses auth."""
    resp = await auth_client.get("/.well-known/agent.json")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Read-only middleware tests (public demo mode)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _module_readonly_app(tmp_path_factory: pytest.TempPathFactory):
    """Single readonly-mode app shared across readonly tests."""
    jsonl_path = tmp_path_factory.mktemp("readonly") / "tasks.jsonl"
    return create_app(
        jsonl_path=jsonl_path,
        readonly=True,
    )


@pytest.fixture()
def readonly_app(_module_readonly_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Per-test: fresh store for readonly app."""
    _reset_app_state(_module_readonly_app, tmp_path)
    _module_readonly_app.state.readonly = True
    return _module_readonly_app


@pytest.fixture()
async def readonly_client(readonly_app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=readonly_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_readonly_blocks_post(readonly_client: AsyncClient) -> None:
    """POST is rejected with 405 in readonly mode."""
    resp = await readonly_client.post("/tasks", json=TASK_PAYLOAD)
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_readonly_blocks_put(readonly_client: AsyncClient) -> None:
    """PUT is rejected with 405 in readonly mode."""
    resp = await readonly_client.put("/tasks/abc123", json={})
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_readonly_blocks_delete(readonly_client: AsyncClient) -> None:
    """DELETE is rejected with 405 in readonly mode."""
    resp = await readonly_client.delete("/tasks/abc123")
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_readonly_allows_get(readonly_client: AsyncClient) -> None:
    """GET passes through in readonly mode."""
    resp = await readonly_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_readonly_allows_status(readonly_client: AsyncClient) -> None:
    """GET /status passes through in readonly mode."""
    resp = await readonly_client.get("/status")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_readonly_ready_reports_not_ready(readonly_client: AsyncClient) -> None:
    """/ready should fail readiness when readonly mode is active."""
    resp = await readonly_client.get("/ready")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["status"] == "not_ready"
    assert payload["reason"] == "readonly"
