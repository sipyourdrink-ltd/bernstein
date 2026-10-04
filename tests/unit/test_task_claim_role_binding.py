"""Task claims and role edits are bound to the role on the caller's agent token.

An agent identity is minted for one role.  The claim routes take that role
from the authenticated identity rather than from anything the caller sends,
and ``PATCH /tasks/{id}`` refuses a role that is not the caller's own, so a
worker cannot re-label a task into its role and then claim it.

Operator credentials carry no agent identity and keep full control.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-claim-role-tests"
_peer = itertools.count()


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
    )


def _client(application: FastAPI) -> TestClient:
    """A client with a fresh peer address so the write rate limiter stays out of the way."""
    index = next(_peer)
    return TestClient(application, client=(f"10.41.{index // 256}.{index % 256}", 43000 + index % 20000))


def _operator() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _agent(application: FastAPI, role: str, task_id: str) -> dict[str, str]:
    """Mint an agent identity token for *role* scoped to *task_id*.

    The token is in scope for the task on purpose: these tests exercise the
    role binding, and an out-of-scope token is refused by the task-scope gate
    before the role is ever compared.
    """
    store: Any = application.state.identity_store
    _, token = store.create_identity(f"session-{role}-{next(_peer)}", role, task_ids=[task_id])
    return {"Authorization": f"Bearer {token}"}


def _create_task(application: FastAPI, role: str) -> str:
    response = _client(application).post(
        "/tasks",
        headers=_operator(),
        json={"title": f"{role} work", "description": f"{role} work", "role": role},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _task(application: FastAPI, task_id: str) -> Any:
    task = application.state.store.get_task(task_id)
    assert task is not None
    return task


# ---------------------------------------------------------------------------
# POST /tasks/{id}/claim
# ---------------------------------------------------------------------------


def test_agent_cannot_claim_task_of_another_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).post(f"/tasks/{task_id}/claim", headers=_agent(app, "qa", task_id))

    assert response.status_code == 403, response.text
    assert "role" in response.json()["detail"]
    assert _task(app, task_id).status.value == "open"


def test_agent_claims_task_of_its_own_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).post(f"/tasks/{task_id}/claim", headers=_agent(app, "backend", task_id))

    assert response.status_code == 200, response.text
    assert _task(app, task_id).status.value == "claimed"


def test_operator_claim_is_not_role_bound(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).post(f"/tasks/{task_id}/claim", headers=_operator())

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# PATCH /tasks/{id}
# ---------------------------------------------------------------------------


def test_agent_cannot_relabel_task_into_its_own_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).patch(f"/tasks/{task_id}", headers=_agent(app, "qa", task_id), json={"role": "qa"})

    assert response.status_code == 403, response.text
    assert _task(app, task_id).role == "backend"


def test_agent_cannot_set_a_role_other_than_its_own(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).patch(f"/tasks/{task_id}", headers=_agent(app, "backend", task_id), json={"role": "qa"})

    assert response.status_code == 403, response.text
    assert _task(app, task_id).role == "backend"


def test_relabel_then_claim_is_refused_end_to_end(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")
    qa = _agent(app, "qa", task_id)

    _client(app).patch(f"/tasks/{task_id}", headers=qa, json={"role": "qa"})
    response = _client(app).post(f"/tasks/{task_id}/claim", headers=qa)

    assert response.status_code == 403, response.text
    assert _task(app, task_id).status.value == "open"


def test_agent_patch_without_role_still_allowed(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).patch(f"/tasks/{task_id}", headers=_agent(app, "qa", task_id), json={"priority": 1})

    assert response.status_code == 200, response.text
    assert _task(app, task_id).priority == 1


def test_operator_can_reassign_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).patch(f"/tasks/{task_id}", headers=_operator(), json={"role": "qa"})

    assert response.status_code == 200, response.text
    assert _task(app, task_id).role == "qa"


# ---------------------------------------------------------------------------
# Sibling claim routes
# ---------------------------------------------------------------------------


def test_agent_claim_batch_skips_tasks_of_another_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).post(
        "/tasks/claim-batch",
        headers=_agent(app, "qa", task_id),
        json={"task_ids": [task_id], "agent_id": "qa-agent"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["claimed"] == []
    assert _task(app, task_id).status.value == "open"


def test_agent_cannot_claim_next_for_another_role(app: FastAPI) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).get("/tasks/next/backend", headers=_agent(app, "qa", task_id))

    assert response.status_code == 403, response.text
    assert _task(app, task_id).status.value == "open"


# ---------------------------------------------------------------------------
# Auto-claim inside /complete and /fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "body"),
    [("complete", {"result_summary": "done"}), ("fail", {"reason": "gave up"})],
)
def test_agent_cannot_auto_claim_open_task_of_another_role(app: FastAPI, action: str, body: dict[str, str]) -> None:
    """``/complete`` and ``/fail`` re-claim a task that reverted to open; that claim is role-bound too."""
    task_id = _create_task(app, "backend")

    response = _client(app).post(f"/tasks/{task_id}/{action}", headers=_agent(app, "qa", task_id), json=body)

    assert response.status_code == 403, response.text
    assert "role" in response.json()["detail"]
    assert _task(app, task_id).status.value == "open"


@pytest.mark.parametrize(
    ("action", "body", "final"),
    [("complete", {"result_summary": "done"}, "done"), ("fail", {"reason": "gave up"}, "failed")],
)
def test_agent_finishes_reverted_task_of_its_own_role(
    app: FastAPI, action: str, body: dict[str, str], final: str
) -> None:
    task_id = _create_task(app, "backend")

    response = _client(app).post(f"/tasks/{task_id}/{action}", headers=_agent(app, "backend", task_id), json=body)

    assert response.status_code == 200, response.text
    assert _task(app, task_id).status.value == final
