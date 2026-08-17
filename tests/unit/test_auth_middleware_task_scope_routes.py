"""Route-table-derived coverage for per-agent task scoping (#3036).

``_check_agent_task_scope`` binds an agent identity to the tasks its token
was issued for.  The guarded set used to be a hand-maintained alternation
of action names, so every task route added afterwards silently escaped the
check.  These tests derive the expectations from the *registered* route
table instead of a literal list: a newly added ``/tasks/{task_id}/...``
mutation is covered automatically, and a newly added collection route under
``/tasks/`` fails the pinning test until it is exempted deliberately.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.auth_middleware import (
    _TASK_ID_PATH_RE,
    TASK_BODY_SCOPED_SEGMENTS,
    TASK_COLLECTION_SEGMENTS,
    _check_agent_task_scope,
    task_collection_route_patterns,
)
from fastapi.testclient import TestClient

from bernstein.core.routes.route_table import iter_route_paths

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Any path template that addresses a single task by id, on the root mount or
# on a versioned mirror (``/api/v1/...``).
_TASK_ID_ROUTE_RE = re.compile(r"^(?:/api/v\d+)?/tasks/\{task_id\}(?:/|$)")

# A literal segment directly under ``/tasks/`` - a collection route, not a
# task id (e.g. ``/tasks/self-create``).
_TASK_COLLECTION_ROUTE_RE = re.compile(r"^(?:/api/v\d+)?/tasks/(?P<segment>[^/{}]+)(?:/|$)")

# One ``{name}`` or ``{name:convertor}`` placeholder in a path template.
_TEMPLATE_PARAM_RE = re.compile(r"\{[^{}]+\}")

_IN_SCOPE_TASK_ID = "task-mine"
_OUT_OF_SCOPE_TASK_ID = "task-not-mine"

# Sanity floor: the enumeration must actually find the task surface. Without
# it a refactor that stops matching route templates would make every
# enumerating assertion pass vacuously.
_MIN_EXPECTED_MUTATING_ROUTES = 20


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """Build the real application so its route table drives the assertions."""
    from bernstein.core.server import create_app

    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


def _mutating_task_id_routes(application: FastAPI) -> list[tuple[str, str]]:
    """Return ``(method, path_template)`` for every mutating per-task route."""
    found: set[tuple[str, str]] = set()
    for template, route in iter_route_paths(application):
        if not _TASK_ID_ROUTE_RE.match(template):
            continue
        for method in getattr(route, "methods", set()) or set():
            if method.upper() not in _READ_METHODS:
                found.add((method.upper(), template))
    return sorted(found)


def _task_collection_segments(application: FastAPI) -> set[str]:
    """Return the literal (non task-id) first segments under ``/tasks/``."""
    segments: set[str] = set()
    for template, _route in iter_route_paths(application):
        match = _TASK_COLLECTION_ROUTE_RE.match(template)
        if match is not None:
            segments.add(match.group("segment"))
    return segments


def _task_collection_routes(application: FastAPI) -> list[tuple[str, str]]:
    """Return ``(method, path_template)`` for every registered collection route."""
    found: set[tuple[str, str]] = set()
    for template, route in iter_route_paths(application):
        if _TASK_COLLECTION_ROUTE_RE.match(template) is None:
            continue
        for method in getattr(route, "methods", set()) or set():
            found.add((method.upper(), template))
    return sorted(found)


def _fill_collection_template(template: str) -> str:
    """Substitute a placeholder value for any parameter in a collection template."""
    return _TEMPLATE_PARAM_RE.sub("backend", template)


def test_enumeration_finds_the_task_surface(app: FastAPI) -> None:
    """The route enumeration is non-empty, so the assertions below can bite."""
    routes = _mutating_task_id_routes(app)

    assert len(routes) >= _MIN_EXPECTED_MUTATING_ROUTES, routes


def test_every_mutating_task_route_is_scope_checked(app: FastAPI) -> None:
    """Every mutating per-task route reports an out-of-scope task id."""
    for method, template in _mutating_task_id_routes(app):
        path = template.replace("{task_id}", _OUT_OF_SCOPE_TASK_ID)
        error = _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID])

        assert error is not None, f"{method} {path} is not scope-checked"
        assert _OUT_OF_SCOPE_TASK_ID in error, f"{method} {path}"


def test_every_mutating_task_route_denies_out_of_scope_identity(app: FastAPI) -> None:
    """A token scoped to task A is rejected on every mutating task-B route.

    End-to-end through the real middleware stack: the handler must never run
    for a task the token was not issued for.
    """
    store: Any = app.state.identity_store
    _, token = store.create_identity("session-scope-probe", "backend", task_ids=[_IN_SCOPE_TASK_ID])
    headers = {"Authorization": f"Bearer {token}"}

    routes = _mutating_task_id_routes(app)
    for index, (method, template) in enumerate(routes):
        path = template.replace("{task_id}", _OUT_OF_SCOPE_TASK_ID)
        # A fresh peer address per request: the write rate limiter allows 30
        # requests/minute per client and would answer 429 long before the
        # enumeration finished, masking the authorization result.
        client = TestClient(app, client=(f"10.{index // 256}.{index % 256}.1", 40000 + index))
        response = client.request(method, path, headers=headers, json={})

        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


def test_every_mutating_task_route_allows_in_scope_identity(app: FastAPI) -> None:
    """The same routes are permitted when the path addresses the token's own task."""
    for method, template in _mutating_task_id_routes(app):
        path = template.replace("{task_id}", _IN_SCOPE_TASK_ID)

        assert _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID]) is None, f"{method} {path}"


