"""Route-permission enforcement for the cluster shared secret.

The cluster secret is a worker credential: one string handed to every node
in the fleet so that a single token clears both the outer middleware and the
inner cluster route layer (#2805).  It authenticated and then went straight
to ``call_next``, bounded only by the operator-only (``admin:manage``)
refusal on writes, so the permission a route declares constrained every
credential kind except this one - the broadest one.

The fleet secret now carries a fixed authority
(``_CLUSTER_SECRET_PERMISSIONS``): what joining and working a cluster needs,
and nothing a worker never does.  Each surface is pinned twice - refused for
a permission outside the set, served for one inside it - so the gate is
shown to key on the declared permission rather than on the path or on the
credential kind.

The inner layer is covered here too: ``POST /cluster/steal`` was the one
mutating ``/cluster/*`` route with no ``_verify_cluster_auth`` call, so the
outer check was its only gate while the surrounding routes had two.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.models import ClusterConfig
from fastapi.testclient import TestClient

from bernstein.core.security.auth_middleware import (
    _CLUSTER_SECRET_PERMISSIONS,
    _get_required_permission,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-cluster-secret-tests"  # NOSONAR - test fixture
_CLUSTER_SECRET = "cluster-shared-secret-for-route-permission-tests"  # NOSONAR - test fixture

# The session the fleet credential tries to read, stream, or kill.  No worker
# reaches another session's agent over HTTP; that is what makes this the
# surface a fleet-wide string must not carry.
_VICTIM_SESSION = "backend-victim02"

# Content of the bulletin message the refused write tries to append, read
# back off the board to prove the refusal stopped the write.
_BULLETIN_PROBE = "cluster-probe-that-must-not-be-published"

_NODE_PAYLOAD: dict[str, Any] = {
    "name": "worker-perm-1",
    "url": "",
    "capacity": {
        "max_agents": 4,
        "available_slots": 4,
        "active_agents": 0,
        "gpu_available": False,
        "supported_models": ["sonnet"],
    },
    "labels": {},
    "cell_ids": [],
}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application with cluster mode on and both credentials wired."""
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        cluster_config=ClusterConfig(enabled=True, auth_token=_CLUSTER_SECRET),
        plan_mode=True,
    )


def _client(application: FastAPI, index: int) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    return TestClient(application, client=(f"10.41.{index // 256}.{index % 256}", 44000 + index))


def _cluster_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_CLUSTER_SECRET}"}


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


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
# The authority the fleet credential carries
# ---------------------------------------------------------------------------


def test_cluster_secret_authority_covers_cluster_and_task_work_only() -> None:
    """The premise of every refusal below, stated as the set itself.

    Without this the refusals could pass for a reason unrelated to the
    permission, and a later widening of the set would go unnoticed because
    every refusal would simply stop being reached.
    """
    expected = {
        "cluster:write",  # register, heartbeat, cordon, drain, unregister, gossip, rebalance
        "cluster:read",  # the node registry a worker reads back
        "tasks:write",  # complete / fail / release the work it pulled
        "tasks:read",  # pull the next task for a role
        "status:read",  # the read floor every credential holds
    }

    assert set(_CLUSTER_SECRET_PERMISSIONS) == expected


def test_cluster_secret_authority_excludes_the_agent_and_operator_surfaces() -> None:
    """Named individually, because each one is a route class refused below."""
    for permission in (
        "agents:read",
        "agents:kill",
        "agents:write",
        "bulletin:write",
        "bulletin:read",
        "auth:manage",
        "webhooks:manage",
        "admin:read",
        "admin:manage",
    ):
        assert permission not in _CLUSTER_SECRET_PERMISSIONS, permission


def test_routes_refused_below_declare_the_permissions_they_are_checked_against() -> None:
    """The route map names the permissions the assertions below quote."""
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/logs", "GET") == "agents:read"
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/stream", "GET") == "agents:read"
    assert _get_required_permission(f"/agents/{_VICTIM_SESSION}/kill", "POST") == "agents:kill"
    assert _get_required_permission("/bulletin", "POST") == "bulletin:write"
    assert _get_required_permission("/bulletin", "GET") == "bulletin:read"


# ---------------------------------------------------------------------------
# Route class: agent session control (``agents:kill``)
# ---------------------------------------------------------------------------


