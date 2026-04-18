"""Tests for secure-by-default auth middleware behavior (audit-113).

Covers:
- Unauthenticated requests to protected paths return 401.
- Valid bearer tokens return 200.
- ``/hooks/{session_id}`` requires a valid HMAC signature.
- ``/health`` remains public.
- ``BERNSTEIN_AUTH_DISABLED=1`` opt-out is honoured and logs a warning.
"""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportFunctionMemberAccess=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.auth_middleware import (
    AUTH_DEV_ONLY_PUBLIC_PATHS,
    AUTH_HMAC_PATH_PREFIXES,
    AUTH_HMAC_PATHS,
    AUTH_PUBLIC_PATHS,
    SSOAuthMiddleware,
    auth_disabled_via_opt_out,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth_enabled


# ---------------------------------------------------------------------------
# Public-path surface
# ---------------------------------------------------------------------------


def test_public_paths_do_not_include_dashboard_or_events_or_webhook() -> None:
    """Dashboard, SSE, hook, and webhook paths must not be in AUTH_PUBLIC_PATHS."""
    # Dashboard and SSE need a bearer token (they expose operational state).
    assert "/dashboard" not in AUTH_PUBLIC_PATHS
    assert "/dashboard/data" not in AUTH_PUBLIC_PATHS
    assert "/dashboard/file_locks" not in AUTH_PUBLIC_PATHS
    assert "/dashboard/events" not in AUTH_PUBLIC_PATHS
    assert "/events" not in AUTH_PUBLIC_PATHS
    # Broadcast mutates cluster state.
    assert "/broadcast" not in AUTH_PUBLIC_PATHS
    # Webhook/hook paths belong to the HMAC-auth set, not bare public.
    assert "/webhook" not in AUTH_PUBLIC_PATHS
    assert "/webhooks/github" not in AUTH_PUBLIC_PATHS
    assert "/webhook" in AUTH_HMAC_PATHS
    assert "/webhooks/github" in AUTH_HMAC_PATHS
    assert "/hooks/" in AUTH_HMAC_PATH_PREFIXES


def test_public_paths_still_contain_health_and_login_flow() -> None:
    """Trivially-public endpoints stay reachable without a token."""
    for path in (
        "/health",
        "/health/ready",
        "/health/live",
        "/ready",
        "/alive",
        "/.well-known/agent.json",
        "/auth/login",
        "/auth/cli/device",
    ):
        assert path in AUTH_PUBLIC_PATHS, path


def test_docs_and_openapi_are_dev_only_public() -> None:
    """API docs and the OpenAPI schema are no longer unconditionally public.

    Audit-047 requires that ``/docs`` and ``/openapi.json`` only be
    anonymously reachable in true dev mode (no auth backend).  Once any
    authenticator is wired up they must require viewer-level auth.
    """
    for path in ("/docs", "/redoc", "/openapi.json", "/openapi.yaml"):
        assert path not in AUTH_PUBLIC_PATHS, path
        assert path in AUTH_DEV_ONLY_PUBLIC_PATHS, path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(
    *,
    legacy_token: str | None = None,
    auth_disabled: bool | None = None,
    agent_identity_store: Any = None,
) -> TestClient:
    """Build a minimal FastAPI app wrapped in SSOAuthMiddleware."""
    app = FastAPI()
    app.add_middleware(
        SSOAuthMiddleware,
        legacy_token=legacy_token,
        agent_identity_store=agent_identity_store,
        auth_disabled=auth_disabled,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/dashboard")
    async def dashboard_index() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/dashboard/data")
    async def dashboard_data() -> dict[str, str]:
        return {"tasks": "secret-metadata"}

    @app.get("/dashboard/file_locks")
    async def dashboard_file_locks() -> dict[str, str]:
        return {"locks": "secret"}

    @app.get("/dashboard/events")
    async def dashboard_events() -> dict[str, str]:
        return {"stream": "ok"}

    @app.get("/events")
    async def status_events() -> dict[str, str]:
        return {"stream": "ok"}

    @app.get("/docs")
    async def docs() -> dict[str, str]:
        return {"docs": "ok"}

    @app.get("/openapi.json")
    async def openapi_json() -> dict[str, str]:
        return {"openapi": "3.0.0"}

    @app.post("/broadcast")
    async def broadcast(request: Request) -> dict[str, str]:
        del request
        return {"ok": "yes"}

    @app.post("/tasks")
    async def create_task() -> dict[str, str]:
        return {"id": "t1"}

    return TestClient(app)


def _hmac_header(body: bytes, secret: str) -> dict[str, str]:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Bernstein-Hook-Signature-256": f"sha256={digest}"}


# ---------------------------------------------------------------------------
# Default-on auth
# ---------------------------------------------------------------------------


def test_unauth_dashboard_events_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a bearer token, GET /dashboard/events must be 401."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/dashboard/events")

    assert response.status_code == 401


def test_unauth_status_events_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSE status stream at /events must require auth too."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/events")

    assert response.status_code == 401


