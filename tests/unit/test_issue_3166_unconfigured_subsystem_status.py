"""Unconfigured optional subsystems must not answer with a server-fault status.

The nightly Schemathesis deep sweep drives every documented operation
against a task server built with no SSO provider and no plan store. Two
route families answered that *configuration* state with ``503 Service
Unavailable``:

* the SSO routes, when ``app.state.auth_service`` is unset;
* the plan-approval routes, when ``app.state.plan_store`` is unset.

``503`` is wrong on both counts it asserts. Nothing on the server failed,
and the condition is not transient: it holds for the lifetime of the
deployment until an operator changes the configuration. Every stock HTTP
client treats ``503`` as retryable, and this repo's own retry policy
lists it in ``retryable_status_codes``, so a caller burns its whole
backoff budget on a state that will never change on its own.

``404 Not Found`` is the accurate answer: this deployment serves no such
resource. It is permanent, cacheable, and terminal for the client, and it
keeps the response in the 4xx class where "the request cannot be
satisfied as addressed" belongs.

The tests below pin that contract at both the response layer and the
OpenAPI-document layer so it cannot regress silently.
"""

from __future__ import annotations

import os
from typing import Any

# Auth must be disabled before the app is imported or built, otherwise the
# auth middleware short-circuits every request with 401 before the route
# handler runs. This mirrors the documented opt-out used by the
# Schemathesis contract suite.
os.environ.setdefault("BERNSTEIN_AUTH_DISABLED", "1")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.server.server_app import create_app


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """A task server with neither SSO nor plan mode configured."""
    built = create_app(auth_token=None, readonly=False, cluster_config=None)
    assert getattr(built.state, "auth_service", None) is None
    assert getattr(built.state, "plan_store", None) is None
    return built


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# SSO routes with no auth service configured
# ---------------------------------------------------------------------------

# (method, path, request kwargs) for every SSO operation that reaches the
# "is SSO configured?" guard. Routes that check the caller identity first
# (`GET /auth/users`, `PUT /auth/group-mappings`) answer 401 before the
# guard runs and are therefore out of scope here.
_SSO_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/auth/login", {"params": {"provider": "oidc"}}),
    ("GET", "/auth/login", {"params": {"provider": "saml"}}),
    ("GET", "/auth/oidc/callback", {}),
    ("GET", "/auth/saml/metadata", {}),
    ("POST", "/auth/saml/acs", {"data": {}}),
    ("GET", "/auth/group-mappings", {}),
    ("POST", "/auth/cli/device", {"json": {}}),
    ("POST", "/auth/cli/token", {"json": {"device_code": "x"}}),
    ("POST", "/auth/cli/authorize", {"json": {"user_code": "x"}}),
]


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(("method", "path", "kwargs"), _SSO_CASES, ids=lambda v: str(v))
def test_unconfigured_sso_answers_404(
    client: TestClient, prefix: str, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """Unconfigured SSO is a 404, never a 5xx."""
    resp = client.request(method, f"{prefix}{path}", **kwargs)
    assert resp.status_code == 404, f"{method} {prefix}{path} -> {resp.status_code}: {resp.text[:200]}"
    assert "not configured" in resp.text


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(("method", "path", "kwargs"), _SSO_CASES, ids=lambda v: str(v))
def test_unconfigured_sso_is_not_a_server_error(
    client: TestClient, prefix: str, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """The property the nightly deep sweep actually asserts."""
    resp = client.request(method, f"{prefix}{path}", **kwargs)
    assert resp.status_code < 500, f"{method} {prefix}{path} -> {resp.status_code}: {resp.text[:200]}"


# ---------------------------------------------------------------------------
# Plan routes with no plan store configured
# ---------------------------------------------------------------------------

# The three requests below are the ones the nightly sweep reported
# alongside the SSO paths. They share the SSO root cause: an unconfigured
# optional subsystem reported as a server fault.
_PLAN_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/plans", {}),
    ("GET", "/plans", {"params": [("status", "null"), ("status", "null")]}),
    ("GET", "/plans/0", {}),
    ("POST", "/plans/0/approve", {"content": "null", "headers": {"Content-Type": "application/json"}}),
    ("POST", "/plans/0/reject", {"content": "null", "headers": {"Content-Type": "application/json"}}),
]


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(("method", "path", "kwargs"), _PLAN_CASES, ids=lambda v: str(v))
def test_unconfigured_plan_store_answers_404(
    client: TestClient, prefix: str, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """Plan mode disabled is a 404, never a 5xx."""
    resp = client.request(method, f"{prefix}{path}", **kwargs)
    assert resp.status_code == 404, f"{method} {prefix}{path} -> {resp.status_code}: {resp.text[:200]}"
    assert "plan mode" in resp.text.lower()


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(("method", "path", "kwargs"), _PLAN_CASES, ids=lambda v: str(v))
def test_unconfigured_plan_store_is_not_a_server_error(
    client: TestClient, prefix: str, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    resp = client.request(method, f"{prefix}{path}", **kwargs)
    assert resp.status_code < 500, f"{method} {prefix}{path} -> {resp.status_code}: {resp.text[:200]}"


# ---------------------------------------------------------------------------
# The OpenAPI document must not advertise the old contract either
# ---------------------------------------------------------------------------


def test_openapi_documents_no_5xx_for_auth_or_plan_routes(client: TestClient) -> None:
    """No ``/auth`` or ``/plans`` operation may document a 5xx response.

    Schemathesis derives its sweep from this document. Leaving a ``503``
    in it would keep telling clients that an unconfigured deployment is a
    transient server fault even after the handlers stopped saying so.
    """
    spec = client.get("/openapi.json").json()
    offenders: list[str] = []
    for path, operations in spec["paths"].items():
        if "/auth/" not in path and "/plans" not in path:
            continue
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for code in operation.get("responses", {}):
                if str(code).startswith("5"):
                    offenders.append(f"{method.upper()} {path} -> {code}")
    assert not offenders, f"5xx documented for configuration states: {offenders}"
