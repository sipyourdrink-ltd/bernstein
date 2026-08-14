"""Task scoping for ids the URL path does not carry (#3036).

The companion module ``test_auth_middleware_task_scope_routes.py`` covers the
``/tasks/`` surface: paths that name a task and the collection routes that
take ids in their body.  This module covers the rest of the ways a request
reaches a task, because a rule enforced on only some of them is enforced on
an arbitrary subset:

* a **parent id** in a create body, which grafts a child onto an existing
  task and changes when that task can complete;
* a **body-carried id** on a route outside ``/tasks/`` (``POST /a2a/message``
  appends progress to the task it names);
* an id the handler **resolves indirectly** - the Bernstein task behind an
  ACP run, the tasks a plan decision promotes or cancels, the tasks a cluster
  steal reassigns;
* per-task routes whose path names ``{task_id}`` under some prefix other than
  ``/tasks/`` (``/approvals/{task_id}/approve`` and the review board), which
  a ``/tasks/``-anchored pattern cannot see.

Several of those routes are also refused for agent tokens by the fail-closed
``admin:manage`` fallback in ``_get_required_permission``, which answers 403
for reasons that have nothing to do with task scope.  Tests that would
otherwise be measuring that fallback give the route a non-admin permission
first, so the assertion is about the task-scope rule and not about which
route happens to be missing from ``_ROUTE_PERMISSIONS`` today.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.auth_middleware import (
    _check_agent_task_scope,
    task_id_route_patterns,
)
from bernstein.core.models import TaskStatus
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-indirect-scope-tests"
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_OUT_OF_SCOPE_TASK_ID = "task-not-mine"
_IN_SCOPE_TASK_ID = "task-mine"

# Any registered path template that addresses a task by id, under any prefix.
_TASK_ID_TEMPLATE_RE = re.compile(r"\{task_id\}")

# Sanity floor for the enumeration below: the app registers per-task routes
# outside ``/tasks/`` (approvals, the review board), so a matcher set that
# found none of them would make the assertion pass vacuously.
_MIN_NON_TASKS_PREFIX_ROUTES = 2


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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
    return TestClient(application, client=(f"10.30.{index // 256}.{index % 256}", 42000 + index))


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _agent_headers(application: FastAPI, session: str, task_ids: list[str]) -> dict[str, str]:
    """Mint an agent identity token scoped to *task_ids*."""
    identity_store: Any = application.state.identity_store
    _, token = identity_store.create_identity(session, "backend", task_ids=task_ids)
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


def _task(application: FastAPI, task_id: str) -> Any:
    task = application.state.store.get_task(task_id)
    assert task is not None, task_id
    return task


def _grant_non_admin_permission(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    """Give *prefix* a permission the agent under test holds.

    An agent token is refused outright on any route whose required
    permission it does not hold - ``admin:manage`` for a prefix missing from
    ``_ROUTE_PERMISSIONS``, ``cluster:write`` for ``/cluster``, and so on -
    which would answer 403 whatever the task scope says.  Giving the prefix
    ``tasks:write`` for the duration of a test isolates the task-scope rule
    from the permission gate, and states the invariant the rule has to hold
    under: the scoping must not depend on which permission a prefix carries
    in ``_ROUTE_PERMISSIONS``.
    """
    from bernstein.core.security import auth_middleware

    monkeypatch.setitem(auth_middleware._ROUTE_PERMISSIONS, prefix, "tasks:write")


# ---------------------------------------------------------------------------
# Parent ids in a create body
# ---------------------------------------------------------------------------


def test_create_task_denies_an_out_of_scope_parent(app: FastAPI) -> None:
    """``POST /tasks`` cannot graft a child onto another agent's task."""
    victim_id = _create_task(app, 0, "victim-parent")
    own_id = _create_task(app, 1, "own-task")
    headers = _agent_headers(app, "session-create-parent", [own_id])

    response = _client(app, 2).post(
        "/tasks",
        headers=headers,
        json={"title": "child", "description": "child", "role": "backend", "parent_task_id": victim_id},
    )

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    assert app.state.store.count_subtasks(victim_id) == 0


