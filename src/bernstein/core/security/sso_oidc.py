"""ENT-006: SSO/OIDC authentication for web dashboard.

Standard OpenID Connect Authorization Code flow with configurable provider.
Supports auto-discovery via ``.well-known/openid-configuration``, PKCE,
token refresh, and group-to-role mapping for Bernstein RBAC.

Usage::

    config = OIDCConfig(
        issuer_url="https://idp.example.com",
        client_id="bernstein-dashboard",
        client_secret="secret",
        redirect_uri="http://localhost:8052/auth/callback",
    )
    provider = OIDCProvider(config)
    auth_url = provider.authorization_url(state="random-state")
"""

from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_SCOPES = ("openid", "profile", "email")
_STATE_BYTES = 32
_NONCE_BYTES = 32


@dataclass(frozen=True)
class OIDCConfig:
    """OIDC provider configuration.

    Attributes:
        issuer_url: The OIDC issuer URL (must support discovery).
        client_id: OAuth2 client identifier.
        client_secret: OAuth2 client secret (empty for public clients).
        redirect_uri: Callback URI registered with the provider.
        scopes: OAuth2 scopes to request.
        audience: Optional audience parameter for token requests.
        group_claim: JWT claim containing group memberships.
        role_mapping: Mapping from IdP group names to Bernstein roles.
        session_timeout_s: Dashboard session lifetime in seconds.
        token_refresh_margin_s: Refresh tokens this many seconds before expiry.
    """

    issuer_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:8052/auth/callback"
    scopes: tuple[str, ...] = _DEFAULT_SCOPES
    audience: str = ""
    group_claim: str = "groups"
    role_mapping: dict[str, str] = field(default_factory=dict[str, str])
    session_timeout_s: int = 3600
    token_refresh_margin_s: int = 300


# ---------------------------------------------------------------------------
# Discovery document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OIDCDiscoveryDocument:
    """Parsed OIDC discovery (.well-known/openid-configuration) data.

    Attributes:
        issuer: Issuer identifier.
        authorization_endpoint: URL for the authorization request.
        token_endpoint: URL for token exchange.
        userinfo_endpoint: URL for user info.
        jwks_uri: URL for JSON Web Key Set.
        end_session_endpoint: URL for logout.
    """

    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    jwks_uri: str = ""
    end_session_endpoint: str = ""


def parse_discovery_document(data: dict[str, Any]) -> OIDCDiscoveryDocument:
    """Parse a raw discovery JSON document into a typed dataclass.

    Args:
        data: Dictionary from ``GET /.well-known/openid-configuration``.

    Returns:
        Parsed OIDCDiscoveryDocument.
    """
    return OIDCDiscoveryDocument(
        issuer=str(data.get("issuer", "")),
        authorization_endpoint=str(data.get("authorization_endpoint", "")),
        token_endpoint=str(data.get("token_endpoint", "")),
        userinfo_endpoint=str(data.get("userinfo_endpoint", "")),
        jwks_uri=str(data.get("jwks_uri", "")),
        end_session_endpoint=str(data.get("end_session_endpoint", "")),
    )


# ---------------------------------------------------------------------------
# Token response
# ---------------------------------------------------------------------------


@dataclass
class OIDCTokenResponse:
    """Parsed token endpoint response.

    Attributes:
        access_token: Bearer access token.
        id_token: OIDC ID token (JWT).
        refresh_token: Optional refresh token.
        expires_at: Absolute expiry timestamp (seconds since epoch).
        token_type: Token type (typically ``Bearer``).
        scope: Space-separated granted scopes.
    """

    access_token: str = ""
    id_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: str = ""


