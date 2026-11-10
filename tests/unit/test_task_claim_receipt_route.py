"""Claim-receipt route tests (#2555).

``POST /tasks/claim-receipt`` drives the dependency-gated claim path and
returns a signed, content-addressed claim receipt instead of a mutable task
projection. These tests exercise the route end to end and verify the returned
receipt offline against the on-disk backlog and audit chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.protocols.mcp.claim_receipt import ClaimReceipt, verify_claim_receipt
from bernstein.core.security.audit_chain import EVENT_TASK_CLAIM_RECEIPT, AuditChainStore
from bernstein.core.server import SSEBus, TaskStore, create_app
from bernstein.core.tasks.claim import Backlog, BacklogEntry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio

_KEY = b"claim-receipt-route-test-key"


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("claim-receipt-server")
    return create_app(jsonl_path=root / "runtime" / "tasks.jsonl")


@pytest.fixture()
def app(_module_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    _module_app.state.store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    _module_app.state.sse_bus = SSEBus()
    _module_app.state.draining = False
    _module_app.state.sdd_dir = tmp_path
    _module_app.state.runtime_dir = tmp_path / "runtime"
    _module_app.state.audit_chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    _module_app.state.claim_backlog_path = tmp_path / "runtime" / "task-backlog.json"
    _module_app.state.claim_identity_dir = tmp_path / "identity"
    return _module_app


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _seed(app, entries: list[BacklogEntry]) -> None:  # type: ignore[no-untyped-def]
    Backlog.write(app.state.claim_backlog_path, entries)


def _on_disk_rows(app) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    return [e.to_dict() for e in Backlog.load(app.state.claim_backlog_path).entries]


async def test_granted_claim_returns_verifiable_receipt(app, client: AsyncClient) -> None:
    _seed(app, [BacklogEntry(id="t1", role="backend")])
    resp = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
    assert resp.status_code == 200, resp.text
    wire = resp.json()
    assert wire["granted"] is True
    assert wire["taskId"] == "t1"
    assert wire["signature"]
    assert wire["receiptHash"]

    receipt = ClaimReceipt.from_wire(wire)
    ok, reason = verify_claim_receipt(receipt, _on_disk_rows(app), app.state.audit_chain)
    assert ok, reason


async def test_gated_task_returns_refusal_receipt(app, client: AsyncClient) -> None:
    # t2 depends on t1, which is not complete: the claim must be refused, and
    # the refusal is itself a signed, verifiable receipt (no silent skip).
    _seed(app, [BacklogEntry(id="t2", role="backend", depends_on=["t1"])])
    resp = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
    assert resp.status_code == 200, resp.text
    wire = resp.json()
    assert wire["granted"] is False
    assert wire["taskId"] == ""

    receipt = ClaimReceipt.from_wire(wire)
    ok, reason = verify_claim_receipt(receipt, _on_disk_rows(app), app.state.audit_chain)
    assert ok, reason


async def test_gated_task_granted_when_dependency_completed(app, client: AsyncClient) -> None:
    _seed(app, [BacklogEntry(id="t2", role="backend", depends_on=["t1"])])
    resp = await client.post(
        "/tasks/claim-receipt",
        json={"claimer_id": "worker-1", "completed_ids": ["t1"]},
    )
    assert resp.status_code == 200, resp.text
    wire = resp.json()
    assert wire["granted"] is True
    assert wire["taskId"] == "t2"


async def test_granted_claim_reuses_existing_audit_event(app, client: AsyncClient) -> None:
    # The claim must land as the existing ``task.claim_receipt`` event - no
    # new audit event type is introduced.
    _seed(app, [BacklogEntry(id="t1", role="backend")])
    before = len(app.state.audit_chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT))
    await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
    events = app.state.audit_chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT)
    assert len(events) == before + 1
    assert events[-1].details["claim_path"] == "mcp_claim"
    assert events[-1].details["task_id"] == "t1"


async def test_second_store_verifies_receipt_offline(app, client: AsyncClient, tmp_path: Path) -> None:
    # A fresh chain store over the same on-disk state (a second server
    # process) verifies the receipt with no in-memory session (#2506).
    _seed(app, [BacklogEntry(id="t1", role="backend")])
    resp = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
    receipt = ClaimReceipt.from_wire(resp.json())
    reloaded = AuditChainStore(tmp_path / "audit", key=_KEY)
    ok, reason = verify_claim_receipt(receipt, _on_disk_rows(app), reloaded)
    assert ok, reason


async def test_granted_claim_publishes_task_claimed_event(app, client: AsyncClient) -> None:
    from unittest.mock import MagicMock

    _seed(app, [BacklogEntry(id="t1", role="backend")])
    app.state.sse_bus = MagicMock()
    await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
    published = [call.args[0] for call in app.state.sse_bus.publish.call_args_list]
    assert "task_claimed" in published


async def test_draining_server_refuses_new_claims(app, client: AsyncClient) -> None:
    _seed(app, [BacklogEntry(id="t1", role="backend")])
    app.state.draining = True
    try:
        resp = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker-1"})
        assert resp.status_code == 503
    finally:
        app.state.draining = False


async def test_http_created_task_is_claimed_and_completed(app, client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Administrative brief",
            "description": "Prepare the internal brief",
            "role": "backend",
            "priority": 1,
            "scope": "small",
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    claimed = await client.post(
        "/tasks/claim-receipt",
        json={
            "claimer_id": "worker-1",
            "role": "backend",
        },
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    assert app.state.store.get_task(task_id).status.value == "claimed"
    completed = await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "Brief delivered"})
    assert completed.status_code == 200, completed.text


async def test_store_claim_filters_dependencies_and_verifies_snapshot(app, client: AsyncClient) -> None:
    first = (
        await client.post(
            "/tasks",
            json={
                "title": "Dependency",
                "description": "Write brief",
                "role": "docs",
            },
        )
    ).json()["id"]
    second = (
        await client.post(
            "/tasks",
            json={
                "title": "Dependent",
                "description": "Review brief",
                "role": "backend",
                "depends_on": [first],
                "priority": 1,
                "scope": "large",
                "model": "sonnet",
                "effort": "high",
            },
        )
    ).json()["id"]
    for payload in [
        {"claimer_id": "worker", "role": "security"},
        {"claimer_id": "worker", "role": "backend", "completed_ids": [first]},
    ]:
        assert (await client.post("/tasks/claim-receipt", json=payload)).json()["granted"] is False
    await client.post(f"/tasks/{first}/complete", json={"result_summary": "Dependency delivered"})
    response = await client.post(
        "/tasks/claim-receipt",
        json={
            "claimer_id": "worker",
            "role": "backend",
            "completed_ids": [first],
        },
    )
    wire = response.json()
    assert wire["taskId"] == second
    assert wire["backlogSource"] == "task_store"
    ok, reason = verify_claim_receipt(ClaimReceipt.from_wire(wire), wire["backlogSnapshot"], app.state.audit_chain)
    assert ok, reason
    task = app.state.store.get_task(second)
    assert (task.priority, task.scope.value, task.model, task.effort) == (1, "large", "sonnet", "high")
    assert not app.state.claim_backlog_path.exists()


async def test_creation_uses_install_hmac_chain(app, client: AsyncClient, tmp_path: Path) -> None:
    response = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Audited brief",
            "description": "Internal report",
            "role": "docs",
        },
    )
    task_id = response.json()["id"]
    events = app.state.audit_chain.query(event_type="task.created")
    event = next(e for e in events if e.resource_id == task_id)
    assert event.details["title"] == "Audited brief"
    assert event.details["role"] == "docs"
    assert app.state.audit_chain.verify() == (True, [])
    files = list((tmp_path / "audit").rglob("*.jsonl"))
    assert files
    changed = False
    for path in files:
        text = path.read_text()
        if "Audited brief" in text:
            path.write_text(text.replace("Audited brief", "Altered brief"))
            changed = True
    assert changed
    assert app.state.audit_chain.verify()[0] is False


async def test_invalid_create_and_claim_leave_store_unchanged(app, client: AsyncClient) -> None:
    for body in [
        {"title": "Invalid", "description": "Invalid scope", "scope": "huge"},
        {"title": "Invalid", "description": "Unknown dependency", "depends_on": ["missing"]},
    ]:
        assert (await client.post("/api/v1/tasks", json=body)).status_code == 422
    assert app.state.store.list_tasks() == []
    assert app.state.audit_chain.query(event_type="task.created") == []
    assert (await client.post("/tasks/claim-receipt", json={"claimer_id": "../escape"})).status_code == 422
    assert not app.state.claim_backlog_path.exists()


async def test_real_mcp_create_claim_complete(app, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from bernstein.mcp.server import create_mcp_server

    real_client = AsyncClient
    monkeypatch.setattr(
        "bernstein.mcp.server.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=ASGITransport(app=app), base_url="http://test"),
    )
    from unittest.mock import Mock

    planner = Mock(side_effect=AssertionError("Bare tasks must not invoke the planner"))
    spawn = Mock(side_effect=AssertionError("Bare tasks must not spawn agents"))
    monkeypatch.setattr("bernstein.core.planning.planner.plan", planner)
    monkeypatch.setattr("bernstein.core.agents.spawner_core.AgentSpawner.spawn_for_tasks", spawn)
    mcp = create_mcp_server(server_url="http://test")

    async def invoke(name, args):
        result = await mcp.call_tool(name, args)
        payload = json.loads(result[0][0].text)
        return payload.get("result", payload)

    task = await invoke(
        "bernstein_create_task",
        {
            "title": "MCP brief",
            "description": "Prepare internal report",
            "role": "docs",
            "scope": "small",
        },
    )
    claimed = await invoke("bernstein_claim", {"claimer_id": "writer", "role": "docs"})
    assert claimed["taskId"] == task["id"]
    assert app.state.store.get_task(task["id"]).status.value == "claimed"
    completed = await invoke("bernstein_complete", {"task_id": task["id"], "result_summary": "Report delivered"})
    assert completed["task_id"] == task["id"]
    assert completed["status"] in ("done", "pending_approval")
    planner.assert_not_called()
    spawn.assert_not_called()
    assert not app.state.claim_backlog_path.exists()


async def test_priority_selection_and_no_double_claim(app, client: AsyncClient) -> None:
    ids = {}
    for priority in [3, 1, 2]:
        response = await client.post(
            "/tasks",
            json={
                "title": f"Priority {priority}",
                "description": "Prepare brief",
                "role": "docs",
                "priority": priority,
            },
        )
        ids[priority] = response.json()["id"]
    for priority in [1, 2, 3]:
        wire = (await client.post("/tasks/claim-receipt", json={"claimer_id": "worker", "role": "docs"})).json()
        assert wire["taskId"] == ids[priority]
    assert (await client.post("/tasks/claim-receipt", json={"claimer_id": "worker", "role": "docs"})).json()[
        "granted"
    ] is False


async def test_mcp_invalid_create_does_not_send_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from bernstein.mcp.server import create_mcp_server

    network = Mock(side_effect=AssertionError("Invalid input must not reach HTTP"))
    monkeypatch.setattr("bernstein.mcp.server.httpx.AsyncClient", network)
    mcp = create_mcp_server(server_url="http://test")
    result = await mcp.call_tool("bernstein_create_task", {"title": "", "description": "brief"})
    assert "error" in result[0][0].text.lower()
    network.assert_not_called()


async def test_claim_respects_planned_status_and_preserves_approval_metadata(app, client: AsyncClient) -> None:
    from bernstein.core.models import TaskStatus

    response = await client.post(
        "/tasks",
        json={
            "title": "Approval brief",
            "description": "Internal report",
            "role": "docs",
            "approval_required": True,
        },
    )
    task_id = response.json()["id"]
    task = app.state.store.get_task(task_id)
    assert task.approval_required is True
    # Plan approval owns this transition; acquisition must never clear it.
    task.status = TaskStatus.PLANNED
    claimed = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker", "role": "docs"})
    assert claimed.json()["granted"] is False
    assert task.status == TaskStatus.PLANNED
    assert task.approval_required is True


async def test_default_application_persists_creation_on_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.security.audit import load_or_create_audit_key

    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    application = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as c:
        response = await c.post(
            "/api/v1/tasks",
            json={
                "title": "Default audit",
                "description": "Persist without an orchestrator",
                "role": "docs",
            },
        )
    assert response.status_code == 201, response.text
    reloaded = AuditChainStore(tmp_path / ".sdd" / "audit", key=load_or_create_audit_key())
    assert reloaded.verify() == (True, [])
    events = reloaded.query(event_type="task.created", resource_id=response.json()["id"])
    assert len(events) == 1
    assert events[0].details["description"] == "Persist without an orchestrator"
    assert events[0].details["role"] == "docs"