def test_cluster_secret_cannot_kill_an_agent_session_without_agents_kill(app: FastAPI, tmp_path: Path) -> None:
    """The fleet credential cannot request termination of an agent session.

    Asserts the side effect as well as the status: the route's whole
    behaviour is writing a ``.kill`` signal file the orchestrator polls, so a
    refusal that still wrote the file would terminate the session anyway.
    """
    runtime_dir = tmp_path / ".sdd" / "runtime"

    response = _client(app, 1).post(f"/agents/{_VICTIM_SESSION}/kill", headers=_cluster_headers())

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "agents:kill"
    assert not (runtime_dir / f"{_VICTIM_SESSION}.kill").exists()


# ---------------------------------------------------------------------------
# Route class: agent log / stream reads (``agents:read``)
# ---------------------------------------------------------------------------


def test_cluster_secret_cannot_read_an_agent_log_without_agents_read(app: FastAPI) -> None:
    """A worker drives its own agents locally; the HTTP log surface is not its own."""
    response = _client(app, 2).get(f"/agents/{_VICTIM_SESSION}/logs", headers=_cluster_headers())

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["required_permission"] == "agents:read"
    # The refusal replaces the payload rather than accompanying it.
    assert "content" not in body


def test_cluster_secret_cannot_stream_an_agent_session_without_agents_read(app: FastAPI) -> None:
    """The SSE stream of a session's output is refused on the same authority."""
    response = _client(app, 3).get(f"/agents/{_VICTIM_SESSION}/stream", headers=_cluster_headers())

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "agents:read"


# ---------------------------------------------------------------------------
# Route class: bulletin write (``bulletin:write``)
# ---------------------------------------------------------------------------


def test_cluster_secret_cannot_post_to_the_bulletin_without_bulletin_write(app: FastAPI) -> None:
    """A fleet credential cannot publish to the board agents coordinate on.

    The body is a valid ``BulletinPostRequest`` and the board is read back
    afterwards, so this fails on the authorisation and not on schema
    validation, and a refusal that still appended would be caught.
    """
    response = _client(app, 4).post(
        "/bulletin",
        headers=_cluster_headers(),
        json={"agent_id": "worker-perm-1", "type": "status", "content": _BULLETIN_PROBE},
    )

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "bulletin:write"

    board = _client(app, 5).get("/bulletin", headers=_operator_headers())
    assert board.status_code == 200, board.text
    assert all(message["content"] != _BULLETIN_PROBE for message in board.json())


# ---------------------------------------------------------------------------
# Route class: operator surfaces
# ---------------------------------------------------------------------------


def test_cluster_secret_cannot_read_the_bulletin_without_bulletin_read(app: FastAPI) -> None:
    """Reads are gated too, on a route that really returns the data.

    The pre-existing operator-only refusal ran on non-read methods only, so a
    read of a surface the fleet credential holds nothing for was a gap that
    check could not close by construction. The board carries what every agent
    posts, so a fleet-wide string reading it is the whole coordination
    history, not an empty 404.
    """
    posted = _client(app, 6).post(
        "/bulletin",
        headers=_operator_headers(),
        json={"agent_id": "operator", "type": "status", "content": _BULLETIN_PROBE},
    )
    assert posted.status_code == 201, posted.text

    response = _client(app, 17).get("/bulletin", headers=_cluster_headers())

    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "bulletin:read"
    assert _BULLETIN_PROBE not in response.text


def test_cluster_secret_keeps_its_operator_specific_refusal(app: FastAPI) -> None:
    """The pre-existing ``admin:manage`` message is not replaced by the general one.

    Both gates would refuse ``/shutdown``; the operator-only message is the
    more specific one and has to keep winning.
    """
    response = _client(app, 7).post("/shutdown", headers=_cluster_headers(), json={})

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Cluster credential cannot access operator-only endpoints"


# ---------------------------------------------------------------------------
# What a worker must keep reaching
# ---------------------------------------------------------------------------


def test_cluster_secret_still_registers_and_heartbeats_a_node(app: FastAPI) -> None:
    """Node join is the credential's reason to exist and stays reachable.

    Also the positive control for the refusals above: the gate keys on the
    permission a route declares, not on the credential kind.
    """
    registered = _client(app, 8).post("/cluster/nodes", headers=_cluster_headers(), json=_NODE_PAYLOAD)
    assert registered.status_code == 201, registered.text
    node_id = registered.json()["id"]

    heartbeat = _client(app, 9).post(
        f"/cluster/nodes/{node_id}/heartbeat",
        headers=_cluster_headers(),
        json={"capacity": None},
    )
    assert heartbeat.status_code == 200, heartbeat.text


def test_cluster_secret_still_reads_the_node_registry(app: FastAPI) -> None:
    """``cluster:read`` is in the set, so the fleet view stays readable."""
    response = _client(app, 10).get("/cluster/status", headers=_cluster_headers())

    assert response.status_code == 200, response.text


