"""Task scope on the routes where the server picks the task to claim.

``GET /tasks/next/{role}`` and ``POST /tasks/claim-receipt`` name no task in
the path or the body: the server chooses the row and flips it to claimed.
The path-level gate in the middleware therefore has nothing to check and
exempts both as collection routes, and ``GET`` is skipped by the mutating-
method filter on top of that.  Both are still claims, so a task-scoped agent
token must only ever be handed a task inside its own ``task_ids`` - otherwise
a token scoped to task A walks away holding task B.

The rule is enforced where the candidate is chosen (the store query and the
backlog claim filter), so an out-of-scope row is never picked, not picked
and then refused.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.models import TaskStatus
from fastapi.testclient import TestClient

from bernstein.core.protocols.mcp.claim_receipt import filter_digest
from bernstein.core.tasks.claim import Backlog, BacklogEntry, ClaimFilter

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-claim-next-scope-tests"

# ``filter_digest(ClaimFilter(role="backend"))`` as computed before the filter
# carried a task scope.
_LEGACY_BACKEND_FILTER_DIGEST = "sha256:883d533077d42ee9842957d870fa120b98d4c6d015c4468d8e0b70d9c0463b9f"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application with auth on and an operator bearer for setup."""
    from bernstein.core.server import create_app

    application = create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )
    application.state.claim_backlog_path = tmp_path / "runtime" / "task-backlog.json"
    application.state.claim_identity_dir = tmp_path / "identity"
    return application


_client_counter = iter(range(1, 1_000_000))


def _client(application: FastAPI) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    index = next(_client_counter)
    return TestClient(application, client=(f"10.41.{index // 256}.{index % 256}", 43000 + index % 20000))


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _agent_headers(application: FastAPI, session: str, role: str, task_ids: list[str]) -> dict[str, str]:
    identity_store: Any = application.state.identity_store
    _, token = identity_store.create_identity(session, role, task_ids=task_ids)
    return {"Authorization": f"Bearer {token}"}


def _create_task(application: FastAPI, title: str, priority: int, role: str = "backend") -> str:
    response = _client(application).post(
        "/tasks",
        headers=_operator_headers(),
        json={"title": title, "description": title, "role": role, "priority": priority},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _status(application: FastAPI, task_id: str) -> TaskStatus:
    task = application.state.store.get_task(task_id)
    assert task is not None, task_id
    return task.status


# ---------------------------------------------------------------------------
# GET /tasks/next/{role}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
def test_scoped_token_claim_next_never_takes_an_out_of_scope_task(app: FastAPI, prefix: str) -> None:
    # B outranks A, so an unfiltered queue hands B out first.
    peer = _create_task(app, "peer task", priority=1)
    own = _create_task(app, "own task", priority=3)
    headers = _agent_headers(app, "backend-scoped", "backend", [own])

    response = _client(app).get(f"{prefix}/tasks/next/backend", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == own
    assert _status(app, own) == TaskStatus.CLAIMED
    assert _status(app, peer) == TaskStatus.OPEN


def test_scoped_token_claim_next_with_nothing_in_scope_claims_nothing(app: FastAPI) -> None:
    peer = _create_task(app, "peer task", priority=1)
    own = _create_task(app, "own task", priority=3)
    headers = _agent_headers(app, "backend-scoped", "backend", [own])

    first = _client(app).get("/tasks/next/backend", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["id"] == own

    # Its only in-scope task is taken: the next call must come back empty
    # rather than fall through to the peer task.
    second = _client(app).get("/tasks/next/backend", headers=headers)
    assert second.status_code == 404, second.text
    assert _status(app, peer) == TaskStatus.OPEN


def test_out_of_scope_tasks_stay_claimable_by_their_owner(app: FastAPI) -> None:
    peer = _create_task(app, "peer task", priority=1)
    own = _create_task(app, "own task", priority=3)
    scoped = _agent_headers(app, "backend-scoped", "backend", [own])
    owner = _agent_headers(app, "backend-owner", "backend", [peer])

    assert _client(app).get("/tasks/next/backend", headers=scoped).json()["id"] == own
    # Skipping the peer row must leave it queued, not drop it from the queue.
    response = _client(app).get("/tasks/next/backend", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == peer


def test_manager_token_claim_next_is_unrestricted(app: FastAPI) -> None:
    # A claim is bound to the token's role, so the manager claims manager tasks.
    peer = _create_task(app, "peer task", priority=1, role="manager")
    _create_task(app, "own task", priority=3, role="manager")
    headers = _agent_headers(app, "manager-root", "manager", [])

    response = _client(app).get("/tasks/next/manager", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == peer


def test_operator_claim_next_is_unchanged(app: FastAPI) -> None:
    peer = _create_task(app, "peer task", priority=1)
    _create_task(app, "own task", priority=3)

    response = _client(app).get("/tasks/next/backend", headers=_operator_headers())

    assert response.status_code == 200, response.text
    assert response.json()["id"] == peer


# ---------------------------------------------------------------------------
# POST /tasks/claim-receipt
# ---------------------------------------------------------------------------


def _seed_backlog(application: FastAPI, entries: list[BacklogEntry]) -> None:
    Backlog.write(application.state.claim_backlog_path, entries)


def _backlog_claimer(application: FastAPI, entry_id: str) -> str | None:
    for entry in Backlog.load(application.state.claim_backlog_path).entries:
        if entry.id == entry_id:
            return entry.claimer
    raise AssertionError(entry_id)


def test_scoped_token_claim_receipt_only_grants_an_in_scope_row(app: FastAPI) -> None:
    _seed_backlog(app, [BacklogEntry(id="row-peer", role="backend"), BacklogEntry(id="row-own", role="backend")])
    headers = _agent_headers(app, "backend-scoped", "backend", ["row-own"])

    response = _client(app).post("/tasks/claim-receipt", headers=headers, json={"claimer_id": "backend-scoped"})

    assert response.status_code == 200, response.text
    wire = response.json()
    assert wire["granted"] is True
    assert wire["taskId"] == "row-own"
    assert _backlog_claimer(app, "row-peer") is None


def test_scoped_token_claim_receipt_refuses_when_nothing_is_in_scope(app: FastAPI) -> None:
    _seed_backlog(app, [BacklogEntry(id="row-peer", role="backend")])
    headers = _agent_headers(app, "backend-scoped", "backend", ["row-own"])

    response = _client(app).post("/tasks/claim-receipt", headers=headers, json={"claimer_id": "backend-scoped"})

    assert response.status_code == 200, response.text
    assert response.json()["granted"] is False
    assert _backlog_claimer(app, "row-peer") is None


def test_operator_claim_receipt_is_unchanged(app: FastAPI) -> None:
    _seed_backlog(app, [BacklogEntry(id="row-peer", role="backend")])

    response = _client(app).post("/tasks/claim-receipt", headers=_operator_headers(), json={"claimer_id": "op"})

    assert response.status_code == 200, response.text
    assert response.json()["taskId"] == "row-peer"


def test_filter_digest_is_unchanged_for_an_unscoped_filter() -> None:
    # Receipts minted before the scope field existed must keep verifying, so
    # an unscoped filter hashes exactly as it did; a scoped one hashes apart.
    unscoped = ClaimFilter(role="backend", task_ids=None)
    assert filter_digest(unscoped) == _LEGACY_BACKEND_FILTER_DIGEST
    assert filter_digest(ClaimFilter(role="backend", task_ids=frozenset({"a"}))) != filter_digest(unscoped)