def test_create_task_allows_the_agents_own_parent(app: FastAPI) -> None:
    """The same route still works when the parent is the token's own task."""
    own_id = _create_task(app, 3, "own-parent")
    headers = _agent_headers(app, "session-create-own-parent", [own_id])

    response = _client(app, 4).post(
        "/tasks",
        headers=headers,
        json={"title": "child", "description": "child", "role": "backend", "parent_task_id": own_id},
    )

    assert response.status_code == 201, response.text
    assert response.json()["parent_task_id"] == own_id


def test_create_task_without_a_parent_is_unaffected(app: FastAPI) -> None:
    """A create that names no parent names no existing task, so it is allowed."""
    own_id = _create_task(app, 5, "own-noparent")
    headers = _agent_headers(app, "session-create-noparent", [own_id])

    response = _client(app, 6).post(
        "/tasks",
        headers=headers,
        json={"title": "standalone", "description": "standalone", "role": "backend"},
    )

    assert response.status_code == 201, response.text


def test_batch_create_denies_an_out_of_scope_parent(app: FastAPI) -> None:
    """One out-of-scope parent in a batch stops the whole batch."""
    victim_id = _create_task(app, 7, "victim-batch-parent")
    own_id = _create_task(app, 8, "own-batch")
    headers = _agent_headers(app, "session-batch-parent", [own_id])

    response = _client(app, 9).post(
        "/tasks/batch",
        headers=headers,
        json={
            "tasks": [
                {"title": "mine", "description": "mine", "role": "backend", "parent_task_id": own_id},
                {"title": "theirs", "description": "theirs", "role": "backend", "parent_task_id": victim_id},
            ]
        },
    )

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    assert app.state.store.count_subtasks(victim_id) == 0
    assert app.state.store.count_subtasks(own_id) == 0


def test_depends_on_is_not_scope_checked(app: FastAPI) -> None:
    """A dependency edge is stored on the new row and mutates nothing.

    Pinned deliberately: ``depends_on`` names an existing task but neither
    changes its state nor its reachability, so it is outside this gate. If
    that ever stops being true, this test is the place the decision is
    revisited.
    """
    other_id = _create_task(app, 10, "dependency-target")
    own_id = _create_task(app, 11, "own-depends")
    headers = _agent_headers(app, "session-depends", [own_id])
    before = _task(app, other_id)

    response = _client(app, 12).post(
        "/tasks",
        headers=headers,
        json={"title": "dependent", "description": "dependent", "role": "backend", "depends_on": [other_id]},
    )

    assert response.status_code == 201, response.text
    after = _task(app, other_id)
    assert after.status == before.status
    assert after.version == before.version


# ---------------------------------------------------------------------------
# Body-carried id outside /tasks/: POST /a2a/message
# ---------------------------------------------------------------------------


