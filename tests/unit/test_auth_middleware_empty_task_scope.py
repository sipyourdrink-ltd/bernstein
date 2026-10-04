"""An empty task scope is unrestricted only for the roles minted unscoped.

``task_ids == []`` used to mean "unrestricted" for every agent token,
whatever its role.  Only one identity is ever minted that way on purpose:
the run-root ``manager`` identity the orchestrator creates per run.  The
spawner always mints workers with the task list they were spawned for, so a
worker-role token with an empty list is not a manager - it is a worker
whose scope is empty, and it must reach no task at all rather than every
task.

The rule is checked on each surface the scope applies to: the path-level
gate, the body-addressed handlers, the server-chosen claim, and the mailbox
route the path gate defers to its handler.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.models import TaskStatus
from fastapi.testclient import TestClient

from bernstein.core.identity.agent_jwt import AGENT_ROLE_PERMISSIONS, UNSCOPED_AGENT_ROLES
from bernstein.core.security.auth_middleware import check_agent_task_scope_ids

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-empty-scope-tests"

# Worker roles the spawner mints; every one of them must fail closed on an
# empty scope.  ``reviewer`` is not in ``AGENT_ROLE_PERMISSIONS`` and so
# exercises the default grant.
_WORKER_ROLES = ("backend", "frontend", "qa", "security", "devops", "reviewer")


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application, with an operator bearer token for fixture setup."""
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )


_client_counter = iter(range(1, 1_000_000))


def _client(application: FastAPI) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    index = next(_client_counter)
    return TestClient(application, client=(f"10.43.{index // 256}.{index % 256}", 45000 + index % 20000))


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _agent_headers(application: FastAPI, session: str, role: str, task_ids: list[str]) -> dict[str, str]:
    identity_store: Any = application.state.identity_store
    _, token = identity_store.create_identity(session, role, task_ids=task_ids)
    return {"Authorization": f"Bearer {token}"}


def _create_task(application: FastAPI, title: str, role: str = "backend") -> str:
    response = _client(application).post(
        "/tasks",
        headers=_operator_headers(),
        json={"title": title, "description": title, "role": role},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _task(application: FastAPI, task_id: str) -> Any:
    task = application.state.store.get_task(task_id)
    assert task is not None, task_id
    return task


# ---------------------------------------------------------------------------
# The unscoped-role set
# ---------------------------------------------------------------------------


def test_unscoped_roles_are_real_agent_roles() -> None:
    # A typo here would silently fail every manager token closed.
    assert UNSCOPED_AGENT_ROLES
    assert set(UNSCOPED_AGENT_ROLES) <= set(AGENT_ROLE_PERMISSIONS)


def test_run_root_identity_role_is_unscoped() -> None:
    # The orchestrator mints the run root as ``manager`` with no task list;
    # that identity is the reason an unscoped role exists at all.
    assert "manager" in UNSCOPED_AGENT_ROLES


# ---------------------------------------------------------------------------
# Path-addressed task routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _WORKER_ROLES)
def test_worker_with_empty_scope_cannot_mutate_a_task(app: FastAPI, role: str) -> None:
    target = _create_task(app, "target", role=role)
    headers = _agent_headers(app, f"{role}-noscope", role, [])

    claim = _client(app).post(f"/tasks/{target}/claim", headers=headers)
    cancel = _client(app).post(f"/tasks/{target}/cancel", headers=headers, json={"reason": "x"})
    patch = _client(app).patch(f"/api/v1/tasks/{target}", headers=headers, json={"priority": 0})

    assert claim.status_code == 403, claim.text
    assert cancel.status_code == 403, cancel.text
    assert patch.status_code == 403, patch.text
    assert _task(app, target).status == TaskStatus.OPEN


def test_worker_with_empty_scope_can_still_read(app: FastAPI) -> None:
    target = _create_task(app, "target")
    headers = _agent_headers(app, "backend-noscope", "backend", [])

    response = _client(app).get(f"/tasks/{target}", headers=headers)

    assert response.status_code == 200, response.text


def test_manager_with_empty_scope_is_unrestricted(app: FastAPI) -> None:
    target = _create_task(app, "target")
    headers = _agent_headers(app, "manager-root", "manager", [])

    response = _client(app).post(f"/tasks/{target}/cancel", headers=headers, json={"reason": "x"})

    assert response.status_code == 200, response.text
    assert _task(app, target).status == TaskStatus.CANCELLED


def test_manager_with_a_scope_is_held_to_it(app: FastAPI) -> None:
    own = _create_task(app, "own")
    peer = _create_task(app, "peer")
    headers = _agent_headers(app, "manager-scoped", "manager", [own])

    response = _client(app).post(f"/tasks/{peer}/cancel", headers=headers, json={"reason": "x"})

    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Body-addressed, server-chosen and handler-deferred routes
# ---------------------------------------------------------------------------


def test_worker_with_empty_scope_cannot_act_through_a_body_addressed_route(app: FastAPI) -> None:
    target = _create_task(app, "target")
    headers = _agent_headers(app, "backend-noscope", "backend", [])

    response = _client(app).post(
        "/tasks/claim-batch",
        headers=headers,
        json={"task_ids": [target], "agent_id": "backend-noscope"},
    )

    assert response.status_code == 403, response.text
    assert _task(app, target).status == TaskStatus.OPEN


def test_worker_with_empty_scope_claims_nothing_through_claim_next(app: FastAPI) -> None:
    target = _create_task(app, "target")
    headers = _agent_headers(app, "backend-noscope", "backend", [])

    response = _client(app).get("/tasks/next/backend", headers=headers)

    assert response.status_code == 404, response.text
    assert _task(app, target).status == TaskStatus.OPEN


def test_manager_with_empty_scope_claims_through_claim_next(app: FastAPI) -> None:
    # A claim is bound to the token's role, so the manager claims a manager task.
    target = _create_task(app, "target", role="manager")
    headers = _agent_headers(app, "manager-root", "manager", [])

    response = _client(app).get("/tasks/next/manager", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == target


def test_worker_with_empty_scope_cannot_post_to_a_task_mailbox(app: FastAPI) -> None:
    target = _create_task(app, "target")
    headers = _agent_headers(app, "backend-noscope", "backend", [])

    response = _client(app).post(
        f"/tasks/{target}/messages",
        headers=headers,
        json={"sender": "backend-noscope", "kind": "finding", "body": "x"},
    )

    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# The id-list helper
# ---------------------------------------------------------------------------


def test_scope_helper_distinguishes_unrestricted_from_empty() -> None:
    assert check_agent_task_scope_ids(None, ["any"]) is None
    assert check_agent_task_scope_ids([], ["any"]) is not None
    assert check_agent_task_scope_ids([], []) is None
    assert check_agent_task_scope_ids(["a"], ["a"]) is None
    assert check_agent_task_scope_ids(["a"], ["b"]) is not None