def test_task_collection_segments_are_pinned_to_the_route_table(app: FastAPI) -> None:
    """Exempt segments match the collection routes actually registered.

    Adding a new ``/tasks/<literal>`` route fails here until the segment is
    added to ``TASK_COLLECTION_SEGMENTS`` deliberately - the exemption can
    never be acquired by accident.
    """
    assert _task_collection_segments(app) == set(TASK_COLLECTION_SEGMENTS)


def test_collection_routes_are_not_treated_as_task_ids(app: FastAPI) -> None:
    """Collection routes stay reachable for a task-scoped agent."""
    collection = task_collection_route_patterns(app)
    for method, template in _task_collection_routes(app):
        path = _fill_collection_template(template)

        assert _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID], (), collection, method) is None, path


def test_a_task_id_equal_to_a_collection_segment_is_still_scope_checked(app: FastAPI) -> None:
    """A collection segment name used as a task id does not borrow the exemption.

    The exemption belongs to the registered collection ROUTE, not to the text
    of the segment.  Keying it on the text alone would leave
    ``POST /tasks/archive/cancel`` ungated for a task whose id is
    ``archive``, while ``POST /tasks/<any other id>/cancel`` was denied.
    """
    collection = task_collection_route_patterns(app)
    for segment in sorted(TASK_COLLECTION_SEGMENTS):
        for path in (f"/tasks/{segment}/cancel", f"/api/v1/tasks/{segment}/complete"):
            error = _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID], (), collection, "POST")

            assert error is not None, f"{path} is not scope-checked"
            assert segment in error, path


def test_a_collection_route_is_exempt_only_for_the_methods_it_serves(app: FastAPI) -> None:
    """A path a collection template matches under another method stays gated.

    ``POST /tasks/next/cancel`` matches the GET-only ``/tasks/next/{role}``
    on path alone, but the router dispatches it to
    ``POST /tasks/{task_id}/cancel`` with the id ``next``, so the gate has to
    follow the router rather than the path match.
    """
    collection = task_collection_route_patterns(app)

    assert _check_agent_task_scope("/tasks/next/backend", [_IN_SCOPE_TASK_ID], (), collection, "GET") is None

    error = _check_agent_task_scope("/tasks/next/cancel", [_IN_SCOPE_TASK_ID], (), collection, "POST")

    assert error is not None
    assert "next" in error


def test_versioned_mirror_is_scope_checked() -> None:
    """The ``/api/v1`` mirror of a task route is gated like the root mount."""
    error = _check_agent_task_scope(f"/api/v1/tasks/{_OUT_OF_SCOPE_TASK_ID}/complete", [_IN_SCOPE_TASK_ID])

    assert error is not None
    assert _OUT_OF_SCOPE_TASK_ID in error


def test_dead_steal_alternative_is_gone() -> None:
    """``/tasks/{id}/steal`` never existed; the pattern no longer names it."""
    assert "steal" not in _TASK_ID_PATH_RE.pattern


def test_cluster_steal_is_not_a_task_scoped_path() -> None:
    """The real steal route (``POST /cluster/steal``) is outside this gate."""
    assert _check_agent_task_scope("/cluster/steal", [_IN_SCOPE_TASK_ID]) is None


