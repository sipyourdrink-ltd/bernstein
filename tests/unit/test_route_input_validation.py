"""Input-validation hardening for task-server REST routes.

Regression coverage for a robustness gap surfaced by the nightly
Schemathesis deep sweep: several route handlers read request inputs
without FastAPI-typed validation, so a malformed value raised an
unhandled exception that the outermost crash guard turned into a
``500 Internal server error`` instead of a proper ``4xx``.

Correct behaviour for already-malformed input is ``422`` (or a clean
``400``), never ``500``. Each test below pins one endpoint's contract:

* ``/identities`` - invalid ``status`` enum value.
* ``/broadcast`` - non-object / malformed JSON body.
* ``/events`` + ``/events/cost`` - the ``/api/v1``-prefixed SSE routes
  must be recognised as SSE so the crash guard passes them through the
  same way it already does for the unprefixed aliases (which the sweep
  did *not* flag).
* ``/auth/login`` - invalid ``provider`` value.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os

# Auth must be disabled before the app is imported/built - otherwise the
# auth middleware short-circuits every request with 401 before parameter
# validation runs. This mirrors the documented opt-out used by the
# Schemathesis contract suite.
os.environ.setdefault("BERNSTEIN_AUTH_DISABLED", "1")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.server.server_app import create_app
from bernstein.core.server.server_middleware import _is_sse_request


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(auth_token=None, readonly=False, cluster_config=None)


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /identities - status is a closed enum (active/suspended/revoked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/identities", "/api/v1/identities"])
@pytest.mark.parametrize("bad_status", ["null", "bogus", "ACTIVE", ""])
def test_identities_invalid_status_returns_422(client: TestClient, path: str, bad_status: str) -> None:
    resp = client.get(path, params={"status": bad_status})
    assert resp.status_code == 422, resp.text


def test_identities_exact_schemathesis_repro(client: TestClient) -> None:
    """The exact failing case from the nightly sweep.

    ``status=null`` plus an unknown query property must be a clean 422,
    not a crash-guard 500.
    """
    resp = client.get(
        "/api/v1/identities",
        params={"status": "null", "x-schemathesis-unknown-property": "42"},
    )
    assert resp.status_code == 422, resp.text
    assert "crash guard" not in resp.text


@pytest.mark.parametrize("path", ["/identities", "/api/v1/identities"])
@pytest.mark.parametrize("good_status", ["active", "suspended", "revoked"])
def test_identities_valid_status_still_works(client: TestClient, path: str, good_status: str) -> None:
    resp = client.get(path, params={"status": good_status})
    assert resp.status_code == 200, resp.text
    assert "identities" in resp.json()


def test_identities_no_status_still_works(client: TestClient) -> None:
    resp = client.get("/identities")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /broadcast - body must be a JSON object with a "message" field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/broadcast", "/api/v1/broadcast"])
@pytest.mark.parametrize("body", [42, "hello", ["a", "b"], True])
def test_broadcast_non_object_body_returns_422(client: TestClient, path: str, body: object) -> None:
    resp = client.post(path, json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("path", ["/broadcast", "/api/v1/broadcast"])
def test_broadcast_malformed_json_returns_422(client: TestClient, path: str) -> None:
    resp = client.post(path, content=b"not-json", headers={"content-type": "application/json"})
    assert resp.status_code == 422, resp.text


def test_broadcast_valid_body_still_works(client: TestClient) -> None:
    resp = client.post("/api/v1/broadcast", json={"message": "hello agents"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "broadcast_sent"


def test_broadcast_empty_message_returns_400(client: TestClient) -> None:
    """An empty message is a domain error (400), preserved from before."""
    resp = client.post("/broadcast", json={"message": ""})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# SSE routes - the /api/v1-prefixed variants must be detected as SSE so
# the crash guard passes them through instead of wrapping the stream.
# ---------------------------------------------------------------------------


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    def __init__(self, path: str, accept: str = "") -> None:
        self.url = _FakeURL(path)
        self.headers = {"accept": accept}


@pytest.mark.parametrize(
    "path",
    ["/events", "/events/cost", "/api/v1/events", "/api/v1/events/cost"],
)
def test_sse_paths_detected(path: str) -> None:
    assert _is_sse_request(_FakeRequest(path)) is True  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    [
        "/tasks",
        "/webhooks/slack/events",
        "/api/v1/webhooks/slack/events",
        "/identities",
    ],
)
def test_non_sse_paths_not_detected(path: str) -> None:
    """Non-SSE routes must stay wrapped by the crash guard.

    ``/webhooks/slack/events`` also ends in ``/events`` but is a POST
    webhook, not a stream - it must NOT be treated as SSE.
    """
    assert _is_sse_request(_FakeRequest(path)) is False  # type: ignore[arg-type]


# End-to-end proof that the crash guard passes the versioned SSE routes
# through (mid-stream exceptions propagate instead of becoming a JSON
# 500) lives in tests/unit/test_crash_guard_middleware.py, which owns
# the middleware's streaming-behaviour harness.


# ---------------------------------------------------------------------------
# GET /auth/login - provider is a closed enum (oidc/saml)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/auth/login", "/api/v1/auth/login"])
@pytest.mark.parametrize("bad_provider", ["bogus", "null", "OIDC", "google"])
def test_login_invalid_provider_returns_422(client: TestClient, path: str, bad_provider: str) -> None:
    resp = client.get(path, params={"provider": bad_provider})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("provider", ["oidc", "saml"])
def test_login_valid_provider_passes_validation(client: TestClient, provider: str) -> None:
    """Valid providers must pass parameter validation (never 422).

    In this auth-disabled harness no auth service is configured, so the
    handler returns the unconfigured-SSO 404 pinned by
    tests/unit/test_issue_3166_unconfigured_subsystem_status.py - the
    point here is that a supported provider reaches the handler rather
    than being rejected at the validation layer.
    """
    resp = client.get("/auth/login", params={"provider": provider})
    assert resp.status_code != 422, resp.text
