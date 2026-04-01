"""Authentication middleware for the Bernstein task server.

Replaces the simple BearerAuthMiddleware with a multi-strategy middleware
that supports:
- JWT tokens (from SSO login)
- Legacy bearer tokens (backwards compatible)
- Public path exemptions
- User context injection into request.state
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    from bernstein.core.auth import AuthService

logger = logging.getLogger(__name__)


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

# Read-only methods that viewers can access
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Route → required permission mapping for write operations
_ROUTE_PERMISSIONS: dict[str, str] = {
    "/tasks": "tasks:write",
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
        return "tasks:write"

    for prefix, perm in _ROUTE_PERMISSIONS.items():
        if path.startswith(prefix):
            return perm

    return "tasks:write"  # Default write permission


class SSOAuthMiddleware(BaseHTTPMiddleware):
    """Multi-strategy authentication middleware.

    Authentication strategies (tried in order):
    1. JWT token in Authorization: Bearer <jwt>
    2. Legacy static bearer token
    3. No auth (if auth is not configured)

    On successful auth, injects ``request.state.user`` (AuthUser)
    and ``request.state.auth_claims`` (dict) for downstream routes.
    """

    def __init__(
        self,
        app: Any,
        auth_service: AuthService | None = None,
        legacy_token: str | None = None,
    ) -> None:
        super().__init__(app)
        self._auth_service = auth_service
        self._legacy_token = legacy_token

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        # No auth configured — pass through
        if self._auth_service is None and not self._legacy_token:
            response: StarletteResponse = await call_next(request)
            return response

        path = request.url.path

        # Public paths are always accessible
        if path in AUTH_PUBLIC_PATHS:
            response = await call_next(request)
            return response

        # Extract token from Authorization header
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # Strip "Bearer "

        # Strategy 1: Try JWT validation (if SSO auth service is available)
        if self._auth_service is not None:
            result = self._auth_service.validate_token(token)
            if result is not None:
                user, claims = result
                request.state.user = user  # type: ignore[attr-defined]
                request.state.auth_claims = claims  # type: ignore[attr-defined]

                # RBAC check
                permission = _get_required_permission(path, request.method)
                if permission and not user.has_permission(permission):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": f"Insufficient permissions. Required: {permission}",
                            "role": user.role.value,
                        },
                    )

                response = await call_next(request)
                return response

        # Strategy 2: Legacy bearer token
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