def test_non_task_paths_are_unaffected() -> None:
    """Bulletin/status/task-collection paths never trigger a scope error."""
    for path in ("/bulletin", "/status", "/tasks", "/tasks/", "/agents/a1/kill", "/api/v1/tasks"):
        assert _check_agent_task_scope(path, [_IN_SCOPE_TASK_ID]) is None, path


# ---------------------------------------------------------------------------
# Collection routes that name existing tasks in the request body
# ---------------------------------------------------------------------------
#
# The path-level gate cannot see a request body, so exempting these segments
# from it is only safe while their handlers apply the same rule to the ids
# they act on.  Without that, a token scoped to task A is denied on
# ``POST /tasks/B/cancel`` and permitted to cancel the same task B through
# ``POST /tasks/batch-ops``.

_OPERATOR_TOKEN = "operator-token-for-scope-tests"

# One probe body per body-scoped segment, with ``{task_id}`` standing in for
# the task the request names.  ``test_body_scoped_segments_all_have_a_probe``
# pins this mapping to the segment set, so a newly exempted segment fails
# until its handler is covered here.
_BODY_SCOPED_PROBES: dict[str, dict[str, Any]] = {
    "batch-ops": {"action": "cancel", "ids": ["{task_id}"]},
    "claim-batch": {"task_ids": ["{task_id}"], "agent_id": "probe-agent"},
    "self-create": {
        "title": "probe subtask",
        "description": "probe subtask",
        "role": "backend",
        "parent_task_id": "{task_id}",
    },
}

# The response field each probe reports its successfully handled ids under, so
# a permitted request is asserted to have actually run rather than merely to
# have avoided a 403.
_BODY_SCOPED_SUCCESS_FIELD: dict[str, str] = {
    "batch-ops": "succeeded",
    "claim-batch": "claimed",
    "self-create": "parent_task_id",
}

# Status code a permitted probe answers with. ``self-create`` is a creation
# route and answers 201; the rest report per-id outcomes with 200.
_BODY_SCOPED_OK_STATUS: dict[str, int] = {"self-create": 201}


@pytest.fixture
def authed_app(tmp_path: Path) -> FastAPI:
    """The real application with an operator bearer token for fixture setup."""
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
    )


def _client(application: FastAPI, index: int) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    return TestClient(application, client=(f"10.20.{index // 256}.{index % 256}", 41000 + index))


