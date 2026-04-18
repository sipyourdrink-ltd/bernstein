"""Focused tests for auth_middleware.py."""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false, reportFunctionMemberAccess=false, reportAttributeAccessIssue=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.auth_middleware import SSOAuthMiddleware, _get_required_permission
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# Module-level: these tests rely on the secure-by-default middleware behaviour
# (no ``BERNSTEIN_AUTH_DISABLED`` shim), so opt out of the autouse fixture.
pytestmark = pytest.mark.auth_enabled


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


def test_admin_only_routes_require_admin_manage() -> None:
    """/shutdown, /broadcast, /drain, and /config map to admin:manage (audit-119)."""
    assert _get_required_permission("/shutdown", "POST") == "admin:manage"
    assert _get_required_permission("/broadcast", "POST") == "admin:manage"
    assert _get_required_permission("/drain", "POST") == "admin:manage"
    assert _get_required_permission("/drain/cancel", "POST") == "admin:manage"
    assert _get_required_permission("/config", "POST") == "admin:manage"


def test_unknown_write_route_falls_closed_to_admin_manage() -> None:
    """Unknown write routes require admin:manage — fail CLOSED (audit-119)."""
    # Previously fell through to tasks:write; now requires operator-level access.
    assert _get_required_permission("/some/unmapped/write/path", "POST") == "admin:manage"
    assert _get_required_permission("/brand-new-endpoint", "DELETE") == "admin:manage"


def test_tasks_write_token_denied_on_shutdown_route() -> None:
    """A token with only tasks:write is rejected by /shutdown with 403 (audit-119)."""

    def _has_tasks_write_only(permission: str) -> bool:
        return permission == "tasks:write"

    user = SimpleNamespace(role=SimpleNamespace(value="operator"), has_permission=_has_tasks_write_only)

    def _validate_token(token: str) -> tuple[Any, dict[str, str]]:
        del token
        return user, {"sub": "op1"}

    auth_service = SimpleNamespace(validate_token=_validate_token)
    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, auth_service=auth_service)

    @app.post("/shutdown")
    async def shutdown() -> dict[str, str]:
        return {"status": "shutting_down"}

    client = TestClient(app)
    response = client.post("/shutdown", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert "admin:manage" in response.json()["detail"]


def test_admin_manage_token_accepted_on_shutdown_route() -> None:
    """A token with admin:manage is accepted by /shutdown (audit-119)."""

    def _has_admin_manage(permission: str) -> bool:
        return permission == "admin:manage"

    user = SimpleNamespace(role=SimpleNamespace(value="admin"), has_permission=_has_admin_manage)

    def _validate_token(token: str) -> tuple[Any, dict[str, str]]:
        del token
        return user, {"sub": "admin1"}

    auth_service = SimpleNamespace(validate_token=_validate_token)
    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, auth_service=auth_service)

    @app.post("/shutdown")
    async def shutdown() -> dict[str, str]:
        return {"status": "shutting_down"}

    client = TestClient(app)
    response = client.post("/shutdown", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["status"] == "shutting_down"


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


# ---------------------------------------------------------------------------
# Zero-trust agent JWT task-scope enforcement
# ---------------------------------------------------------------------------


def _app_with_agent_identity_store(tmp_path: Any) -> tuple[TestClient, Any]:
    """Build a test app with an AgentIdentityStore for zero-trust JWT tests."""
    from pathlib import Path

    from bernstein.core.agent_identity import AgentIdentityStore

    auth_dir = Path(str(tmp_path))
    store = AgentIdentityStore(auth_dir)

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, agent_identity_store=store)

    @app.post("/tasks/{task_id}/complete")
    async def complete_task(task_id: str, request: Request) -> dict[str, Any]:
        return {"task_id": task_id, "agent_id": getattr(request.state, "auth_claims", {}).get("agent_id")}

    @app.get("/status")
    async def get_status(request: Request) -> dict[str, Any]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False), store


