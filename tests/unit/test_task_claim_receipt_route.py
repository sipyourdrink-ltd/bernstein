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