def test_cluster_secret_still_claims_and_completes_a_task(app: FastAPI) -> None:
    """Pulling work and reporting it done is what a worker node does.

    A gate that refused ``tasks:write`` for this credential would leave every
    worker able to join a cluster and unable to finish anything in it.
    """
    task_id = _create_task(app, 11, "cluster-secret-completes")

    claimed = _client(app, 12).post(f"/tasks/{task_id}/claim", headers=_cluster_headers())
    assert claimed.status_code == 200, claimed.text

    completed = _client(app, 13).post(
        f"/tasks/{task_id}/complete",
        headers=_cluster_headers(),
        json={"result_summary": "done"},
    )
    assert completed.status_code == 200, completed.text


def test_cluster_secret_still_pulls_the_next_task_for_a_role(app: FastAPI) -> None:
    """``tasks:read`` covers the claim-next pull the worker loop polls."""
    _create_task(app, 14, "cluster-secret-pulls")

    response = _client(app, 15).get("/tasks/next/backend", headers=_cluster_headers())

    assert response.status_code not in {401, 403}, response.text


def test_cluster_secret_still_reads_status(app: FastAPI) -> None:
    """``status:read`` is the read floor every credential keeps."""
    response = _client(app, 16).get("/status", headers=_cluster_headers())

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The inner cluster layer on POST /cluster/steal
# ---------------------------------------------------------------------------


@pytest.fixture
def inner_layer_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Cluster auth on, outer bearer auth off, to reach the inner layer alone.

    The outer middleware refuses a node JWT outright - it is neither the raw
    secret nor any other credential it knows - so with both layers on, a
    scope refusal from the inner layer could never be observed.
    """
    from bernstein.core.server import create_app

    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        cluster_config=ClusterConfig(enabled=True, auth_token=_CLUSTER_SECRET),
        plan_mode=True,
    )


def test_cluster_steal_refuses_an_unauthenticated_request_like_its_sibling_routes(
    inner_layer_app: FastAPI,
) -> None:
    """``POST /cluster/steal`` had no inner check while every sibling had one.

    Register, heartbeat, cordon, uncordon, drain, unregister and gossip all
    call ``_verify_cluster_auth``; steal did not, so on a cluster-auth
    deployment it was the one mutating cluster route reachable with no
    cluster credential at all.
    """
    response = TestClient(inner_layer_app).post("/cluster/steal", json={"queue_depths": {}})

    assert response.status_code == 401, response.text


def test_cluster_steal_refuses_a_node_token_without_the_node_admin_scope(
    inner_layer_app: FastAPI,
) -> None:
    """Steal is a node-registry mutation, scoped like cordon / drain / unregister.

    An issued node token carries register + heartbeat, which is enough to
    join and stay in the fleet and deliberately not enough to reassign other
    nodes' claimed work.
    """
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT

    authenticator: Any = inner_layer_app.state.cluster_authenticator
    token = authenticator.issue_node_token("worker-perm-2", scopes=[SCOPE_NODE_HEARTBEAT])

    response = TestClient(inner_layer_app).post(
        "/cluster/steal",
        headers={"Authorization": f"Bearer {token}"},
        json={"queue_depths": {}},
    )

    assert response.status_code == 401, response.text


def test_cluster_steal_is_served_for_a_credential_holding_the_node_admin_scope(
    inner_layer_app: FastAPI,
) -> None:
    """The positive control: the shared secret carries the full node scope set."""
    response = TestClient(inner_layer_app).post(
        "/cluster/steal",
        headers={"Authorization": f"Bearer {_CLUSTER_SECRET}"},
        json={"queue_depths": {}},
    )

    assert response.status_code == 200, response.text


def test_every_mutating_cluster_route_verifies_cluster_auth() -> None:
    """The property the steal fix restores, read off the router itself.

    Enumerating the registered routes rather than a hand-kept list is the
    point: a mutating ``/cluster/*`` route added later and missing the inner
    check fails here, instead of shipping with the outer gate as its only
    layer the way steal did.
    """
    import inspect

    from starlette.routing import Route

    from bernstein.core.routes import task_cluster

    read_methods = {"GET", "HEAD", "OPTIONS"}
    checked: list[str] = []
    for route in task_cluster.router.routes:
        assert isinstance(route, Route)
        if not route.path.startswith("/cluster") or not (set(route.methods or ()) - read_methods):
            continue
        source = inspect.getsource(route.endpoint)
        assert "_verify_cluster_auth(request," in source, route.path
        checked.append(route.path)

    # The enumeration itself has to be non-vacuous.
    assert "/cluster/steal" in checked
    assert len(checked) >= 8
