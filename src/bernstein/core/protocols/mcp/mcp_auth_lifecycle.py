"""MCP-006: MCP server auth lifecycle.

Token refresh on 401, re-auth flow, and session management for MCP
servers that require OAuth or API-key authentication.

Handles:
- Automatic token refresh when a 401/403 is encountered.
- Cooldown between refresh attempts to avoid thundering herd.
- Session tracking with expiry awareness.
- Re-authentication flow when refresh tokens are also expired.

Usage::

    from bernstein.core.protocols.mcp_auth_lifecycle import AuthSession, AuthLifecycleManager

    mgr = AuthLifecycleManager()
    mgr.register_session("github", AuthSession(
        server_name="github",
        access_token="tok_...",
        refresh_token="ref_...",
        token_endpoint="https://github.com/login/oauth/access_token",
        client_id="...",
        expires_at=time.time() + 3600,
    ))
    # On 401:
    result = mgr.handle_auth_failure("github", 401)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REFRESH_COOLDOWN: float = 60.0  # seconds between refresh attempts
DEFAULT_EXPIRY_BUFFER: float = 300.0  # refresh 5 min before expiry
MAX_REFRESH_RETRIES: int = 3


class AuthState(StrEnum):
    """Authentication lifecycle state for an MCP server session."""

    ACTIVE = "active"
    REFRESHING = "refreshing"
    EXPIRED = "expired"
    FAILED = "failed"


class RefreshResult(StrEnum):
    """Outcome of a token refresh attempt."""

    SUCCESS = "success"
    COOLDOWN = "cooldown"
    NO_SESSION = "no_session"
    REFRESH_FAILED = "refresh_failed"
    MAX_RETRIES = "max_retries"


@dataclass
class AuthSession:
    """Authentication session for a single MCP server.

    Attributes:
        server_name: MCP server name.
        access_token: Current access token.
        refresh_token: Token used to obtain a new access token.
        token_endpoint: URL to POST token refresh requests to.
        client_id: OAuth client ID.
        expires_at: Unix timestamp when the access token expires.
        state: Current auth lifecycle state.
        refresh_count: Number of refresh attempts since last manual auth.
        last_refresh_at: Unix timestamp of last refresh attempt.
    """

    server_name: str
    access_token: str = ""
    refresh_token: str = ""
    token_endpoint: str = ""
    client_id: str = ""
    expires_at: float = 0.0
    state: AuthState = AuthState.ACTIVE
    refresh_count: int = 0
    last_refresh_at: float = 0.0

    def is_expired(self, buffer: float = DEFAULT_EXPIRY_BUFFER) -> bool:
        """Return True if the token is expired or about to expire.

        Args:
            buffer: Seconds before actual expiry to consider as expired.
        """
        if self.expires_at <= 0:
            return False  # No expiry set, assume valid
        return time.time() >= (self.expires_at - buffer)

    def seconds_until_expiry(self) -> float:
        """Return seconds until token expiry, or 0 if already expired."""
        if self.expires_at <= 0:
            return float("inf")
        remaining = self.expires_at - time.time()
        return max(0.0, remaining)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (excluding tokens)."""
        return {
            "server_name": self.server_name,
            "state": self.state.value,
            "expires_at": self.expires_at,
            "seconds_until_expiry": self.seconds_until_expiry(),
            "refresh_count": self.refresh_count,
            "last_refresh_at": self.last_refresh_at,
            "has_refresh_token": bool(self.refresh_token),
        }


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a token refresh attempt.

    Attributes:
        server_name: MCP server name.
        result: The outcome category.
        new_token: The new access token, if refresh succeeded.
        error: Error message, if refresh failed.
    """

    server_name: str
    result: RefreshResult
    new_token: str = ""
    error: str = ""


# Type alias for the token refresher callable
type _TokenRefresher = Any  # Callable[[AuthSession], tuple[str, float]]


class AuthLifecycleManager:
    """Manages auth sessions for multiple MCP servers.

    Tracks per-server auth state, handles token refresh with cooldown,
    and provides expiry dashboards.

    Args:
        refresh_cooldown: Minimum seconds between refresh attempts per server.
        max_retries: Maximum refresh attempts before marking as FAILED.
        token_refresher: Optional callable to perform the actual HTTP token
            refresh. Signature: (session: AuthSession) -> tuple[str, float]
            returning (new_access_token, new_expires_at). If None, uses a
            stub that always fails (useful for testing session lifecycle).
    """

    def __init__(
        self,
        *,
        refresh_cooldown: float = DEFAULT_REFRESH_COOLDOWN,
        max_retries: int = MAX_REFRESH_RETRIES,
        token_refresher: _TokenRefresher | None = None,
    ) -> None:
        self._sessions: dict[str, AuthSession] = {}
        self._refresh_cooldown = refresh_cooldown
        self._max_retries = max_retries
        self._token_refresher = token_refresher

    def register_session(self, name: str, session: AuthSession) -> None:
        """Register an auth session for a server.

        Args:
            name: Server name (used as key).
            session: The auth session to track.
        """
        self._sessions[name] = session
        logger.info("Registered auth session for MCP server '%s'", name)

    def get_session(self, name: str) -> AuthSession | None:
        """Return the auth session for a server, or None."""
        return self._sessions.get(name)

    def handle_auth_failure(self, name: str, status_code: int) -> RefreshOutcome:
        """Handle an authentication failure (401/403) for a server.

        Attempts token refresh with cooldown and retry limits.

        Args:
            name: Server name that returned the error.
            status_code: HTTP status code (401 or 403).

        Returns:
            A RefreshOutcome describing what happened.
        """
        if status_code not in (401, 403):
            return RefreshOutcome(server_name=name, result=RefreshResult.SUCCESS)

        session = self._sessions.get(name)
        if session is None:
            return RefreshOutcome(
                server_name=name,
                result=RefreshResult.NO_SESSION,
                error=f"No auth session registered for '{name}'",
            )

        # Check cooldown
        now = time.time()
        if session.last_refresh_at > 0:
            elapsed = now - session.last_refresh_at
            if elapsed < self._refresh_cooldown:
                return RefreshOutcome(
                    server_name=name,
                    result=RefreshResult.COOLDOWN,
                    error=f"Cooldown active ({elapsed:.0f}s / {self._refresh_cooldown:.0f}s)",
                )

        # Check retry limit
        if session.refresh_count >= self._max_retries:
            session.state = AuthState.FAILED
            return RefreshOutcome(
                server_name=name,
                result=RefreshResult.MAX_RETRIES,
                error=f"Max retries ({self._max_retries}) exceeded",
            )

        # Attempt refresh
        session.state = AuthState.REFRESHING
        session.last_refresh_at = now
        session.refresh_count += 1

        if self._token_refresher is None:
            session.state = AuthState.FAILED
            return RefreshOutcome(
                server_name=name,
                result=RefreshResult.REFRESH_FAILED,
                error="No token refresher configured",
            )

        try:
            new_token, new_expiry = self._token_refresher(session)
            session.access_token = new_token
            session.expires_at = new_expiry
            session.state = AuthState.ACTIVE
            session.refresh_count = 0  # reset on success
            logger.info("Token refreshed for MCP server '%s'", name)
            return RefreshOutcome(
                server_name=name,
                result=RefreshResult.SUCCESS,
                new_token=new_token,
            )
        except Exception as exc:
            session.state = AuthState.EXPIRED
            logger.warning("Token refresh failed for '%s': %s", name, exc)
            return RefreshOutcome(
                server_name=name,
                result=RefreshResult.REFRESH_FAILED,
                error=str(exc),
            )

    def check_expiring_soon(self, buffer: float = DEFAULT_EXPIRY_BUFFER) -> list[AuthSession]:
        """Return sessions whose tokens are expiring within buffer seconds.

        Args:
            buffer: Seconds before expiry to flag.

        Returns:
            List of sessions nearing expiry.
        """
        return [s for s in self._sessions.values() if s.is_expired(buffer)]

    def proactive_refresh(self, buffer: float = DEFAULT_EXPIRY_BUFFER) -> list[RefreshOutcome]:
        """Proactively refresh tokens that are about to expire.

        Args:
            buffer: Seconds before expiry to trigger refresh.

        Returns:
            List of refresh outcomes.
        """
        outcomes: list[RefreshOutcome] = []
        for name, session in self._sessions.items():
            if session.is_expired(buffer) and session.state != AuthState.FAILED:
                outcome = self.handle_auth_failure(name, 401)
                outcomes.append(outcome)
        return outcomes

    def list_sessions(self) -> list[AuthSession]:
        """Return all tracked auth sessions."""
        return list(self._sessions.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize all sessions to a JSON-compatible dict."""
        return {name: s.to_dict() for name, s in self._sessions.items()}
