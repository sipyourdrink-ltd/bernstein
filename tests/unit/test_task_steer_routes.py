"""Task-server steering endpoint tests (#2508).

POST /tasks/{task_id}/steer records a signed steering receipt on the audit
chain before delivering the effect through the mailbox, gated by the scoped
dashboard-token registry. These tests cover the happy path, the audit-chain
mirror, scope enforcement, and the malformed / mismatch edges.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.communication.task_mailbox import TaskMailbox
from bernstein.core.security.audit_chain import EVENT_STEERING_RECEIPT, AuditChainStore
from bernstein.core.server import SSEBus, TaskStore, create_app
from bernstein.core.server.dashboard_tokens import SCOPE_OPERATOR, SCOPE_VIEWER, DashboardTokenRegistry

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"steer-routes-test-key"


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def _module_app(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    root = tmp_path_factory.mktemp("steer-server")
    return create_app(jsonl_path=root / "runtime" / "tasks.jsonl")


@pytest.fixture()
def app(_module_app, tmp_path: Path):  # type: ignore[no-untyped-def]
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
    # No scoped-token auth by default (loopback posture -> operator).
    _module_app.state.dashboard_auth_state = SimpleNamespace(token_registry=None)
    return _module_app


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(client: AsyncClient, title: str) -> str:
    resp = await client.post(
        "/tasks",
        json={"title": title, "description": f"{title} description", "role": "backend"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _configure_tokens(app, tmp_path: Path) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    registry = DashboardTokenRegistry(tmp_path / "auth" / "dashboard_tokens.jsonl", hmac_key=_KEY)
    op_token, _ = registry.issue(principal="alice", scope=SCOPE_OPERATOR, now=1)
    viewer_token, _ = registry.issue(principal="viewer-vic", scope=SCOPE_VIEWER, now=2)
    app.state.dashboard_auth_state = SimpleNamespace(token_registry=registry)
    return op_token, viewer_token


# ---------------------------------------------------------------------------
# Happy path: guidance is a receipt-first delivery
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_guidance_steer_records_receipt_and_delivers(client: AsyncClient, app) -> None:  # type: ignore[no-untyped-def]
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "guidance", "principal": "alice", "guidance": "stop refactoring, fix the failing test"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "guidance"
    assert body["receipt_hash"]
    assert body["payload_hash"].startswith("sha256:")
    assert body["mailbox_entry_hash"].startswith("hmac-sha256:")

    chain: AuditChainStore = app.state.audit_chain
    receipts = chain.query(event_type=EVENT_STEERING_RECEIPT)
    assert len(receipts) == 1
    assert receipts[0].hmac == body["receipt_hash"]
    ok, problems = chain.verify()
    assert ok, problems


@pytest.mark.anyio
async def test_abort_writes_scheduler_signal(client: AsyncClient, tmp_path: Path) -> None:
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "abort", "principal": "alice", "session_id": "sess-1", "reason": "wrong path"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["abort_signal_written"] is True
    assert (tmp_path / "runtime" / "signals" / "sess-1" / "SHUTDOWN").is_file()


# ---------------------------------------------------------------------------
# Scope enforcement via the scoped-token registry
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_operator_token_is_authorised(client: AsyncClient, app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    op_token, _ = _configure_tokens(app, tmp_path)
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "guidance", "guidance": "focus on tests"},
        headers={"Authorization": f"Bearer {op_token}"},
    )
    assert resp.status_code == 201, resp.text
    # The receipt attributes to the token principal, not a client-claimed one.
    assert resp.json()["principal"] == "alice"


@pytest.mark.anyio
async def test_viewer_token_is_rejected(client: AsyncClient, app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, viewer_token = _configure_tokens(app, tmp_path)
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "abort", "session_id": "sess-1"},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert resp.status_code == 403, resp.text
    # No receipt was written and no signal was created.
    chain: AuditChainStore = app.state.audit_chain
    assert chain.query(event_type=EVENT_STEERING_RECEIPT) == []
    assert not (tmp_path / "runtime" / "signals" / "sess-1").exists()


@pytest.mark.anyio
async def test_missing_token_when_auth_configured_is_rejected(client: AsyncClient, app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _configure_tokens(app, tmp_path)
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "guidance", "guidance": "focus"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unknown_task_is_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/tasks/does-not-exist/steer",
        json={"kind": "guidance", "principal": "alice", "guidance": "x"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.anyio
async def test_guidance_without_text_is_422(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={"kind": "guidance", "principal": "alice"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.anyio
async def test_displayed_payload_mismatch_is_409(client: AsyncClient) -> None:
    task_id = await _create_task(client, "Task A")
    resp = await client.post(
        f"/tasks/{task_id}/steer",
        json={
            "kind": "guidance",
            "principal": "alice",
            "guidance": "focus",
            "displayed_payload_hash": "sha256:does-not-match",
        },
    )
    assert resp.status_code == 409, resp.text
