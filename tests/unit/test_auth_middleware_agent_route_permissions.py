"""Route-permission enforcement for agent identity tokens.

The middleware gates every credential on the permission the requested route
declares (``_get_required_permission``).  The SSO principal has always been
checked against that permission; an agent identity authenticated and then
went straight to the handler, so the only bound the declared permission
placed was on SSO users.

The gap was not a missing entry in a denylist - it was the check itself.  A
task-scoped worker token read another session's agent log and stream and
requested that session's termination while holding neither ``agents:read``
nor ``agents:kill``, and the same held for every other surface an agent
grant does not cover (``/cluster``, ``/bulletin``, ...).

These tests run against the real application and the real routes, because
the property is about what an authenticated read actually returns: a stub
handler would prove only that a stub was not called.  Each route class is
pinned twice - refused for an identity that does not hold the permission,
served for one that does - so the gate is shown to key on the held
permission rather than on the path.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.identity.agent_jwt import permissions_for_role
from bernstein.core.security.auth_middleware import _get_required_permission

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-route-permission-tests"

# The session an agent token tries to read, stream, or kill.  It is never the
# caller's own session, which is the point: the gate is about the permission,
# not about which session id the caller names.
_VICTIM_SESSION = "backend-victim01"

# Content of the bulletin message the refused writes try to append.  Read
# back off the board to prove the refusal stopped the write, not just the
# response.
_BULLETIN_PROBE = "probe-that-must-not-be-published"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application, with an operator bearer token for fixture setup."""
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )


def _client(application: FastAPI, index: int) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    return TestClient(application, client=(f"10.40.{index // 256}.{index % 256}", 43000 + index))


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _agent_headers(
    application: FastAPI,
    session: str,
    task_ids: list[str],
    *,
    role: str = "backend",
    extra_permissions: frozenset[str] | None = None,
) -> dict[str, str]:
    """Mint an agent identity token scoped to *task_ids*."""
    identity_store: Any = application.state.identity_store
    _, token = identity_store.create_identity(
        session,
        role,
        task_ids=task_ids,
        extra_permissions=extra_permissions,
    )
    return {"Authorization": f"Bearer {token}"}


