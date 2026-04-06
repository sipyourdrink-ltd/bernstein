"""Dashboard session-based authentication.

Provides a lightweight session-based auth layer for /dashboard routes.
The password/token is configured in bernstein.yaml under ``dashboard_auth``
or via the ``BERNSTEIN_DASHBOARD_PASSWORD`` environment variable.

Sessions are stored in-memory with configurable timeout.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Paths that require dashboard authentication when enabled
_DASHBOARD_PATHS = frozenset(
    {
        "/dashboard",
        "/dashboard/data",
        "/dashboard/file_locks",
    }
)

# Prefix match for dashboard sub-routes
_DASHBOARD_PREFIX = "/dashboard/"

# The login/logout endpoints themselves must be public
_DASHBOARD_AUTH_PUBLIC = frozenset(
    {
        "/dashboard/auth/login",
        "/dashboard/auth/logout",
        "/dashboard/auth/status",
    }
)

# Cookie name for dashboard sessions
SESSION_COOKIE = "bernstein_dashboard_session"


@dataclass
class DashboardSession:
    """A single dashboard session."""

    token: str
    created_at: float
    last_accessed: float


class DashboardSessionStore:
    """In-memory session store with expiration.

    Args:
        timeout_seconds: Session lifetime in seconds.
        max_sessions: Maximum concurrent sessions to prevent memory growth.
    """

    def __init__(self, timeout_seconds: int = 3600, max_sessions: int = 100) -> None:
        self._sessions: dict[str, DashboardSession] = {}
        self._timeout_seconds = timeout_seconds
        self._max_sessions = max_sessions

    def create_session(self) -> str:
        """Create a new session and return the session token."""
        self._cleanup_expired()
        # Evict oldest if at capacity
        if len(self._sessions) >= self._max_sessions:
            oldest_token = min(self._sessions, key=lambda t: self._sessions[t].last_accessed)
            del self._sessions[oldest_token]

        token = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[token] = DashboardSession(
            token=token,
            created_at=now,
            last_accessed=now,
        )
        return token

    def validate_session(self, token: str) -> bool:
        """Check whether a session token is valid and not expired."""
        session = self._sessions.get(token)
        if session is None:
            return False
        now = time.time()
        if (now - session.created_at) > self._timeout_seconds:
            del self._sessions[token]
            return False
        session.last_accessed = now
        return True

    def revoke_session(self, token: str) -> None:
        """Revoke (delete) a session."""
        self._sessions.pop(token, None)

    @property
    def active_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = time.time()
        expired = [
            token for token, session in self._sessions.items() if (now - session.created_at) > self._timeout_seconds
        ]
        for token in expired:
            del self._sessions[token]


def _get_dashboard_password() -> str:
    """Resolve dashboard password from seed config or environment."""
    return os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")


def verify_password(provided: str, expected: str) -> bool:
    """Constant-time password comparison."""
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


class DashboardAuthMiddleware(BaseHTTPMiddleware):
    """Session-based authentication for /dashboard routes.

    When a dashboard password is configured, all /dashboard requests
    must carry a valid session cookie.  Sessions are created via
    POST /dashboard/auth/login.
    """

    def __init__(
        self,
        app: ASGIApp,
        session_store: DashboardSessionStore | None = None,
        password: str = "",
    ) -> None:
        super().__init__(app)
        self._session_store = session_store or DashboardSessionStore()
        self._password = password

    @property
    def session_store(self) -> DashboardSessionStore:
        """Access the session store (for route handlers)."""
        return self._session_store

    @property
    def password(self) -> str:
        """Access the configured password."""
        return self._password

    def _is_dashboard_path(self, path: str) -> bool:
        """Check if a path requires dashboard authentication."""
        return path in _DASHBOARD_PATHS or path.startswith(_DASHBOARD_PREFIX)

    def _is_auth_exempt(self, path: str) -> bool:
        """Check if a path is exempt from dashboard auth."""
        return path in _DASHBOARD_AUTH_PUBLIC

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        path = request.url.path

        # Only intercept dashboard paths
        if not self._is_dashboard_path(path):
            return await call_next(request)

        # Auth endpoints are always accessible
        if self._is_auth_exempt(path):
            return await call_next(request)

        # If no password is configured, pass through
        effective_password = self._password or _get_dashboard_password()
        if not effective_password:
            return await call_next(request)

        # Check session cookie
        session_token = request.cookies.get(SESSION_COOKIE, "")
        if session_token and self._session_store.validate_session(session_token):
            return await call_next(request)

        # Also check Authorization header (for API access to dashboard data)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if self._session_store.validate_session(token):
                return await call_next(request)

        # For HTML dashboard page requests, redirect to login
        accept = request.headers.get("accept", "")
        if "text/html" in accept and path == "/dashboard":
            return JSONResponse(
                status_code=401,
                content={"detail": "Dashboard authentication required", "login_url": "/dashboard/auth/login"},
            )

        return JSONResponse(
            status_code=401,
            content={"detail": "Dashboard authentication required"},
        )
