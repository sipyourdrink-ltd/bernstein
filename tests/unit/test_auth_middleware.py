"""Focused tests for auth_middleware.py."""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportFunctionMemberAccess=false, reportAttributeAccessIssue=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from bernstein.core.auth_middleware import SSOAuthMiddleware, _get_required_permission


def _app_with_middleware(auth_service: Any = None, legacy_token: str | None = None) -> TestClient:
    """Build a tiny FastAPI app wrapped in SSOAuthMiddleware for tests."""
    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, auth_service=auth_service, legacy_token=legacy_token)

    @app.get("/tasks")
    async def read_tasks(request: Request) -> dict[str, Any]:
        return {
            "user_present": hasattr(request.state, "user"),
            "claims": getattr(request.state, "auth_claims", None),
        }

    @app.post("/tasks")
    async def write_tasks(request: Request) -> dict[str, Any]:
        return {
            "user_present": hasattr(request.state, "user"),
            "claims": getattr(request.state, "auth_claims", None),
        }

    return TestClient(app)


def test_get_required_permission_maps_read_write_and_kill_routes() -> None:
    """_get_required_permission chooses the correct read, write, and kill permissions."""
    assert _get_required_permission("/tasks", "GET") == "tasks:read"
    assert _get_required_permission("/tasks", "POST") == "tasks:write"
    assert _get_required_permission("/agents/abc/kill", "POST") == "agents:kill"


def test_public_paths_bypass_authentication() -> None:
    """Public paths are reachable without any auth configuration."""
    client = _app_with_middleware(auth_service=object())
    app = client.app

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"ok": "yes"}

    response = client.get("/health")

    assert response.status_code == 200


def test_missing_bearer_header_returns_401() -> None:
    """Protected routes return 401 when Authorization is missing."""
    client = _app_with_middleware(auth_service=object())

    response = client.get("/tasks")

    assert response.status_code == 401


def test_jwt_user_without_permission_gets_403() -> None:
    """A validated JWT still gets 403 when the user lacks the required permission."""

    def _deny(permission: str) -> bool:
        del permission
        return False

    user = SimpleNamespace(role=SimpleNamespace(value="viewer"), has_permission=_deny)

    def _validate_token(token: str) -> tuple[Any, dict[str, str]]:
        del token
        return user, {"sub": "u1"}

    auth_service = SimpleNamespace(validate_token=_validate_token)
    client = _app_with_middleware(auth_service=auth_service)

    response = client.post("/tasks", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert response.json()["detail"].startswith("Insufficient permissions")


def test_legacy_token_grants_access_when_it_matches() -> None:
    """Legacy bearer tokens still grant access and inject legacy auth claims."""
    client = _app_with_middleware(legacy_token="secret")

    response = client.post("/tasks", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    assert response.json()["claims"] == {"legacy": True}