def test_a2a_message_denies_an_out_of_scope_task(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming another agent's task in an A2A message body is denied."""
    _grant_non_admin_permission(monkeypatch, "/a2a")
    victim_id = _create_task(app, 13, "victim-a2a")
    own_id = _create_task(app, 14, "own-a2a")
    headers = _agent_headers(app, "session-a2a", [own_id])
    before = _task(app, victim_id)

    response = _client(app, 15).post(
        "/a2a/message",
        headers=headers,
        json={"task_id": victim_id, "content": "injected", "sender": "probe", "recipient": "bernstein"},
    )

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    after = _task(app, victim_id)
    assert after.version == before.version


def test_a2a_message_allows_the_agents_own_task(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same route still works for the task the token was issued for."""
    _grant_non_admin_permission(monkeypatch, "/a2a")
    own_id = _create_task(app, 16, "own-a2a-allowed")
    headers = _agent_headers(app, "session-a2a-own", [own_id])

    response = _client(app, 17).post(
        "/a2a/message",
        headers=headers,
        json={"task_id": own_id, "content": "hello", "sender": "probe", "recipient": "bernstein"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["task_id"] == own_id


# ---------------------------------------------------------------------------
# Indirect resolution: an ACP run id resolves to a Bernstein task
# ---------------------------------------------------------------------------


def _create_acp_run(application: FastAPI, index: int, text: str) -> tuple[str, str]:
    """Create an ACP run with the operator credential.

    Returns:
        ``(run_id, bernstein_task_id)``.
    """
    response = _client(application, index).post(
        "/acp/v0/runs",
        headers=_operator_headers(),
        json={"input": text},
    )
    assert response.status_code in (200, 201), response.text
    payload = response.json()
    run_id = str(payload["run_id"] if "run_id" in payload else payload["id"])
    handler = application.state.acp_handler
    run = handler.get_run(run_id)
    assert run is not None and run.bernstein_task_id is not None, payload
    return run_id, str(run.bernstein_task_id)


def test_acp_run_cancel_denies_an_out_of_scope_task(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling through a run id is scoped like ``POST /tasks/{id}/cancel``."""
    _grant_non_admin_permission(monkeypatch, "/acp")
    run_id, victim_id = _create_acp_run(app, 18, "victim run")
    own_id = _create_task(app, 19, "own-acp")
    headers = _agent_headers(app, "session-acp", [own_id])

    response = _client(app, 20).delete(f"/acp/v0/runs/{run_id}", headers=headers)

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    assert _task(app, victim_id).status.value != "cancelled"


def test_acp_run_cancel_allows_the_agents_own_task(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token holding the underlying task may still cancel its own run."""
    _grant_non_admin_permission(monkeypatch, "/acp")
    run_id, own_id = _create_acp_run(app, 21, "own run")
    headers = _agent_headers(app, "session-acp-own", [own_id])

    response = _client(app, 22).delete(f"/acp/v0/runs/{run_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert _task(app, own_id).status.value == "cancelled"


# ---------------------------------------------------------------------------
# Indirect resolution: a plan id resolves to a batch of tasks
# ---------------------------------------------------------------------------


def _mark_planned(application: FastAPI, task_id: str) -> None:
    """Move a task into ``PLANNED``, the state a plan decision transitions.

    A plan decision only touches its ``PLANNED`` tasks, so a probe left in
    ``OPEN`` would prove nothing: the route would answer 200 having mutated
    nothing at all, and the denial assertion would not be measuring the scope
    rule.
    """
    store = application.state.store
    task = _task(application, task_id)
    store._index_remove(task)
    task.status = TaskStatus.PLANNED
    store._index_add(task)


def _save_plan(application: FastAPI, task_ids: list[str]) -> str:
    """Persist a plan over *task_ids* (moved to ``PLANNED``) and return its id."""
    from bernstein.core.security.plan_approval import create_plan

    for task_id in task_ids:
        _mark_planned(application, task_id)
    tasks = [_task(application, task_id) for task_id in task_ids]
    plan = create_plan("probe goal", tasks)
    application.state.plan_store.save_plan(plan)
    return str(plan.id)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_plan_decision_denies_out_of_scope_tasks(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """A plan decision is scoped to the tasks it is about to transition."""
    _grant_non_admin_permission(monkeypatch, "/plans")
    victim_id = _create_task(app, 23, f"victim-plan-{decision}")
    own_id = _create_task(app, 24, f"own-plan-{decision}")
    plan_id = _save_plan(app, [victim_id])
    headers = _agent_headers(app, f"session-plan-{decision}", [own_id])
    before = _task(app, victim_id)

    response = _client(app, 25).post(f"/plans/{plan_id}/{decision}", headers=headers, json={"reason": "probe"})

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    after = _task(app, victim_id)
    assert after.status == before.status == TaskStatus.PLANNED
    assert app.state.plan_store.get_plan(plan_id).status.value == "pending"


def test_plan_decision_allows_a_plan_over_the_agents_own_tasks(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan whose tasks are all in scope is still decidable."""
    _grant_non_admin_permission(monkeypatch, "/plans")
    own_id = _create_task(app, 26, "own-plan-allowed")
    plan_id = _save_plan(app, [own_id])
    headers = _agent_headers(app, "session-plan-own", [own_id])

    response = _client(app, 27).post(f"/plans/{plan_id}/approve", headers=headers, json={"reason": "probe"})

    assert response.status_code == 200, response.text
    assert response.json()["promoted_task_ids"] == [own_id]
    assert app.state.plan_store.get_plan(plan_id).status.value == "approved"


# ---------------------------------------------------------------------------
# Server-resolved ids: POST /cluster/steal
# ---------------------------------------------------------------------------


def _register_node(application: FastAPI, index: int, name: str, slots: int) -> str:
    response = _client(application, index).post(
        "/cluster/nodes",
        headers=_operator_headers(),
        json={
            "name": name,
            "url": f"http://{name}.invalid:8000",
            "capacity": {"max_agents": 8, "available_slots": slots, "active_agents": 0},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_cluster_steal_denies_an_out_of_scope_task(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steal reassigns tasks the caller never named; the ids are still bound.

    ``POST /cluster/steal`` reaches ``force_claim`` on tasks the policy picks,
    which is the mutation ``POST /tasks/{id}/force-claim`` performs behind the
    path gate.  The check runs on the ids the policy resolved, so a
    task-scoped token cannot reset a task it does not hold.
    """
    _grant_non_admin_permission(monkeypatch, "/cluster")
    victim_id = _create_task(app, 28, "victim-steal")
    own_id = _create_task(app, 29, "own-steal")
    claimed = _client(app, 30).post(f"/tasks/{victim_id}/claim", headers=_operator_headers())
    assert claimed.status_code == 200, claimed.text
    before = _task(app, victim_id)

    donor = _register_node(app, 31, "overloaded", slots=1)
    receiver = _register_node(app, 32, "idle", slots=8)
    headers = _agent_headers(app, "session-steal", [own_id])

    response = _client(app, 33).post(
        "/cluster/steal",
        headers=headers,
        json={"queue_depths": {donor: 10, receiver: 0}},
    )

    assert response.status_code == 403, response.text
    assert victim_id in response.json()["detail"]
    after = _task(app, victim_id)
    assert after.status == before.status
    assert after.version == before.version


def test_cluster_steal_is_unrestricted_for_an_unscoped_token(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manager token (``task_ids == []``) redistributes as before."""
    _grant_non_admin_permission(monkeypatch, "/cluster")
    victim_id = _create_task(app, 34, "manager-steal")
    claimed = _client(app, 35).post(f"/tasks/{victim_id}/claim", headers=_operator_headers())
    assert claimed.status_code == 200, claimed.text

    donor = _register_node(app, 36, "overloaded-mgr", slots=1)
    receiver = _register_node(app, 37, "idle-mgr", slots=8)
    headers = _agent_headers(app, "session-steal-manager", [])

    response = _client(app, 38).post(
        "/cluster/steal",
        headers=headers,
        json={"queue_depths": {donor: 10, receiver: 0}},
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Per-task paths outside /tasks/
# ---------------------------------------------------------------------------


def _per_task_route_templates(application: FastAPI) -> list[tuple[str, str]]:
    """Return ``(method, template)`` for every mutating route naming ``{task_id}``."""
    found: set[tuple[str, str]] = set()
    for route in application.routes:
        template = getattr(route, "path", "")
        if not template or not _TASK_ID_TEMPLATE_RE.search(template):
            continue
        for method in getattr(route, "methods", set()) or set():
            if method.upper() not in _READ_METHODS:
                found.add((method.upper(), template))
    return sorted(found)


def _fill(template: str, task_id: str) -> str:
    """Fill a path template, giving every non-task placeholder a literal."""
    filled = template.replace("{task_id}", task_id)
    return re.sub(r"\{[^{}]+\}", "probe", filled)


def test_enumeration_finds_per_task_routes_outside_the_tasks_prefix(app: FastAPI) -> None:
    """The enumeration below is non-empty, so its assertion can bite."""
    outside = [
        (method, template)
        for method, template in _per_task_route_templates(app)
        if not re.match(r"^(?:/api/v\d+)?/tasks/", template)
    ]

    assert len(outside) >= _MIN_NON_TASKS_PREFIX_ROUTES, outside


def test_every_registered_per_task_route_is_scope_checked(app: FastAPI) -> None:
    """Every mutating route whose path names a task denies an out-of-scope id.

    Derived from the route table rather than from a list of prefixes: a
    per-task route registered later under any prefix is covered the moment it
    exists, which is the property #3036 was filed about.
    """
    patterns = task_id_route_patterns(app)
    for method, template in _per_task_route_templates(app):
        path = _fill(template, _OUT_OF_SCOPE_TASK_ID)
        error = _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID], patterns)

        assert error is not None, f"{method} {path} is not scope-checked"
        assert _OUT_OF_SCOPE_TASK_ID in error, f"{method} {path}"


def test_every_registered_per_task_route_allows_the_agents_own_task(app: FastAPI) -> None:
    """The same routes are permitted when the path names the token's own task."""
    patterns = task_id_route_patterns(app)
    for method, template in _per_task_route_templates(app):
        path = _fill(template, _IN_SCOPE_TASK_ID)

        assert _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID], patterns) is None, f"{method} {path}"


def test_approvals_route_is_scope_checked(app: FastAPI) -> None:
    """``POST /approvals/{task_id}/approve`` is a per-task write outside ``/tasks/``."""
    patterns = task_id_route_patterns(app)

    for action in ("approve", "reject"):
        error = _check_agent_task_scope(
            f"/approvals/{_OUT_OF_SCOPE_TASK_ID}/{action}",
            [_IN_SCOPE_TASK_ID],
            patterns,
        )

        assert error is not None, action
        assert _OUT_OF_SCOPE_TASK_ID in error, action


def test_review_board_route_is_scope_checked(app: FastAPI) -> None:
    """The review board's per-task decision route carries ``{task_id}`` too."""
    patterns = task_id_route_patterns(app)

    error = _check_agent_task_scope(
        f"/dashboard/review-board/runs/run-1/tasks/{_OUT_OF_SCOPE_TASK_ID}/review",
        [_IN_SCOPE_TASK_ID],
        patterns,
    )

    assert error is not None
    assert _OUT_OF_SCOPE_TASK_ID in error


def test_a2a_task_ids_are_not_bernstein_task_ids(app: FastAPI) -> None:
    """``/a2a/tasks/{a2a_task_id}`` addresses the A2A namespace, not a task id.

    Pinned deliberately: the A2A id is a separate identifier that the handler
    links to a Bernstein task, so checking it against ``task_ids`` would
    compare values from two different namespaces.
    """
    patterns = task_id_route_patterns(app)

    assert (
        _check_agent_task_scope(f"/a2a/tasks/{_OUT_OF_SCOPE_TASK_ID}/artifacts", [_IN_SCOPE_TASK_ID], patterns) is None
    )


def test_route_patterns_are_memoised_on_the_app(app: FastAPI) -> None:
    """The matchers are compiled once, not per request."""
    assert task_id_route_patterns(app) is task_id_route_patterns(app)


# ---------------------------------------------------------------------------
# Credentials that must stay unaffected
# ---------------------------------------------------------------------------


def test_operator_credential_is_unaffected_by_the_indirect_checks(app: FastAPI) -> None:
    """The operator bearer is not an agent identity, so nothing is bound."""
    victim_id = _create_task(app, 39, "operator-parent")

    response = _client(app, 40).post(
        "/tasks",
        headers=_operator_headers(),
        json={"title": "child", "description": "child", "role": "backend", "parent_task_id": victim_id},
    )

    assert response.status_code == 201, response.text
    assert response.json()["parent_task_id"] == victim_id


def test_unscoped_agent_token_is_unaffected_by_the_indirect_checks(app: FastAPI) -> None:
    """A manager token (``task_ids == []``) stays unrestricted, as on the path gate."""
    victim_id = _create_task(app, 41, "manager-parent")
    headers = _agent_headers(app, "session-manager-indirect", [])

    response = _client(app, 42).post(
        "/tasks",
        headers=headers,
        json={"title": "child", "description": "child", "role": "backend", "parent_task_id": victim_id},
    )

    assert response.status_code == 201, response.text