def _create_task(application: FastAPI, index: int, title: str) -> str:
    """Create a task with the operator credential and return its server-assigned id."""
    response = _client(application, index).post(
        "/tasks",
        headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"},
        json={"title": title, "description": title, "role": "backend"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _probe_body(segment: str, task_id: str) -> dict[str, Any]:
    """Fill a probe body template with a concrete task id.

    The placeholder is substituted whether the template carries it as a bare
    value (``parent_task_id``) or inside a list (``ids``, ``task_ids``).
    """

    def _fill(value: Any) -> Any:
        if isinstance(value, list):
            return [_fill(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
        return task_id if value == "{task_id}" else value

    return {key: _fill(value) for key, value in _BODY_SCOPED_PROBES[segment].items()}


def test_body_scoped_segments_all_have_a_probe() -> None:
    """Every body-scoped segment is exercised by the tests below."""
    assert set(_BODY_SCOPED_PROBES) == set(TASK_BODY_SCOPED_SEGMENTS)
    assert set(_BODY_SCOPED_SUCCESS_FIELD) == set(TASK_BODY_SCOPED_SEGMENTS)
    assert set(_BODY_SCOPED_OK_STATUS) <= set(TASK_BODY_SCOPED_SEGMENTS)


def test_body_scoped_segments_are_exempt_from_the_path_gate() -> None:
    """The body-scoped segments are a subset of the path-level exemptions."""
    assert TASK_BODY_SCOPED_SEGMENTS <= TASK_COLLECTION_SEGMENTS


def test_body_scoped_routes_deny_an_out_of_scope_task_id(authed_app: FastAPI) -> None:
    """Naming another agent's task in the body is denied, and nothing is mutated."""
    victim_id = _create_task(authed_app, 0, "victim")
    own_id = _create_task(authed_app, 1, "own")
    store: Any = authed_app.state.identity_store
    _, token = store.create_identity("session-body-scope", "backend", task_ids=[own_id])
    headers = {"Authorization": f"Bearer {token}"}
    before = authed_app.state.store.get_task(victim_id)
    assert before is not None

    for index, segment in enumerate(sorted(TASK_BODY_SCOPED_SEGMENTS)):
        response = _client(authed_app, 10 + index).post(
            f"/tasks/{segment}",
            headers=headers,
            json=_probe_body(segment, victim_id),
        )

        assert response.status_code == 403, f"/tasks/{segment} -> {response.status_code} {response.text}"
        assert victim_id in response.json()["detail"]
        after = authed_app.state.store.get_task(victim_id)
        assert after is not None
        assert after.status == before.status, f"/tasks/{segment} mutated an out-of-scope task"
        assert after.version == before.version, f"/tasks/{segment} mutated an out-of-scope task"


def test_body_scoped_routes_allow_the_agents_own_task(authed_app: FastAPI) -> None:
    """The same routes still work when the body names the token's own task."""
    store: Any = authed_app.state.identity_store

    for index, segment in enumerate(sorted(TASK_BODY_SCOPED_SEGMENTS)):
        own_id = _create_task(authed_app, 20 + index, f"own-{segment}")
        _, token = store.create_identity(f"session-own-{segment}", "backend", task_ids=[own_id])
        response = _client(authed_app, 30 + index).post(
            f"/tasks/{segment}",
            headers={"Authorization": f"Bearer {token}"},
            json=_probe_body(segment, own_id),
        )

        expected = _BODY_SCOPED_OK_STATUS.get(segment, 200)
        assert response.status_code == expected, f"/tasks/{segment} -> {response.status_code} {response.text}"
        assert own_id in response.json()[_BODY_SCOPED_SUCCESS_FIELD[segment]], f"/tasks/{segment} {response.text}"


def test_body_scoped_routes_allow_an_unscoped_manager_token(authed_app: FastAPI) -> None:
    """A token with ``task_ids == []`` stays unrestricted, as on the path gate."""
    store: Any = authed_app.state.identity_store
    _, token = store.create_identity("session-manager", "backend", task_ids=[])

    for index, segment in enumerate(sorted(TASK_BODY_SCOPED_SEGMENTS)):
        # A task the manager token was never scoped to, fresh per segment so
        # one probe cannot leave the next one nothing to act on.
        target_id = _create_task(authed_app, 40 + index, f"manager-target-{segment}")
        response = _client(authed_app, 50 + index).post(
            f"/tasks/{segment}",
            headers={"Authorization": f"Bearer {token}"},
            json=_probe_body(segment, target_id),
        )

        expected = _BODY_SCOPED_OK_STATUS.get(segment, 200)
        assert response.status_code == expected, f"/tasks/{segment} -> {response.status_code} {response.text}"
        assert target_id in response.json()[_BODY_SCOPED_SUCCESS_FIELD[segment]], f"/tasks/{segment} {response.text}"


def test_batch_ops_scope_check_sees_the_normalised_id(authed_app: FastAPI) -> None:
    """The check runs on the id the handler acts on, not on the raw input.

    ``batch-ops`` strips non-word characters before touching the store, so a
    check against the raw string would let ``<victim>!`` through and then
    cancel ``<victim>``.
    """
    victim_id = _create_task(authed_app, 60, "victim-normalised")
    own_id = _create_task(authed_app, 61, "own-normalised")
    store: Any = authed_app.state.identity_store
    _, token = store.create_identity("session-normalised", "backend", task_ids=[own_id])

    response = _client(authed_app, 62).post(
        "/tasks/batch-ops",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "cancel", "ids": [f"{victim_id}!!"]},
    )

    assert response.status_code == 403, response.text
    victim = authed_app.state.store.get_task(victim_id)
    assert victim is not None
    assert victim.status.value != "cancelled"


def test_body_scoped_routes_are_unaffected_for_the_operator_credential(authed_app: FastAPI) -> None:
    """The operator bearer token is not an agent identity and stays unrestricted."""
    victim_id = _create_task(authed_app, 70, "operator-target")

    response = _client(authed_app, 71).post(
        "/tasks/batch-ops",
        headers={"Authorization": f"Bearer {_OPERATOR_TOKEN}"},
        json={"action": "cancel", "ids": [victim_id]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["succeeded"] == [victim_id]
