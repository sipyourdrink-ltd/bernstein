"""Unconfigured optional subsystems must not answer with a server-fault status.

The nightly Schemathesis deep sweep drives every documented operation
against a stock task server: no SSO provider, no plan store, no webhook
secrets, no telemetry receiver. Three route families answered that
*configuration* state with ``503 Service Unavailable``:

* the SSO routes, when ``app.state.auth_service`` is unset;
* the plan-approval routes, when ``app.state.plan_store`` is unset;
* every webhook receiver, when its shared secret or receiver is unset.

The reasoning for answering ``404`` instead lives in
``bernstein.core.routes._unconfigured``, next to the constant. The tests
below pin it at the response layer, at the OpenAPI-document layer, and
across the whole documented surface, so it cannot regress silently.

``test_no_documented_operation_answers_5xx_on_a_stock_server`` is the
broad one: it drives every documented operation once and fails on any
5xx. That is the property the nightly sweep exists to protect, asserted
here in seconds rather than in a 35-minute fuzz whose per-operation
ordering can hide a finding behind a rate limiter.
"""

from __future__ import annotations

import itertools
import os
import re
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


_peer_counter = itertools.count(1)


def _next_peer() -> tuple[str, int]:
    """A source address no earlier request in this module has used."""
    n = next(_peer_counter)
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}", 1234


def _fresh_client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, client=_next_peer())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A client presenting a source address no other test has used.

    The server rate limits per source address. A module that made every
    request from one address starts collecting 429s partway through and
    stops asserting the thing it was written to assert. That is not a
    hypothetical: it is how the nightly sweep lost these findings for a
    while, since a 429 is not a 5xx and the sweep only looks for 5xx.
    """
    return _fresh_client(app)


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
# Webhook receivers with no secret and no telemetry receiver configured
# ---------------------------------------------------------------------------

# Senders make this concrete: GitHub and GitLab redeliver a webhook on
# 5xx and stop on 4xx. Answering 503 asked them to redeliver forever
# against an endpoint that stays unconfigured until an operator acts.
#: The generic receiver validates its body before the configuration check,
#: so it needs a well-formed one to reach the refusal being asserted.
_WEBHOOK_CASES: list[tuple[str, dict[str, Any]]] = [
    ("/webhook", {"title": "probe", "description": "probe"}),
    ("/webhooks/github", {}),
    ("/webhooks/gitlab", {}),
    ("/webhooks/telemetry/sentry/", {}),
    ("/webhooks/telemetry/datadog/", {}),
    ("/webhooks/telemetry/loki/", {}),
    ("/webhooks/telemetry/gha_failure/", {}),
    ("/webhooks/telemetry/custom_jsonl/", {}),
]


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize(("path", "body"), _WEBHOOK_CASES)
def test_unconfigured_webhook_receiver_answers_404(
    client: TestClient, prefix: str, path: str, body: dict[str, Any]
) -> None:
    """An unconfigured receiver refuses with a 404, never a 5xx."""
    resp = client.post(f"{prefix}{path}", json=body, headers={"X-GitHub-Event": "issues"})
    assert resp.status_code == 404, f"POST {prefix}{path} -> {resp.status_code}: {resp.text[:200]}"
    assert "not configured" in resp.text.lower()


# ---------------------------------------------------------------------------
# Malformed path parameters
# ---------------------------------------------------------------------------


def test_unsafe_receipt_id_is_not_found_rather_than_a_crash(client: TestClient) -> None:
    """A rejected id addresses no receipt; it must not escape as a 500.

    ``read_receipt`` raises for an id containing an unsafe character, and
    nothing caught it, so the crash guard turned a malformed request into
    ``500 Internal Server Error``. The sibling ``GET /sla/{contract_id}``
    already had the guard; this route did not.
    """
    resp = client.get("/sla/receipts/%5C%22/verify")
    assert resp.status_code == 404, resp.text[:200]


# ---------------------------------------------------------------------------
# The whole documented surface, not just the families named above
# ---------------------------------------------------------------------------


def test_no_documented_operation_answers_5xx_on_a_stock_server(app: FastAPI, client: TestClient) -> None:
    """Nothing a stock deployment publishes may answer 5xx to one plain call.

    The nightly sweep asserts this with 50 generated examples per
    operation over 35 minutes, and its ordering lets a rate limiter answer
    429 before a route's real status is ever observed, which is how these
    findings stayed hidden. One deterministic pass, each request from its
    own source address, costs seconds and cannot be masked that way.
    """
    spec = client.get("/openapi.json").json()
    offenders: list[str] = []
    for path, operations in sorted(spec["paths"].items()):
        concrete = re.sub(r"\{[^{}]+\}", "probe-1", path)
        for method in operations:
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            resp = _fresh_client(app).request(method.upper(), concrete, json=None)
            if resp.status_code >= 500:
                offenders.append(f"{method.upper()} {concrete} -> {resp.status_code}: {resp.text[:120]}")
    assert not offenders, "5xx from a stock deployment:\n" + "\n".join(offenders)


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