def test_unauth_broadcast_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /broadcast without a bearer token must be 401."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.post("/broadcast", json={"message": "hi"})

    assert response.status_code == 401


def test_unauth_returns_401_even_without_any_auth_backend_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secure-by-default: no SSO, no legacy token, no agent store → still 401."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app()

    response = client.post("/tasks")

    assert response.status_code == 401


def test_valid_bearer_token_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid legacy bearer token lets the request through to the handler."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get(
        "/dashboard/events",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200


def test_health_is_public_without_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """/health remains reachable with no auth configured and no token provided."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/health")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# audit-047: dashboard / events / openapi surface gating
# ---------------------------------------------------------------------------


def test_unauth_dashboard_data_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a bearer token, GET /dashboard/data must be 401 (audit-047).

    ``/dashboard/data`` leaks the task list, cost data and agent metadata,
    so it MUST require auth whenever the server has an auth backend
    configured.
    """
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/dashboard/data")

    assert response.status_code == 401


def test_auth_dashboard_data_returns_200_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a valid bearer token, GET /dashboard/data is allowed (audit-047)."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get(
        "/dashboard/data",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["tasks"] == "secret-metadata"


def test_unauth_dashboard_file_locks_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: /dashboard/file_locks must require auth when configured."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/dashboard/file_locks")

    assert response.status_code == 401


def test_unauth_dashboard_index_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: /dashboard must require auth when auth backend is configured."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/dashboard")

    assert response.status_code == 401


def test_unauth_openapi_returns_401_when_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: /openapi.json must require auth when a backend is configured.

    Leaking the OpenAPI schema reveals the attack surface (all routes and
    parameter shapes).  With a legacy bearer token configured the server is
    considered production-ready and the schema is gated behind viewer auth.
    """
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/openapi.json")

    assert response.status_code == 401


def test_unauth_docs_returns_401_when_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: /docs is dev-only-public; requires auth in production."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get("/docs")

    assert response.status_code == 401


def test_openapi_is_anonymous_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: in true dev mode (no auth backend) /openapi.json is reachable."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    client = _build_app()  # no legacy_token, no SSO, no agent store

    response = client.get("/openapi.json")

    assert response.status_code == 200


def test_docs_is_anonymous_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: in true dev mode (no auth backend) /docs is reachable."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    client = _build_app()

    response = client.get("/docs")

    assert response.status_code == 200


def test_openapi_with_bearer_token_returns_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: valid bearer token unlocks /openapi.json when auth is configured."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    response = client.get(
        "/openapi.json",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200


def test_env_legacy_token_is_detected_as_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: BERNSTEIN_AUTH_TOKEN in env counts as "auth configured".

    Even when the middleware is constructed without an explicit
    ``legacy_token`` argument, finding ``BERNSTEIN_AUTH_TOKEN`` in the
    environment must flip the middleware into "production" mode so the
    dev-only public paths are gated.
    """
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "env-secret")
    client = _build_app()  # no explicit legacy_token

    response = client.get("/openapi.json")

    assert response.status_code == 401


def test_health_live_stays_public_for_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit-047: k8s probes must keep working even with auth configured."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    client = _build_app(legacy_token="secret")

    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


def test_auth_disabled_env_is_truthy_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth_disabled_via_opt_out reflects the environment variable."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    assert auth_disabled_via_opt_out() is True

    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "true")
    assert auth_disabled_via_opt_out() is True

    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "no")
    assert auth_disabled_via_opt_out() is False

    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    assert auth_disabled_via_opt_out() is False


def test_auth_disabled_opt_out_passes_requests_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When BERNSTEIN_AUTH_DISABLED=1, requests bypass the bearer gate."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    # Reset the one-shot warning flag so the opt-out actually re-warns.
    SSOAuthMiddleware._warned_disabled = False
    client = _build_app(legacy_token="secret")

    response = client.post("/tasks")

    assert response.status_code == 200


