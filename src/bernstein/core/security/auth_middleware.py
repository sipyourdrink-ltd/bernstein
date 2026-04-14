"""Authentication middleware for the Bernstein task server.

Replaces the simple BearerAuthMiddleware with a multi-strategy middleware
that supports:
- JWT tokens (from SSO login)
- Agent identity JWT tokens (per-agent, task-scoped, zero-trust)
- Legacy bearer tokens (backwards compatible)
- Public path exemptions
- User context injection into request.state

Zero-trust enforcement
----------------------
When an agent presents a task-scoped JWT, the middleware extracts the task ID
from the URL path for mutating operations (complete, fail, progress, cancel,
block) and validates that the task ID appears in the token's ``task_ids``
claim.  A token without a task scope (``task_ids == []``) is treated as
unrestricted (manager / orchestrator tokens).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    from bernstein.core.agent_identity import AgentIdentityStore
    from bernstein.core.security.auth import AuthService

_PERM_TASKS_WRITE = "tasks:write"

logger = logging.getLogger(__name__)

# Regex to extract task ID from paths like /tasks/{id}/complete
_TASK_ID_PATH_RE = re.compile(r"^/tasks/([^/]+)/(?:complete|fail|progress|cancel|block|steal)$")


# Paths that are always accessible without authentication
AUTH_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/live",
        "/ready",
        "/alive",
        "/.well-known/agent.json",
        "/docs",
        "/openapi.json",
        "/webhook",
        "/webhooks/github",
        "/dashboard",
        "/dashboard/data",
        "/dashboard/file_locks",
        "/events",
        # Auth flow endpoints (must be public for login to work)
        "/auth/login",
        "/auth/oidc/callback",
        "/auth/saml/acs",
        "/auth/saml/metadata",
        "/auth/cli/device",
        "/auth/cli/token",
        "/auth/providers",
    }
)

# Path prefixes that are always accessible without authentication.
# Used for routes with path parameters (e.g. /hooks/{session_id}).
AUTH_PUBLIC_PATH_PREFIXES = ("/hooks/",)

# Read-only methods that viewers can access
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Route → required permission mapping for write operations
_ROUTE_PERMISSIONS: dict[str, str] = {
    "/tasks": _PERM_TASKS_WRITE,
    "/agents": "agents:write",
    "/cluster": "cluster:write",
    "/bulletin": "bulletin:write",
    "/auth": "auth:manage",
    "/config": "config:write",
    "/webhooks": "webhooks:manage",
}


def _get_required_permission(path: str, method: str) -> str | None:
    """Determine the required permission for a request.

    Returns None if no specific permission is needed (public/read).
    """
    # Check specific path patterns first (before prefix matching)
    if "/kill" in path:
        return "agents:read" if method in _READ_METHODS else "agents:kill"

    if method in _READ_METHODS:
        # Read operations need basic read permission on the resource
        for prefix, perm in _ROUTE_PERMISSIONS.items():
            if path.startswith(prefix):
                return perm.replace(":write", ":read").replace(":manage", ":read")
        return "status:read"  # Default read permission

    # Write operations — check specific action paths before prefix
    if "/complete" in path or "/fail" in path or "/cancel" in path or "/block" in path:
        return _PERM_TASKS_WRITE

    for prefix, perm in _ROUTE_PERMISSIONS.items():
        if path.startswith(prefix):
            return perm

    return _PERM_TASKS_WRITE  # Default write permission


class SSOAuthMiddleware(BaseHTTPMiddleware):
    """Multi-strategy authentication middleware.

    Authentication strategies (tried in order):
    1. SSO JWT token in Authorization: Bearer <jwt>
    2. Agent identity JWT (per-agent, task-scoped — zero-trust enforcement)
    3. Legacy static bearer token
    4. No auth (if auth is not configured)

    On successful auth, injects ``request.state.user`` (AuthUser or None)
    and ``request.state.auth_claims`` (dict) for downstream routes.

    For agent identity JWTs, ``request.state.agent_identity`` is also set
    (``AgentIdentity``) so that route handlers can perform finer-grained
    checks if needed.
    """

    def __init__(
        self,
        app: Any,
        auth_service: AuthService | None = None,
        legacy_token: str | None = None,
        agent_identity_store: AgentIdentityStore | None = None,
    ) -> None:
        super().__init__(app)
        self._auth_service = auth_service
        self._legacy_token = legacy_token
        self._agent_identity_store = agent_identity_store

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        path = request.url.path

        # Public paths are always accessible
        if path in AUTH_PUBLIC_PATHS or path.startswith(AUTH_PUBLIC_PATH_PREFIXES):
            response: StarletteResponse = await call_next(request)
            return response

        auth_header = request.headers.get("authorization", "")
        has_bearer = auth_header.startswith("Bearer ")

        # No SSO or legacy auth configured (dev/no-auth mode).
        # In this mode we still validate Bearer tokens when presented — this
        # enforces zero-trust for agents that do include their tokens while
        # allowing unauthenticated local development requests to pass through.
        if self._auth_service is None and not self._legacy_token:
            if not has_bearer:
                # No token, no auth configured → dev-mode pass-through
                response = await call_next(request)
                return response
            # A Bearer token is present — validate it as an agent JWT.
            token = auth_header[7:]
            return await self._try_agent_or_reject(request, call_next, path, token)

        # Auth IS configured — require a valid token.
        if not has_bearer:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # Strip "Bearer "

        # Strategy 1: Try SSO JWT validation (if SSO auth service is available)
        if self._auth_service is not None:
            sso_result = self._try_sso_auth(request, token, path)
            if sso_result is not None:
                if isinstance(sso_result, JSONResponse):
                    return sso_result
                response = await call_next(request)
                return response

        # Strategy 2: Agent identity JWT (zero-trust, task-scoped)
        if self._agent_identity_store is not None:
            agent_result = await self._try_agent_jwt(request, call_next, path, token)
            if agent_result is not None:
                return agent_result

        # Strategy 3: Legacy bearer token
        if self._legacy_token:
            import hmac

            if hmac.compare_digest(token, self._legacy_token):
                # Legacy tokens get operator-level access
                request.state.user = None  # type: ignore[attr-defined]
                request.state.auth_claims = {"legacy": True}  # type: ignore[attr-defined]
                response = await call_next(request)
                return response

        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid or expired authentication token"},
        )

    async def _try_agent_or_reject(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
        path: str,
        token: str,
    ) -> StarletteResponse:
        """Validate an agent JWT in no-auth mode; reject invalid tokens."""
        if self._agent_identity_store is not None:
            result = await self._try_agent_jwt(request, call_next, path, token)
            if result is not None:
                return result
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid or expired authentication token"},
        )

    def _try_sso_auth(
        self,
        request: Request,
        token: str,
        path: str,
    ) -> JSONResponse | bool | None:
        """Validate SSO JWT. Returns JSONResponse on RBAC fail, True on success, None on miss."""
        assert self._auth_service is not None
        result = self._auth_service.validate_token(token)
        if result is None:
            return None

        user, claims = result
        request.state.user = user  # type: ignore[attr-defined]
        request.state.auth_claims = claims  # type: ignore[attr-defined]

        permission = _get_required_permission(path, request.method)
        if permission and not user.has_permission(permission):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"Insufficient permissions. Required: {permission}",
                    "role": user.role.value,
                },
            )
        return True

    async def _try_agent_jwt(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
        path: str,
        token: str,
    ) -> StarletteResponse | None:
        """Attempt agent identity JWT validation. Returns response or None on miss."""
        assert self._agent_identity_store is not None
        agent_identity = self._agent_identity_store.authenticate(token)
        if agent_identity is None:
            return None

        request.state.user = None  # type: ignore[attr-defined]
        request.state.auth_claims = {  # type: ignore[attr-defined]
            "agent": True,
            "agent_id": agent_identity.id,
            "role": agent_identity.role,
            "task_ids": agent_identity.task_ids,
        }
        request.state.agent_identity = agent_identity  # type: ignore[attr-defined]

        # Zero-trust: enforce task scope for mutating task operations.
        # Agents with a non-empty task_ids list may only act on their assigned
        # tasks.  Agents with task_ids=[] are unrestricted (manager role).
        if agent_identity.task_ids and request.method not in _READ_METHODS:
            task_scope_error = _check_agent_task_scope(path, agent_identity.task_ids)
            if task_scope_error is not None:
                logger.warning(
                    "Agent %s denied task-scope access to %s: %s",
                    agent_identity.id,
                    path,
                    task_scope_error,
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": task_scope_error,
                        "agent_id": agent_identity.id,
                    },
                )

        response: StarletteResponse = await call_next(request)
        return response


def _check_agent_task_scope(path: str, allowed_task_ids: list[str]) -> str | None:
    """Return an error message if the request path is out of the agent's task scope.

    Returns None when the request is permitted.

    Only task-mutating operations are checked — reads and non-task paths are
    always allowed so agents can query status and post to the bulletin board.

    Args:
        path: Request URL path.
        allowed_task_ids: Task IDs the agent token is scoped to.

    Returns:
        Error message string if access should be denied, None otherwise.
    """
    m = _TASK_ID_PATH_RE.match(path)
    if m is None:
        # Not a task-specific mutating path — allow (bulletin, status, etc.)
        return None
    task_id = m.group(1)
    if task_id not in allowed_task_ids:
        return f"Task {task_id!r} is not in this agent's task scope (allowed: {allowed_task_ids})"
    return None
