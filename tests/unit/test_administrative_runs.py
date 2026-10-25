"""Administrative containers are durable, audited and independent of orchestration."""

from types import SimpleNamespace

import pytest

from bernstein.core.administrative_runs import AdministrativeRuns
from bernstein.core.security.audit_chain import AuditChainStore


def task(task_id, state="open", tenant="default"):
    return SimpleNamespace(id=task_id, title=task_id, status=SimpleNamespace(value=state), tenant_id=tenant)


def registry(tmp_path):
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    tasks = {}
    return AdministrativeRuns(chain, tasks.get, tasks.values), tasks, chain


def test_empty_run_and_restart(tmp_path):
    runs, tasks, chain = registry(tmp_path)
    created = runs.create("Weekly sprint", "default")
    assert created["status"] == "working"
    assert created["tasks"] == []
    assert not tasks
    assert AdministrativeRuns(chain, tasks.get, tasks.values).status(created["runId"], "default") == created


def test_membership_states_closure_and_integrity(tmp_path):
    runs, tasks, chain = registry(tmp_path)
    run_id = runs.create("Sprint", "default")["runId"]
    for tid in ("a", "b"):
        tasks[tid] = task(tid)
        runs.add(run_id, tasks[tid], "default")
    tasks["a"].status.value = "done"
    runs.task_updated(tasks["a"])
    assert runs.status(run_id, "default")["status"] == "working"
    tasks["b"].status.value = "failed"
    runs.task_updated(tasks["b"])
    assert runs.status(run_id, "default")["status"] == "working"
    tasks["b"].status.value = "cancelled"
    runs.task_updated(tasks["b"])
    closed = runs.status(run_id, "default")
    assert closed["status"] == "completed"
    assert closed["chainVerified"] is True
    assert closed["receiptHash"]
    assert [t["status"] for t in closed["tasks"]] == ["done", "cancelled"]
    runs.task_updated(tasks["b"])
    assert len(chain.query(event_type="administrative.run_closed")) == 1
    with pytest.raises(ValueError, match="closed"):
        runs.add(run_id, task("c"), "default")
    assert AdministrativeRuns(chain, tasks.get, tasks.values).status(run_id, "default") == closed


def test_scope_missing_and_unrecorded_update(tmp_path):
    runs, tasks, _ = registry(tmp_path)
    run_id = runs.create("Sprint", "tenant-a")["runId"]
    with pytest.raises(KeyError):
        runs.status(run_id, "tenant-b")
    with pytest.raises(KeyError):
        runs.add("missing", task("a"), "default")
    with pytest.raises(ValueError, match="tenant"):
        runs.add(run_id, task("a"), "tenant-a")
    tasks["a"] = task("a", tenant="tenant-a")
    runs.add(run_id, tasks["a"], "tenant-a")
    tasks["a"].status.value = "done"
    with pytest.raises(ValueError, match="snapshot"):
        runs.status(run_id, "tenant-a")


def test_chain_tampering_refuses_status(tmp_path):
    runs, _, _ = registry(tmp_path)
    run_id = runs.create("Sprint", "default")["runId"]
    path = next((tmp_path / "audit").glob("*.jsonl"))
    path.write_text(path.read_text().replace("Sprint", "Forged"))
    with pytest.raises(ValueError, match="audit"):
        runs.status(run_id, "default")


def test_concurrent_membership_and_closure(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    runs, tasks, chain = registry(tmp_path)
    run_id = runs.create("Sprint", "default")["runId"]
    for index in range(8):
        tasks[str(index)] = task(str(index))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda t: runs.add(run_id, t, "default"), tasks.values()))

    def finish(t):
        t.status.value = "done"
        runs.task_updated(t)

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(finish, tasks.values()))
    assert runs.status(run_id, "default")["status"] == "completed"
    assert len(chain.query(event_type="administrative.run_closed")) == 1


@pytest.mark.asyncio
async def test_store_real_lifecycle_survives_restart(tmp_path):
    from bernstein.core.server import TaskCreate, TaskStore

    path = tmp_path / ".sdd" / "tasks.jsonl"
    store = TaskStore(path)
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    runs = AdministrativeRuns(chain, store.get_task, store.list_tasks)
    store.administrative_runs = runs
    run_id = runs.create("Sprint", "default")["runId"]
    created = await store.create(
        TaskCreate(title="manual", description="manual", role="backend", metadata={"administrative_run_id": run_id})
    )
    assert runs.status(run_id, "default")["tasks"][0]["status"] == "open"
    claimed = await store.claim_next("backend")
    assert claimed.id == created.id
    await store.complete(created.id, "Reviewed brief")
    await store.close(created.id)
    assert runs.status(run_id, "default")["status"] == "completed"
    restored = TaskStore(path)
    restored.replay_jsonl()
    assert (
        AdministrativeRuns(chain, restored.get_task, restored.list_tasks).status(run_id, "default")["status"]
        == "completed"
    )