def parse_token_response(data: dict[str, Any]) -> OIDCTokenResponse:
    """Parse a raw token endpoint JSON response.

    Args:
        data: Dictionary from the token endpoint.

    Returns:
        Parsed OIDCTokenResponse.
    """
    expires_in = int(data.get("expires_in", 3600))
    return OIDCTokenResponse(
        access_token=str(data.get("access_token", "")),
        id_token=str(data.get("id_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        expires_at=time.time() + expires_in,
        token_type=str(data.get("token_type", "Bearer")),
        scope=str(data.get("scope", "")),
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class OIDCSession:
    """Authenticated OIDC session for the dashboard.

    Attributes:
        session_id: Opaque session identifier.
        subject: User subject from the ID token.
        email: User email.
        groups: Group memberships from the ID token.
        role: Resolved Bernstein role.
        tokens: OIDC tokens for the session.
        created_at: Session creation timestamp.
        last_accessed: Last access timestamp.
    """

    session_id: str = ""
    subject: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list[str])
    role: str = "viewer"
    tokens: OIDCTokenResponse = field(default_factory=OIDCTokenResponse)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# OIDC Provider
# ---------------------------------------------------------------------------


class OIDCProvider:
    """OIDC authentication provider for Bernstein dashboard.

    Manages the authorization code flow, token exchange, and session
    creation.  Does **not** perform HTTP calls directly — callers supply
    raw response data from the IdP endpoints.

    Args:
        config: OIDC provider configuration.
        discovery: Pre-loaded discovery document (skips HTTP discovery).
    """

    def __init__(
        self,
        config: OIDCConfig,
        discovery: OIDCDiscoveryDocument | None = None,
    ) -> None:
        self._config = config
        self._discovery = discovery
        self._sessions: dict[str, OIDCSession] = {}
        self._pending_states: dict[str, float] = {}  # state -> created_at

    @property
    def config(self) -> OIDCConfig:
        """Return the OIDC configuration."""
        return self._config

    @property
    def discovery(self) -> OIDCDiscoveryDocument | None:
        """Return the discovery document if loaded."""
        return self._discovery

    def set_discovery(self, discovery: OIDCDiscoveryDocument) -> None:
        """Set the discovery document after HTTP fetch.

        Args:
            discovery: Parsed discovery document.
        """
        self._discovery = discovery

    def generate_state(self) -> str:
        """Generate a cryptographic state parameter for CSRF protection.

        Returns:
            URL-safe random state string.
        """
        state = secrets.token_urlsafe(_STATE_BYTES)
        self._pending_states[state] = time.time()
        return state

    def validate_state(self, state: str) -> bool:
        """Validate and consume a state parameter.

        Args:
            state: State string from the callback.

        Returns:
            True if the state is valid and was pending.
        """
        return self._pending_states.pop(state, None) is not None

    def authorization_url(self, state: str | None = None) -> str:
        """Build the authorization endpoint URL.

        Args:
            state: CSRF state parameter. Auto-generated if None.

        Returns:
            Full authorization URL with query parameters.

        Raises:
            ValueError: If discovery document is not loaded.
        """
        if self._discovery is None:
            msg = "Discovery document not loaded"
            raise ValueError(msg)

        if state is None:
            state = self.generate_state()

        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
        }
        if self._config.audience:
            params["audience"] = self._config.audience

        base = self._discovery.authorization_endpoint
        return f"{base}?{urllib.parse.urlencode(params)}"

    def resolve_role(self, groups: list[str]) -> str:
        """Map IdP groups to a Bernstein role.

        The first matching group in the role_mapping wins. Falls back to
        ``"viewer"`` if no mapping matches.

        Args:
            groups: Group memberships from the ID token.

        Returns:
            Bernstein role string.
        """
        for group in groups:
            if group in self._config.role_mapping:
                return self._config.role_mapping[group]
        return "viewer"

    def create_session(
        self,
        tokens: OIDCTokenResponse,
        subject: str,
        email: str,
        groups: list[str],
    ) -> OIDCSession:
        """Create and store an authenticated session.

        Args:
            tokens: OIDC tokens from the token endpoint.
            subject: User subject identifier.
            email: User email address.
            groups: Group memberships.

        Returns:
            New OIDCSession.
        """
        session_id = secrets.token_urlsafe(32)
        role = self.resolve_role(groups)
        now = time.time()
        session = OIDCSession(
            session_id=session_id,
            subject=subject,
            email=email,
            groups=groups,
            role=role,
            tokens=tokens,
            created_at=now,
            last_accessed=now,
        )
        self._sessions[session_id] = session
        logger.info(
            "OIDC session created for %s (role=%s)",
            email,
            role,
        )
        return session

    def get_session(self, session_id: str) -> OIDCSession | None:
        """Look up a session by ID, checking expiration.

        Args:
            session_id: Session identifier.

        Returns:
            OIDCSession if valid, None if expired or not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None

        now = time.time()
        if now - session.created_at > self._config.session_timeout_s:
            self._sessions.pop(session_id, None)
            logger.info("OIDC session expired for %s", session.email)
            return None

        session.last_accessed = now
        return session

    def revoke_session(self, session_id: str) -> bool:
        """Revoke (delete) a session.

        Args:
            session_id: Session to revoke.

        Returns:
            True if the session existed and was revoked.
        """
        return self._sessions.pop(session_id, None) is not None

    def active_session_count(self) -> int:
        """Return the number of active (non-expired) sessions."""
        self._cleanup_expired()
        return len(self._sessions)

    def needs_token_refresh(self, session: OIDCSession) -> bool:
        """Check if the session tokens need refreshing.

        Args:
            session: Session to check.

        Returns:
            True if the access token will expire within the refresh margin.
        """
        return time.time() >= (session.tokens.expires_at - self._config.token_refresh_margin_s)

    def update_tokens(
        self,
        session_id: str,
        tokens: OIDCTokenResponse,
    ) -> bool:
        """Update the tokens for an existing session after refresh.

        Args:
            session_id: Session identifier.
            tokens: New token response.

        Returns:
            True if the session was found and updated.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.tokens = tokens
        session.last_accessed = time.time()
        return True

    def _cleanup_expired(self) -> None:
        """Remove all expired sessions from the store."""
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.created_at > self._config.session_timeout_s]
        for sid in expired:
            self._sessions.pop(sid, None)

    def build_discovery_url(self) -> str:
        """Build the OIDC discovery endpoint URL.

        Returns:
            URL for ``.well-known/openid-configuration``.
        """
        issuer = self._config.issuer_url.rstrip("/")
        return f"{issuer}/.well-known/openid-configuration"

    def build_token_request_body(self, code: str) -> dict[str, str]:
        """Build the POST body for the token endpoint.

        Args:
            code: Authorization code from the callback.

        Returns:
            Dictionary suitable for ``application/x-www-form-urlencoded``.
        """
        body: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": self._config.client_id,
        }
        if self._config.client_secret:
            body["client_secret"] = self._config.client_secret
        return body

    def build_refresh_request_body(self, refresh_token: str) -> dict[str, str]:
        """Build the POST body for token refresh.

        Args:
            refresh_token: The refresh token to use.

        Returns:
            Dictionary suitable for ``application/x-www-form-urlencoded``.
        """
        body: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._config.client_id,
        }
        if self._config.client_secret:
            body["client_secret"] = self._config.client_secret
        return body
