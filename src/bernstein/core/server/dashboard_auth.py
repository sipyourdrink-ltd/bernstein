"""Dashboard authentication: sessions, scoped tokens, and authz enforcement.

Credentials (issue #2366)
-------------------------
Two credential kinds open the dashboard:

* A password (``bernstein.yaml`` ``dashboard_auth`` block or the
  ``BERNSTEIN_DASHBOARD_PASSWORD`` env var). A password login grants the
  ``operator`` scope under the ``dashboard-operator`` principal.
* A scoped token from the signed token journal
  (:class:`~bernstein.core.server.dashboard_tokens.DashboardTokenRegistry`),
  issued by ``bernstein auth dashboard-token issue``. The token carries its
  own principal and scope (``viewer`` or ``operator``).

Sessions wrap a successful login in a cookie with the same principal and
scope -- a session can never widen what the credential granted. Sessions are
in-memory with a timeout; the *grant* itself is not: every login and every
write-authorization is a signed governance decision anchored in the
``dashboard-auth`` lineage-spine run (see
:class:`~bernstein.core.server.dashboard_tokens.DashboardGovernance`), so
the authz history recomputes offline via ``bernstein governance verify``.

Enforcement
-----------
When any credential is configured, every ``/dashboard`` route requires one.
Safe methods (GET / HEAD / OPTIONS) need the ``dashboard.read`` permission;
everything else needs ``dashboard.write``. ``viewer`` never has
``dashboard.write``, so a read-only token cannot trigger a state-changing
action on any route -- current or future -- under the dashboard prefix. The
same enforcement applies to the ``/api/v1/dashboard`` mirror of the routes.

When no credential is configured the middleware passes dashboard requests
through unchanged. The serve entry points close that hole for network
binds: see
:func:`~bernstein.core.server.dashboard_tokens.resolve_dashboard_posture`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.server.dashboard_tokens import (
    ACTION_READ,
    ACTION_WRITE,
    SCOPE_OPERATOR,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

    from bernstein.core.server.dashboard_tokens import (
        DashboardGovernance,
        DashboardTokenRecord,
        DashboardTokenRegistry,
    )

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

# The versioned mirror of the dashboard routes (AUDIT-126 parity mount).
_API_V1_PREFIX = "/api/v1"

# The login/logout endpoints themselves must be public
_DASHBOARD_AUTH_PUBLIC = frozenset(
    {
        "/dashboard/auth/login",
        "/dashboard/auth/logout",
        "/dashboard/auth/status",
    }
)

# HTTP methods that only read state.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Cookie name for dashboard sessions
SESSION_COOKIE = "bernstein_dashboard_session"

#: Principal recorded for password logins (passwords carry no identity of
#: their own; scoped tokens carry a real per-seat principal).
PASSWORD_PRINCIPAL = "dashboard-operator"

#: Subject recorded on decisions for requests without a valid credential.
ANONYMOUS_PRINCIPAL = "anonymous"


def _normalize_dashboard_path(path: str) -> str:
    """Map the ``/api/v1`` mirror of a dashboard path onto its root form."""
    if path.startswith(_API_V1_PREFIX + "/"):
        return path[len(_API_V1_PREFIX) :]
    return path


@dataclass
class DashboardSession:
    """A single dashboard session bound to the credential that opened it."""

    token: str
    created_at: float
    last_accessed: float
    principal: str = PASSWORD_PRINCIPAL
    scope: str = SCOPE_OPERATOR


class DashboardSessionStore:
    """In-memory session store with expiration.

    Sessions are ephemeral projections of a journaled login decision; the
    durable record of the grant lives in the governance spine, not here.

    Args:
        timeout_seconds: Session lifetime in seconds.
        max_sessions: Maximum concurrent sessions to prevent memory growth.
    """

    def __init__(self, timeout_seconds: int = 3600, max_sessions: int = 100) -> None:
        self._sessions: dict[str, DashboardSession] = {}
        self._timeout_seconds = timeout_seconds
        self._max_sessions = max_sessions

    def create_session(self, principal: str = PASSWORD_PRINCIPAL, scope: str = SCOPE_OPERATOR) -> str:
        """Create a new session and return the session token.

        Args:
            principal: The acting principal the session attributes actions to.
            scope: The credential scope the session inherits (never wider
                than what the login credential granted).
        """
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
            principal=principal,
            scope=scope,
        )
        return token

    def resolve_session(self, token: str) -> DashboardSession | None:
        """Return the live session for *token*, or ``None``."""
        session = self._sessions.get(token)
        if session is None:
            return None
        now = time.time()
        if (now - session.created_at) > self._timeout_seconds:
            del self._sessions[token]
            return None
        session.last_accessed = now
        return session

    def validate_session(self, token: str) -> bool:
        """Check whether a session token is valid and not expired."""
        return self.resolve_session(token) is not None

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


# Both digests below are recomputed per comparison and never stored, so a
# per-value random salt would serve no purpose; the fixed salt is domain
# separation only. The KDF gives compare_digest fixed-length inputs and
# makes each online guess computationally expensive.
_PASSWORD_KDF_SALT = b"bernstein-dashboard-password-v1"
_PASSWORD_KDF_ITERATIONS = 210_000


def _password_digest(value: str) -> bytes:
    """Fixed-length PBKDF2 digest used for constant-time comparison."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        _PASSWORD_KDF_SALT,
        _PASSWORD_KDF_ITERATIONS,
    )