def _create_task(application: FastAPI, index: int, title: str) -> str:
    """Create a task with the operator credential and return its id."""
    response = _client(application, index).post(
        "/tasks",
        headers=_operator_headers(),
        json={"title": title, "description": title, "role": "backend"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# ---------------------------------------------------------------------------
# The grant a worker agent actually carries
# ---------------------------------------------------------------------------


def test_worker_role_grant_covers_neither_agents_read_nor_agents_kill() -> None:
    """The premise of every refusal below: a worker grant omits both.

    Without this the refusals could pass for a reason unrelated to the
    permission - a role that happened to hold ``agents:read`` would make the
    ``/agents`` assertions vacuous the day the grant changed.
    """
    backend = permissions_for_role("backend")

    assert "agents:read" not in backend
    assert "agents:kill" not in backend
    assert "cluster:write" not in backend
    assert "bulletin:write" not in backend


def test_agent_log_and_kill_routes_declare_the_permissions_they_are_checked_against() -> None:
    """The route map names ``agents:read`` for the reads and ``agents:kill`` for the kill."""
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/logs", "GET") == "agents:read"
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/stream", "GET") == "agents:read"
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/kill", "POST") == "agents:kill"


# ---------------------------------------------------------------------------
# Route class: agent log / stream reads (``agents:read``)
# ---------------------------------------------------------------------------


def test_agent_log_read_is_refused_without_agents_read(app: FastAPI) -> None:
    """A worker token cannot read another session's agent log."""
    own_id = _create_task(app, 1, "own-log-read")
    headers = _agent_headers(app, "session-log-read", [own_id])

    response = _client(app, 2).get(f"/agents/{_VICTIM_SESSION}/logs", headers=headers)

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["required_permission"] == "agents:read"
    # The refusal replaces the payload rather than accompanying it.
    assert "content" not in body


def test_agent_stream_read_is_refused_without_agents_read(app: FastAPI) -> None:
    """The SSE stream of another session's output is refused on the same grant."""
    own_id = _create_task(app, 3, "own-stream-read")
    headers = _agent_headers(app, "session-stream-read", [own_id])

    response = _client(app, 4).get(f"/agents/{_VICTIM_SESSION}/stream", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "agents:read"


def test_agent_log_read_is_served_when_agents_read_is_held(app: FastAPI) -> None:
    """Holding the declared permission reaches the handler.

    Pins that the refusals above come from the permission check and not from
    a blanket ban on ``/agents`` for agent credentials.
    """
    own_id = _create_task(app, 5, "own-log-granted")
    headers = _agent_headers(
        app,
        "session-log-granted",
        [own_id],
        extra_permissions=frozenset({"agents:read"}),
    )

    response = _client(app, 6).get(f"/agents/{_VICTIM_SESSION}/logs", headers=headers)

    # 404 is the handler answering for a session with no log file on disk -
    # the request reached it, which is what this pins.
    assert response.status_code in {200, 404}, response.text


# ---------------------------------------------------------------------------
# Route class: session kill (``agents:kill``)
# ---------------------------------------------------------------------------


def test_session_kill_is_refused_without_agents_kill(app: FastAPI, tmp_path: Path) -> None:
    """A worker token cannot request termination of another session.

    Asserts the side effect as well as the status: the route's whole
    behaviour is writing a ``.kill`` signal file the orchestrator polls, so
    a refusal that still wrote the file would terminate the session anyway.
    """
    own_id = _create_task(app, 7, "own-kill")
    headers = _agent_headers(app, "session-kill", [own_id])
    runtime_dir = tmp_path / ".sdd" / "runtime"

    response = _client(app, 8).post(f"/agents/{_VICTIM_SESSION}/kill", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "agents:kill"
    assert not (runtime_dir / f"{_VICTIM_SESSION}.kill").exists()


def test_session_kill_is_accepted_when_agents_kill_is_held(app: FastAPI) -> None:
    """An identity granted ``agents:kill`` still reaches the kill route."""
    own_id = _create_task(app, 9, "own-kill-granted")
    headers = _agent_headers(
        app,
        "session-kill-granted",
        [own_id],
        extra_permissions=frozenset({"agents:kill"}),
    )

    response = _client(app, 10).post(f"/agents/{_VICTIM_SESSION}/kill", headers=headers)

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Route class: other surfaces an agent grant does not cover
# ---------------------------------------------------------------------------


def test_cluster_rebalance_is_refused_without_cluster_write(app: FastAPI) -> None:
    """Cluster redistribution belongs to the cluster credential, not a worker token."""
    own_id = _create_task(app, 11, "own-cluster")
    headers = _agent_headers(app, "session-cluster", [own_id])

    response = _client(app, 12).post("/cluster/steal", headers=headers, json={"queue_depths": {}})

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "cluster:write"


def test_bulletin_post_is_refused_without_bulletin_write(app: FastAPI) -> None:
    """A worker grant carries no bulletin permission, so the write is refused.

    The body is a valid :class:`BulletinPostRequest`, and the board is read
    back afterwards: a request the schema rejects would 422 before the
    handler either way, which would make this pass for a reason that has
    nothing to do with authorisation, and a refusal that still appended the
    message would have published it regardless of the status code.
    """
    own_id = _create_task(app, 13, "own-bulletin")
    headers = _agent_headers(app, "session-bulletin", [own_id])

    response = _client(app, 14).post(
        "/bulletin",
        headers=headers,
        json={"agent_id": "session-bulletin", "type": "status", "content": _BULLETIN_PROBE},
    )

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "bulletin:write"

    board = _client(app, 24).get("/bulletin", headers=_operator_headers())
    assert board.status_code == 200, board.text
    assert all(message["content"] != _BULLETIN_PROBE for message in board.json())


def test_bulletin_post_body_matches_the_route_schema() -> None:
    """The refusal above is pinned to the real schema, not to a 422.

    ``BulletinPostRequest`` is the model the route validates against.  If the
    probe body drifts from it, the test above stops exercising the permission
    gate and starts exercising body validation.
    """
    from bernstein.core.server.server_models import BulletinPostRequest

    assert set(BulletinPostRequest.model_fields) == {"agent_id", "type", "content", "cell_id"}
    BulletinPostRequest(agent_id="session-bulletin", type="status", content=_BULLETIN_PROBE)


# ---------------------------------------------------------------------------
# Routes a worker agent must keep reaching
# ---------------------------------------------------------------------------


def test_task_scoped_agent_still_completes_its_own_task(app: FastAPI) -> None:
    """``tasks:claim`` is the worker grant for the write ``/complete`` performs.

    The route declares ``tasks:write``; the spawner issues ``tasks:claim``
    for the same authority, and the task-scope gate keeps it bound to the
    agent's own tasks.  A gate that read the two names as different
    permissions would lock every worker out of finishing its work.
    """
    own_id = _create_task(app, 15, "own-complete")
    headers = _agent_headers(app, "session-complete", [own_id])
    claimed = _client(app, 16).post(f"/tasks/{own_id}/claim", headers=headers)
    assert claimed.status_code == 200, claimed.text

    response = _client(app, 17).post(f"/tasks/{own_id}/complete", headers=headers, json={"result_summary": "done"})

    assert response.status_code == 200, response.text


def test_task_scoped_agent_still_creates_subtasks(app: FastAPI) -> None:
    """``POST /tasks`` is in the agent contract and stays reachable."""
    own_id = _create_task(app, 18, "own-subtask-parent")
    headers = _agent_headers(app, "session-subtask", [own_id])

    response = _client(app, 19).post(
        "/tasks",
        headers=headers,
        json={"title": "subtask", "description": "subtask", "role": "backend"},
    )

    assert response.status_code == 201, response.text


def test_status_read_stays_reachable_for_a_worker_token(app: FastAPI) -> None:
    """``status:read`` is in every agent role grant, so ``/status`` is unaffected."""
    own_id = _create_task(app, 20, "own-status")
    headers = _agent_headers(app, "session-status", [own_id])

    response = _client(app, 21).get("/status", headers=headers)

    assert response.status_code == 200, response.text


def test_operator_only_route_keeps_its_operator_specific_refusal(app: FastAPI) -> None:
    """The pre-existing ``admin:manage`` refusal is not replaced by the general one.

    Both gates would refuse ``/shutdown``; the operator-only message is the
    more specific one and has to keep winning.
    """
    own_id = _create_task(app, 22, "own-shutdown")
    headers = _agent_headers(app, "session-shutdown", [own_id])

    response = _client(app, 23).post("/shutdown", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Agent tokens cannot access operator-only endpoints"


# ---------------------------------------------------------------------------
# The equivalence itself
# ---------------------------------------------------------------------------


def test_task_write_equivalence_does_not_leak_into_unrelated_permissions() -> None:
    """``tasks:claim`` stands in for ``tasks:write`` and for nothing else."""
    from types import SimpleNamespace

    from bernstein.core.security.auth_middleware import _agent_holds_permission

    held = {"tasks:claim", "tasks:read", "status:read"}
    identity = SimpleNamespace(has_permission=lambda perm: perm in held)

    assert _agent_holds_permission(identity, "tasks:write")
    assert _agent_holds_permission(identity, "tasks:read")
    assert not _agent_holds_permission(identity, "agents:read")
    assert not _agent_holds_permission(identity, "agents:kill")
    assert not _agent_holds_permission(identity, "cluster:write")
    assert not _agent_holds_permission(identity, "admin:manage")