@pytest.mark.auth_enabled
@pytest.mark.asyncio
async def test_http_membership_scope_and_no_orchestration(tmp_path):
    import httpx

    from bernstein.core.server import create_app

    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="test-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/administrative-runs", json={"goal": "Sprint"})).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"
        response = await client.post("/administrative-runs", json={"goal": "Sprint"})
        assert response.status_code == 201, response.text
        run_id = response.json()["runId"]
        assert app.state.store.list_tasks() == []
        response = await client.post(
            "/tasks", json={"title": "manual", "description": "Manual brief", "role": "backend", "run_id": run_id}
        )
        assert response.status_code == 201, response.text
        tid = response.json()["id"]
        status = await client.get(f"/administrative-runs/{run_id}")
        assert status.json()["tasks"][0]["id"] == tid
        assert status.json()["chainVerified"]
        assert (await client.get(f"/administrative-runs/{run_id}", headers={"X-Tenant-ID": "other"})).status_code in {
            403,
            404,
        }
        await app.state.store.cancel(tid, "Cancelled")
        response = await client.post("/tasks", json={"title": "late", "description": "late", "run_id": run_id})
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_task_scoped_credentials_and_batch_cannot_add_members(tmp_path):
    from fastapi import HTTPException, Request

    from bernstein.core.routes.task_crud import (
        AdministrativeRunCreate,
        administrative_run_status,
        create_administrative_run,
        create_task,
        create_tasks_batch,
    )
    from bernstein.core.server import BatchCreateRequest, TaskCreate

    runs, tasks, _ = registry(tmp_path)
    run_id = runs.create("Sprint", "default")["runId"]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(administrative_runs=runs, store=None, sse_bus=None, draining=False)
            ),
        }
    )
    request.state.agent_identity = SimpleNamespace(id="worker", role="backend", task_ids=["allowed"])
    with pytest.raises(HTTPException) as denied:
        create_administrative_run(AdministrativeRunCreate(goal="Sprint"), request)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        await create_task(TaskCreate(title="New", description="Manual", run_id=run_id), request)
    assert denied.value.status_code == 403
    tasks["other"] = task("other")
    runs.add(run_id, tasks["other"], "default")
    with pytest.raises(HTTPException) as denied:
        administrative_run_status(run_id, request)
    assert denied.value.status_code == 403
    request.state.agent_identity = None
    with pytest.raises(HTTPException) as denied:
        await create_tasks_batch(
            BatchCreateRequest(
                tasks=[TaskCreate(title="New", description="Manual", metadata={"administrative_run_id": run_id})]
            ),
            request,
        )
    assert denied.value.status_code == 422


@pytest.mark.asyncio
async def test_interrupted_membership_append_is_never_certified(tmp_path):
    from unittest.mock import patch

    from bernstein.core.server import TaskCreate, TaskStore

    store = TaskStore(tmp_path / ".sdd" / "tasks.jsonl")
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    runs = AdministrativeRuns(chain, store.get_task, store.list_tasks)
    store.administrative_runs = runs
    run_id = runs.create("Sprint", "default")["runId"]
    with patch.object(runs, "_record", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            await store.create(
                TaskCreate(title="manual", description="manual", metadata={"administrative_run_id": run_id})
            )
    with pytest.raises(ValueError, match="membership snapshot"):
        runs.status(run_id, "default")


@pytest.mark.asyncio
async def test_claimed_task_recovery_records_new_open_state(tmp_path):
    from bernstein.core.server import TaskCreate, TaskStore

    path = tmp_path / ".sdd" / "tasks.jsonl"
    store = TaskStore(path)
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    runs = AdministrativeRuns(chain, store.get_task, store.list_tasks)
    store.administrative_runs = runs
    run_id = runs.create("Sprint", "default")["runId"]
    await store.create(
        TaskCreate(title="manual", description="manual", role="backend", metadata={"administrative_run_id": run_id})
    )
    await store.claim_next("backend")
    restored = TaskStore(path)
    restored.administrative_runs = AdministrativeRuns(chain, restored.get_task, restored.list_tasks)
    restored.replay_jsonl()
    restored.recover_stale_claimed_tasks()
    assert restored.administrative_runs.status(run_id, "default")["tasks"][0]["status"] == "open"


def test_archived_registry_events_remain_queryable(tmp_path):
    import gzip

    runs, _, _ = registry(tmp_path)
    initial = runs.create("Sprint", "default")
    run_id = initial["runId"]
    path = next((tmp_path / "audit").glob("*.jsonl"))
    archive_dir = path.parent / "archive"
    archive_dir.mkdir()
    with gzip.open(archive_dir / f"{path.name}.gz", "wb") as archived:
        archived.write(path.read_bytes())
    path.unlink()
    assert runs.status(run_id, "default")["status"] == "working"