def verify_password(provided: str, expected: str) -> bool:
    """Constant-time password comparison."""
    if not expected:
        return False
    return hmac.compare_digest(_password_digest(provided), _password_digest(expected))


@dataclass
class DashboardAuthState:
    """Shared auth state the middleware and the auth routes both use.

    Attributes:
        session_store: Live sessions.
        token_registry: The signed scoped-token journal (may be ``None`` in
            minimal embeddings; token login is then unavailable).
        governance: The decision journal projection (may be ``None`` in
            minimal embeddings; decisions are then not recorded).
        password: Explicitly configured password. When empty the
            ``BERNSTEIN_DASHBOARD_PASSWORD`` env var is consulted at request
            time so operators can configure auth without a restart.
    """

    session_store: DashboardSessionStore = field(default_factory=DashboardSessionStore)
    token_registry: DashboardTokenRegistry | None = None
    governance: DashboardGovernance | None = None
    password: str = ""

    def effective_password(self) -> str:
        """The password in force for this request (config over env)."""
        return self.password or _get_dashboard_password()

    def auth_required(self) -> bool:
        """True when any dashboard credential is configured."""
        if self.effective_password():
            return True
        return self.token_registry is not None and self.token_registry.has_tokens()

    def resolve_credential(self, request: Request) -> tuple[str, str] | None:
        """Resolve the request's credential to ``(principal, scope)``.

        Tried in order: session cookie, session token as Bearer, scoped
        dashboard token as Bearer. Tokens are matched verbatim.
        """
        cookie_token = request.cookies.get(SESSION_COOKIE, "")
        if cookie_token:
            session = self.session_store.resolve_session(cookie_token)
            if session is not None:
                return (session.principal, session.scope)

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:]
            session = self.session_store.resolve_session(bearer)
            if session is not None:
                return (session.principal, session.scope)
            if self.token_registry is not None:
                record: DashboardTokenRecord | None = self.token_registry.validate(bearer)
                if record is not None:
                    return (record.principal, record.scope)
        return None

    def record_decision(self, *, subject: str, scope: str, action: str) -> str:
        """Journal one authz decision; returns the verdict.

        Falls back to a pure (unjournaled) projection when no governance
        sink is attached, so minimal embeddings still enforce scopes.
        """
        if self.governance is None:
            allowed = scope == SCOPE_OPERATOR or (scope and action != ACTION_WRITE)
            return "allow" if allowed else "deny"
        decision = self.governance.decide(
            subject=subject,
            scope=scope,
            action=action,
            now=int(time.time()),
        )
        return decision.verdict


class DashboardAuthMiddleware(BaseHTTPMiddleware):
    """Scope-enforcing authentication for /dashboard routes.

    When a dashboard credential is configured (password or at least one
    scoped token), all /dashboard requests must carry a valid session
    cookie or Bearer credential. Write methods additionally require the
    ``operator`` scope, and every write authorization -- allowed or denied --
    is journaled as a signed governance decision with the acting principal.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: DashboardAuthState | None = None,
        session_store: DashboardSessionStore | None = None,
        password: str = "",
    ) -> None:
        super().__init__(app)
        if state is None:
            state = DashboardAuthState(
                session_store=session_store or DashboardSessionStore(),
                password=password,
            )
        self._state = state

    @property
    def state(self) -> DashboardAuthState:
        """Access the shared auth state (for route handlers and tests)."""
        return self._state

    @property
    def session_store(self) -> DashboardSessionStore:
        """Access the session store (for route handlers)."""
        return self._state.session_store

    @property
    def password(self) -> str:
        """Access the configured password."""
        return self._state.password

    def _is_dashboard_path(self, path: str) -> bool:
        """Check if a path requires dashboard authentication."""
        path = _normalize_dashboard_path(path)
        return path in _DASHBOARD_PATHS or path.startswith(_DASHBOARD_PREFIX)

    def _is_auth_exempt(self, path: str) -> bool:
        """Check if a path is exempt from dashboard auth."""
        return _normalize_dashboard_path(path) in _DASHBOARD_AUTH_PUBLIC

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

        # If no credential backend is configured, pass through. The serve
        # entry points refuse non-loopback binds in this state, so pass-
        # through only ever happens on loopback or in embedded test apps.
        if not self._state.auth_required():
            return await call_next(request)

        credential = self._state.resolve_credential(request)
        if credential is None:
            # No valid credential: reject without journaling. Unauthenticated
            # probes carry no subject worth anchoring and must not be able to
            # grow the decision journal.
            return JSONResponse(
                status_code=401,
                content={"detail": "Dashboard authentication required"},
            )

        principal, scope = credential
        action = ACTION_READ if request.method in _SAFE_METHODS else ACTION_WRITE

        if action == ACTION_WRITE:
            # Every write authorization is a journaled decision (allow or
            # deny), so operator actions carry the acting principal into the
            # audit receipts.
            verdict = self._state.record_decision(subject=principal, scope=scope, action=action)
            if verdict != "allow":
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Dashboard scope does not permit state-changing actions"},
                )
            request.state.dashboard_principal = principal
            request.state.dashboard_scope = scope
            return await call_next(request)

        # Reads: a valid credential of any scope grants dashboard.read; the
        # grant was journaled at login / issuance, so passing reads are not
        # re-journaled on every poll.
        request.state.dashboard_principal = principal
        request.state.dashboard_scope = scope
        return await call_next(request)