def test_agent_jwt_with_task_scope_allows_own_task(tmp_path: Any) -> None:
    """An agent JWT scoped to task-A allows completing task-A."""
    client, store = _app_with_agent_identity_store(tmp_path)
    _, token = store.create_identity("agent-session-1", "backend", task_ids=["task-abc"])

    response = client.post(
        "/tasks/task-abc/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"result_summary": "done"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "task-abc"
    assert data["agent_id"] == "agent-session-1"


def test_agent_jwt_with_task_scope_denies_out_of_scope_task(tmp_path: Any) -> None:
    """An agent JWT scoped to task-A must be denied when it tries to act on task-B."""
    client, store = _app_with_agent_identity_store(tmp_path)
    _, token = store.create_identity("agent-session-2", "backend", task_ids=["task-abc"])

    response = client.post(
        "/tasks/task-xyz/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"result_summary": "done"},
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "task-xyz" in detail
    assert "task-abc" in detail


def test_agent_jwt_without_task_scope_is_unrestricted(tmp_path: Any) -> None:
    """An orchestrator/manager agent JWT (task_ids=[]) may act on any task."""
    client, store = _app_with_agent_identity_store(tmp_path)
    # No task_ids → unrestricted manager token
    _, token = store.create_identity("manager-session-1", "manager", task_ids=[])

    response = client.post(
        "/tasks/any-task-id/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"result_summary": "done"},
    )

    assert response.status_code == 200


def test_agent_jwt_read_requests_not_scope_checked(tmp_path: Any) -> None:
    """GET requests bypass task-scope enforcement even for scoped agents."""
    client, store = _app_with_agent_identity_store(tmp_path)
    _, token = store.create_identity("agent-session-3", "backend", task_ids=["task-abc"])

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_invalid_bearer_token_rejected(tmp_path: Any) -> None:
    """An invalid Bearer token (not a valid agent JWT) is rejected with 401."""
    client, _store = _app_with_agent_identity_store(tmp_path)

    response = client.post(
        "/tasks/task-abc/complete",
        headers={"Authorization": "Bearer not-a-valid-token"},
        json={"result_summary": "done"},
    )

    assert response.status_code == 401


def test_check_agent_task_scope_returns_none_for_non_task_paths() -> None:
    """Non task-mutating paths (bulletin, status) never trigger scope errors."""
    from bernstein.core.auth_middleware import _check_agent_task_scope

    assert _check_agent_task_scope("/bulletin", ["task-abc"]) is None
    assert _check_agent_task_scope("/status", ["task-abc"]) is None
    assert _check_agent_task_scope("/tasks", ["task-abc"]) is None


def test_check_agent_task_scope_allows_scoped_task() -> None:
    """_check_agent_task_scope allows access when task_id is in the allowed list."""
    from bernstein.core.auth_middleware import _check_agent_task_scope

    assert _check_agent_task_scope("/tasks/task-abc/complete", ["task-abc", "task-def"]) is None
    assert _check_agent_task_scope("/tasks/task-def/fail", ["task-abc", "task-def"]) is None


def test_agent_jwt_denied_on_shutdown_route(tmp_path: Any) -> None:
    """Agent identity JWTs are rejected on admin:manage routes (audit-119).

    Even an unrestricted manager-role agent JWT (``task_ids=[]``) must not
    be able to SIGTERM the server via ``/shutdown``.
    """
    from pathlib import Path

    from bernstein.core.agent_identity import AgentIdentityStore

    store = AgentIdentityStore(Path(str(tmp_path)))
    _, token = store.create_identity("manager-1", "manager", task_ids=[])

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, agent_identity_store=store)

    @app.post("/shutdown")
    async def shutdown() -> dict[str, str]:
        return {"status": "shutting_down"}

    client = TestClient(app)
    response = client.post("/shutdown", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["required_permission"] == "admin:manage"


def test_agent_jwt_denied_on_broadcast_and_drain(tmp_path: Any) -> None:
    """Agent identity JWTs are blocked from /broadcast and /drain (audit-119)."""
    from pathlib import Path

    from bernstein.core.agent_identity import AgentIdentityStore

    store = AgentIdentityStore(Path(str(tmp_path)))
    _, token = store.create_identity("mgr-2", "manager", task_ids=[])

    app = FastAPI()
    app.add_middleware(SSOAuthMiddleware, agent_identity_store=store)

    @app.post("/broadcast")
    async def broadcast() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/drain")
    async def drain() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    for path in ("/broadcast", "/drain"):
        response = client.post(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403, path
        assert response.json()["required_permission"] == "admin:manage"


def test_check_agent_task_scope_denies_out_of_scope_task() -> None:
    """_check_agent_task_scope returns error message for out-of-scope task."""
    from bernstein.core.auth_middleware import _check_agent_task_scope

    error = _check_agent_task_scope("/tasks/task-xyz/complete", ["task-abc"])

    assert error is not None
    assert "task-xyz" in error
    assert "task-abc" in error
