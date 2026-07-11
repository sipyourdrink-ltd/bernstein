"""Task-server mailbox endpoint tests (#2357).

AC1: a worker receives another worker's finding on its next poll of the
task server, without any scheduler re-dispatch in between.

Also covers: deterministic delivery order (chain append order), 404/422
edges, the audit-chain mirror of every accepted message, claim receipts
carrying the dependency snapshot, and the ``needs`` alias on task
creation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.communication.task_mailbox import TaskMailbox
from bernstein.core.security.audit_chain import (
    EVENT_TASK_CLAIM_RECEIPT,
    EVENT_TASK_MAILBOX_MESSAGE,
    AuditChainStore,
)
from bernstein.core.server import SSEBus, TaskStore, create_app

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"mailbox-routes-test-key"


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):
    """Single FastAPI app shared across the module (create_app is expensive)."""
    root = tmp_path_factory.mktemp("mailbox-server")
    return create_app(jsonl_path=root / "runtime" / "tasks.jsonl")


@pytest.fixture()
def app(_module_app, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Reuse the shared app with fresh per-test state under ``tmp_path``."""
    _module_app.state.store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    _module_app.state.sse_bus = SSEBus()
    _module_app.state.draining = False
    _module_app.state.sdd_dir = tmp_path
    _module_app.state.runtime_dir = tmp_path / "runtime"
    _module_app.state.audit_chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    _module_app.state.task_mailbox = TaskMailbox(
        tmp_path / "runtime" / "mailbox.jsonl",
        hmac_key=_KEY,
        identity_dir=tmp_path / "identity",
    )
    return _module_app


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(client: AsyncClient, title: str, **extra: object) -> str:
    resp = await client.post(
        "/tasks",
        json={"title": title, "description": f"{title} description", "role": "backend", **extra},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# ---------------------------------------------------------------------------
# create_app wiring
# ---------------------------------------------------------------------------


def test_create_app_wires_mailbox_and_audit_chain(tmp_path: Path) -> None:
    application = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    assert isinstance(application.state.task_mailbox, TaskMailbox)
    assert isinstance(application.state.audit_chain, AuditChainStore)
    # The chain key stays inside the server's state dir - cwd-independent.
    assert (tmp_path / "audit").is_dir()


# ---------------------------------------------------------------------------
# AC1 - finding reaches the other worker on its next poll
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_worker_b_receives_finding_on_next_poll_without_redispatch(client: AsyncClient) -> None:
    await _create_task(client, "Task A")
    task_b = await _create_task(client, "Task B")

    # Both workers are already dispatched (claimed) - no re-dispatch happens below.
    resp = await client.get("/tasks/next/backend", params={"claimed_by_session": "sess-a"})
    assert resp.status_code == 200
    resp = await client.get("/tasks/next/backend", params={"claimed_by_session": "sess-b"})
    assert resp.status_code == 200
    version_before = (await client.get(f"/tasks/{task_b}")).json()["version"]

    # Worker A hands its finding to worker B's task mid-run.
    resp = await client.post(
        f"/tasks/{task_b}/messages",
        json={
            "sender": "worker-a",
            "kind": "finding",
            "body": "Error mapping duplicated; use shared helper in core/errors.",
        },
    )
    assert resp.status_code == 201, resp.text
    posted = resp.json()
    assert posted["seq"] == 0
    assert posted["kind"] == "finding"
    assert posted["entry_hash"].startswith("hmac-sha256:")

    # Worker B's next poll delivers the finding - no scheduler involvement.
    resp = await client.get(f"/tasks/{task_b}/messages")
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 1
    assert messages[0]["sender"] == "worker-a"
    assert messages[0]["body"].startswith("Error mapping duplicated")

    # The delivery did not re-dispatch or mutate the task itself.
    after = (await client.get(f"/tasks/{task_b}")).json()
    assert after["version"] == version_before
    assert after["status"] == "claimed"


@pytest.mark.anyio
async def test_delivery_order_is_chain_append_order_and_stable(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Ordered")
    for i in range(3):
        resp = await client.post(
            f"/tasks/{task_id}/messages",
            json={"sender": f"w{i}", "kind": "finding", "body": f"finding {i}"},
        )
        assert resp.status_code == 201

    first = (await client.get(f"/tasks/{task_id}/messages")).json()
    second = (await client.get(f"/tasks/{task_id}/messages")).json()
    assert [m["seq"] for m in first] == [0, 1, 2]
    assert first == second


@pytest.mark.anyio
async def test_poll_since_seq_returns_only_newer_messages(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Cursor")
    for i in range(3):
        await client.post(
            f"/tasks/{task_id}/messages",
            json={"sender": "w", "kind": "finding", "body": f"m{i}"},
        )
    resp = await client.get(f"/tasks/{task_id}/messages", params={"since_seq": 0})
    assert resp.status_code == 200
    assert [m["body"] for m in resp.json()] == ["m1", "m2"]


# ---------------------------------------------------------------------------
# Validation and access edges
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_message_unknown_task_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/tasks/nope/messages",
        json={"sender": "w", "kind": "finding", "body": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_get_messages_unknown_task_404(client: AsyncClient) -> None:
    resp = await client.get("/tasks/nope/messages")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_post_message_unknown_kind_422(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Kinds")
    resp = await client.post(
        f"/tasks/{task_id}/messages",
        json={"sender": "w", "kind": "chatter", "body": "hi"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_post_message_oversize_body_422(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Caps")
    resp = await client.post(
        f"/tasks/{task_id}/messages",
        json={"sender": "w", "kind": "finding", "body": "x" * 5000},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# AC2 (API half) - every accepted message is mirrored into the audit chain
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_posted_message_is_mirrored_into_audit_chain(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    task_id = await _create_task(client, "Mirrored")
    resp = await client.post(
        f"/tasks/{task_id}/messages",
        json={"sender": "w", "kind": "finding", "body": "mirror me"},
    )
    assert resp.status_code == 201
    entry_hash = resp.json()["entry_hash"]

    chain: AuditChainStore = app.state.audit_chain
    events = chain.query(event_type=EVENT_TASK_MAILBOX_MESSAGE)
    assert [e.details["entry_hash"] for e in events] == [entry_hash]
    assert events[0].details["task_id"] == task_id
    assert "mirror me" not in str(events[0].details)
    ok, problems = chain.verify()
    assert ok, problems


# ---------------------------------------------------------------------------
# Claims are journal entries - the dependency snapshot lands on the chain
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_records_dependency_snapshot_receipt(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    dep_id = await _create_task(client, "Dependency")
    resp = await client.get("/tasks/next/backend", params={"claimed_by_session": "sess-dep"})
    assert resp.status_code == 200
    assert resp.json()["id"] == dep_id
    await client.post(f"/tasks/{dep_id}/complete", json={"result_summary": "done"})

    child_id = await _create_task(client, "Dependent", depends_on=[dep_id])
    resp = await client.get("/tasks/next/backend", params={"claimed_by_session": "sess-child"})
    assert resp.status_code == 200
    assert resp.json()["id"] == child_id

    chain: AuditChainStore = app.state.audit_chain
    receipts = chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT)
    by_task = {e.details["task_id"]: e for e in receipts}
    assert set(by_task) == {dep_id, child_id}
    assert by_task[child_id].details["depends_on"] == [dep_id]
    assert by_task[child_id].details["claim_path"] == "next"
    ok, problems = chain.verify()
    assert ok, problems


@pytest.mark.anyio
async def test_claim_by_id_records_receipt(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    task_id = await _create_task(client, "Direct claim")
    resp = await client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 200

    chain: AuditChainStore = app.state.audit_chain
    receipts = chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT)
    assert len(receipts) == 1
    assert receipts[0].details["task_id"] == task_id
    assert receipts[0].details["claim_path"] == "by_id"


# ---------------------------------------------------------------------------
# Dependency schema - `needs` is accepted as an alias for depends_on
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_task_create_accepts_needs_alias(client: AsyncClient) -> None:
    dep_id = await _create_task(client, "Dep for alias")
    resp = await client.post(
        "/tasks",
        json={
            "title": "Uses needs",
            "description": "declares deps via needs",
            "role": "backend",
            "needs": [dep_id],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["depends_on"] == [dep_id]


@pytest.mark.anyio
async def test_task_create_depends_on_still_accepted(client: AsyncClient) -> None:
    dep_id = await _create_task(client, "Dep classic")
    resp = await client.post(
        "/tasks",
        json={
            "title": "Uses depends_on",
            "description": "declares deps via depends_on",
            "role": "backend",
            "depends_on": [dep_id],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["depends_on"] == [dep_id]


@pytest.mark.anyio
async def test_claim_api_refuses_task_with_incomplete_dependencies(client: AsyncClient) -> None:
    dep_id = await _create_task(client, "Incomplete dep")
    child_id = await _create_task(client, "Gated child", depends_on=[dep_id])

    # Direct claim of the gated child conflicts while the dependency is open.
    resp = await client.post(f"/tasks/{child_id}/claim")
    assert resp.status_code == 409

    # next-task claiming offers the dependency, never the gated child.
    resp = await client.get("/tasks/next/backend")
    assert resp.status_code == 200
    assert resp.json()["id"] == dep_id
