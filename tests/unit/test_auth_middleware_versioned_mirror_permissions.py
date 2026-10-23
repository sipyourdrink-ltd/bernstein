"""A versioned mirror requires exactly what its root route requires.

The app registers most routes twice: at the root and under ``/api/v<n>``.
Both serve the same handler, so they must declare the same permission.
``_get_required_permission`` matched its prefixes against the raw path, so a
mirror did not match the prefix its root route did and fell through to a
default instead: reads dropped to the ``status:read`` floor (a worker token
read ``/api/v1/agents/{id}/logs`` without ``agents:read``) and
``POST /api/v1/drain/cancel`` hit the ``/cancel`` substring heuristic and
needed only ``tasks:write`` while ``POST /drain/cancel`` needed
``admin:manage``.

The invariant is pinned over the whole registered route table, so a mirror
added later is covered without editing this file.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.routes.route_table import iter_route_paths
from bernstein.core.security.auth_middleware import _get_required_permission

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_OPERATOR_TOKEN = "operator-token-for-versioned-mirror-tests"

_VERSION_PREFIX_RE = re.compile(r"^/api/v\d+(?=/|$)")
_TEMPLATE_PARAM_RE = re.compile(r"\{(?P<name>[^{}:]+)(?::(?P<convertor>[^{}]+))?\}")

# Sanity floor: the app mirrors well over a hundred root routes under
# ``/api/v1``.  An enumeration that found only a handful would make the
# invariant below pass vacuously.
_MIN_MIRRORED_ROUTE_METHODS = 100


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application, with an operator bearer token for fixture setup."""
    from bernstein.core.server import create_app

    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )


def _concrete(template: str) -> str:
    """Fill every placeholder so the template becomes a requestable path."""
    return _TEMPLATE_PARAM_RE.sub(
        lambda m: "seg-a/seg-b" if m.group("convertor") == "path" else "probe-id",
        template,
    )


def _mirrored_route_methods(application: FastAPI) -> list[tuple[str, str, str]]:
    """Return ``(method, root_template, mirror_template)`` for every mirrored route."""
    methods: dict[str, set[str]] = {}
    for template, route in iter_route_paths(application):
        methods.setdefault(template, set()).update(m.upper() for m in getattr(route, "methods", None) or ())
    found: list[tuple[str, str, str]] = []
    for mirror, mirror_methods in methods.items():
        if not _VERSION_PREFIX_RE.match(mirror):
            continue
        root = _VERSION_PREFIX_RE.sub("", mirror, count=1) or "/"
        for method in sorted(mirror_methods & methods.get(root, set())):
            found.append((method, root, mirror))
    return sorted(found)


def test_every_versioned_mirror_requires_the_root_permission(app: FastAPI) -> None:
    mirrored = _mirrored_route_methods(app)
    assert len(mirrored) >= _MIN_MIRRORED_ROUTE_METHODS, len(mirrored)

    diverging = [
        (
            method,
            root,
            _get_required_permission(_concrete(root), method),
            _get_required_permission(_concrete(mirror), method),
        )
        for method, root, mirror in mirrored
        if _get_required_permission(_concrete(root), method) != _get_required_permission(_concrete(mirror), method)
    ]

    assert diverging == []


@pytest.mark.parametrize("version", ["/api/v1", "/api/v2", "/api/v37"])
def test_mirror_normalisation_is_not_tied_to_one_version(version: str) -> None:
    assert _get_required_permission(f"{version}/drain/cancel", "POST") == "admin:manage"
    assert _get_required_permission(f"{version}/agents/s-1/logs", "GET") == "agents:read"


# ---------------------------------------------------------------------------
# Live requests
# ---------------------------------------------------------------------------


def _client(application: FastAPI, index: int) -> TestClient:
    """A client with a distinct peer address so the write rate limiter allows it."""
    return TestClient(application, client=(f"10.42.{index // 256}.{index % 256}", 44000 + index))


def _agent_headers(application: FastAPI, session: str, task_ids: list[str]) -> dict[str, str]:
    identity_store: Any = application.state.identity_store
    _, token = identity_store.create_identity(session, "backend", task_ids=task_ids)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", ["/drain/cancel", "/api/v1/drain/cancel"])
def test_worker_token_cannot_cancel_a_drain_on_either_mount(app: FastAPI, path: str) -> None:
    operator = {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}
    assert _client(app, 1).post("/drain", headers=operator).status_code == 200

    response = _client(app, 2).post(path, headers=_agent_headers(app, "backend-w", ["task-mine"]))

    assert response.status_code == 403, response.text
    assert app.state.draining is True


@pytest.mark.parametrize("path", ["/drain/cancel", "/api/v1/drain/cancel"])
def test_operator_can_still_cancel_a_drain_on_either_mount(app: FastAPI, path: str) -> None:
    operator = {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}
    assert _client(app, 3).post("/drain", headers=operator).status_code == 200

    response = _client(app, 4).post(path, headers=operator)

    assert response.status_code == 200, response.text
    assert app.state.draining is False


def test_worker_token_cannot_read_agent_logs_through_the_mirror(app: FastAPI) -> None:
    headers = _agent_headers(app, "backend-w", ["task-mine"])

    root = _client(app, 5).get("/agents/backend-victim01/logs", headers=headers)
    mirror = _client(app, 6).get("/api/v1/agents/backend-victim01/logs", headers=headers)

    assert root.status_code == 403, root.text
    assert mirror.status_code == 403, mirror.text