def test_auth_disabled_logs_loud_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Opting out must emit a loud WARNING so operators notice."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    SSOAuthMiddleware._warned_disabled = False

    caplog.set_level(logging.WARNING)
    client = _build_app(legacy_token="secret")
    # Trigger middleware construction via a real request — Starlette builds
    # the middleware stack lazily on first request.
    client.get("/health")

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("auth is DISABLED" in m for m in messages), messages


# ---------------------------------------------------------------------------
# Hook HMAC auth
# ---------------------------------------------------------------------------


def _build_hooks_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str) -> TestClient:
    """Build a small app that mounts the real /hooks router under SSO middleware."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("BERNSTEIN_HOOK_SECRET", secret)

    app = FastAPI()
    # Real middleware enforces the "no bearer → 401" default.
    app.add_middleware(SSOAuthMiddleware, legacy_token="secret")

    from bernstein.core.routes.hooks import router as hooks_router

    app.include_router(hooks_router)

    # The hook receiver writes under workdir/.sdd — give it a tmp sandbox.
    sdd = tmp_path / ".sdd"
    (sdd / "runtime" / "hooks").mkdir(parents=True)
    (sdd / "runtime" / "completed").mkdir(parents=True)
    app.state.workdir = tmp_path  # type: ignore[attr-defined]
    app.state.sdd_dir = sdd  # type: ignore[attr-defined]

    return TestClient(app)


def test_hooks_without_hmac_returns_401(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """POST /hooks/X without X-Bernstein-Hook-Signature-256 must be 401."""
    client = _build_hooks_app(monkeypatch, tmp_path, "hook-secret")

    response = client.post(
        "/hooks/agent-123",
        content=json.dumps({"hook_event_name": "Stop"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_hooks_with_valid_hmac_returns_200(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A request signed with the configured secret is accepted (no bearer needed)."""
    secret = "hook-secret"
    client = _build_hooks_app(monkeypatch, tmp_path, secret)

    body = json.dumps({"hook_event_name": "Stop"}).encode("utf-8")
    response = client.post(
        "/hooks/agent-123",
        content=body,
        headers={"Content-Type": "application/json", **_hmac_header(body, secret)},
    )

    assert response.status_code == 200, response.text


def test_hooks_with_bad_hmac_returns_401(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A forged signature is rejected by the route handler."""
    client = _build_hooks_app(monkeypatch, tmp_path, "hook-secret")

    body = json.dumps({"hook_event_name": "Stop"}).encode("utf-8")
    response = client.post(
        "/hooks/agent-123",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Bernstein-Hook-Signature-256": "sha256=" + "0" * 64,
        },
    )

    assert response.status_code == 401


def test_hooks_endpoint_is_disabled_when_no_secret_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no secret, the hook endpoint rejects everything (secure default)."""
    monkeypatch.delenv("BERNSTEIN_HOOK_SECRET", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, legacy_token="secret")
    from bernstein.core.routes.hooks import router as hooks_router

    app.include_router(hooks_router)
    sdd = tmp_path / ".sdd"
    (sdd / "runtime" / "hooks").mkdir(parents=True)
    app.state.workdir = tmp_path  # type: ignore[attr-defined]
    app.state.sdd_dir = sdd  # type: ignore[attr-defined]
    client = TestClient(app)

    body = json.dumps({"hook_event_name": "Stop"}).encode("utf-8")
    response = client.post(
        "/hooks/agent-123",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    # Reassure: the handler, not the bearer middleware, rejected this.
    assert isinstance(response.json(), dict)
    assert "Hook endpoint is not configured" in response.json().get("detail", "") or (
        "Invalid or missing hook signature" in response.json().get("detail", "")
    )


# ---------------------------------------------------------------------------
# Agent identity JWT still works under default-on auth
# ---------------------------------------------------------------------------


def test_agent_jwt_token_accepted_under_default_on_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An agent identity JWT remains a valid authenticator when auth is default-on."""
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)

    from bernstein.core.agent_identity import AgentIdentityStore

    store = AgentIdentityStore(tmp_path)
    _, token = store.create_identity("agent-1", "backend", task_ids=[])

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, agent_identity_store=store)

    @app.get("/status")
    async def status(request: Request) -> JSONResponse:
        claims = getattr(request.state, "auth_claims", None)
        return JSONResponse(content={"ok": True, "claims": claims})

    client = TestClient(app)

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["claims"]["agent_id"] == "agent-1"
