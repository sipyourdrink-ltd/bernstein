"""OAuth 2.0 Authorization Code + PKCE flow for the Bernstein web dashboard.

Implements RFC 7636 (Proof Key for Code Exchange) to protect the authorization
code grant against interception attacks.

Two authorization modes are supported:
- automatic: opens the system browser and captures the callback via a local
  HTTP listener on the redirect URI
- manual: prints the authorization URL and prompts the user to paste the code

Usage example::

    from bernstein.core.security.oauth_pkce import PKCEFlow

    flow = PKCEFlow(
        client_id="my-client",
        authorization_endpoint="https://idp.example.com/oauth/authorize",
        token_endpoint="https://idp.example.com/oauth/token",
        redirect_uri="http://localhost:8099/callback",
        scopes="openid profile email",
    )

    # Automatic browser mode
    tokens = flow.run_automatic()

    # Manual mode (no browser)
    url = flow.get_authorization_url()
    code = input("Paste the authorization code: ")
    tokens = await flow.exchange_code(code)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PKCE primitives (RFC 7636)
# ---------------------------------------------------------------------------

_VERIFIER_BYTES = 96  # 96 raw bytes → 128-char base64url (≥ 128 chars per spec)

# Default time-to-live for a state token before it is considered expired.
# RFC 6749 recommends short-lived anti-CSRF tokens; 10 minutes mirrors typical
# IdP authorization session lifetimes.
DEFAULT_STATE_TTL_SECONDS: float = 600.0


def generate_code_verifier() -> str:
    """Return a cryptographically random 128-char PKCE code_verifier.

    The verifier uses unreserved URL-safe characters (A-Z a-z 0-9 - . _ ~)
    and is exactly 128 characters long, satisfying RFC 7636 §4.1 (43-128 chars).
    """
    return secrets.token_urlsafe(_VERIFIER_BYTES)[:128]


def generate_code_challenge(code_verifier: str) -> str:
    """Derive the S256 code_challenge from a code_verifier.

    ``code_challenge = BASE64URL(SHA256(ASCII(code_verifier)))``
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using S256 method.

    Returns:
        A two-element tuple ``(code_verifier, code_challenge)``.
    """
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    return verifier, challenge


# ---------------------------------------------------------------------------
# One-shot local callback server
# ---------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures ``?code=`` and ``?state=`` from the redirect."""

    captured_code: str | None = None
    captured_state: str | None = None
    captured_error: str | None = None

    def do_GET(self) -> None:  # type: ignore[override]
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.captured_error = params["error"][0]
            body = b"<h2>Authorization failed. You may close this window.</h2>"
        elif "code" in params:
            _CallbackHandler.captured_code = params["code"][0]
            # Capture state even if absent so the caller can detect its
            # absence and reject the callback. Storing None here is the
            # signal that the IdP omitted the parameter.
            if "state" in params:
                _CallbackHandler.captured_state = params["state"][0]
            body = b"<h2>Authorization successful! You may close this window.</h2>"
        else:
            body = b"<h2>Waiting for authorization...</h2>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default access log


def _wait_for_callback(port: int, timeout: float = 120.0) -> tuple[str | None, str | None]:
    """Start a one-shot local HTTP server and wait for the OAuth callback.

    Args:
        port: TCP port to bind the local callback listener to.
        timeout: Seconds to wait for a callback before giving up.

    Returns:
        A ``(code, state)`` tuple. Either element may be ``None`` if not
        present in the callback. Callers MUST validate the returned state
        against the flow's expected state before trusting the code.

    Raises:
        OAuthError: If the authorization server reported an error.
    """
    _CallbackHandler.captured_code = None
    _CallbackHandler.captured_state = None
    _CallbackHandler.captured_error = None

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout

    def _serve() -> None:
        server.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    t.join(timeout=timeout + 1)

    if _CallbackHandler.captured_error:
        raise OAuthError(f"Authorization server returned error: {_CallbackHandler.captured_error}")

    return _CallbackHandler.captured_code, _CallbackHandler.captured_state


# ---------------------------------------------------------------------------
# PKCE Flow orchestrator
# ---------------------------------------------------------------------------


class OAuthError(Exception):
    """Raised when the OAuth flow encounters an unrecoverable error."""


class OAuthStateError(OAuthError):
    """Raised when the OAuth ``state`` parameter fails CSRF validation.

    Subclass of :class:`OAuthError` so callers can either catch the
    specific validation failure or the general OAuth failure.
    """


@dataclass
class PKCETokens:
    """Holds the token set returned after a successful PKCE exchange."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    scope: str | None = None
    raw: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> PKCETokens:
        return cls(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in"),
            refresh_token=data.get("refresh_token"),
            id_token=data.get("id_token"),
            scope=data.get("scope"),
            raw=data,
        )


class PKCEFlow:
    """Orchestrates an OAuth 2.0 Authorization Code + PKCE flow.

    Args:
        client_id: OAuth2 client identifier.
        authorization_endpoint: IdP authorization URL.
        token_endpoint: IdP token exchange URL.
        redirect_uri: Callback URI (must match IdP registration).
            For automatic mode this should be ``http://localhost:<port>/callback``.
        scopes: Space-separated list of OAuth scopes.
        client_secret: Optional client secret (public clients may omit it).
        state_ttl_seconds: Lifetime of the anti-CSRF ``state`` token. After
            this many seconds the state is considered expired and any
            callback carrying it will be rejected.
    """

    def __init__(
        self,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
        redirect_uri: str = "http://localhost:8099/callback",
        scopes: str = "openid profile email",
        client_secret: str = "",
        state_ttl_seconds: float = DEFAULT_STATE_TTL_SECONDS,
    ) -> None:
        self.client_id = client_id
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.client_secret = client_secret
        self.state_ttl_seconds = state_ttl_seconds

        # Generated per-flow (reset by start())
        self._code_verifier: str = ""
        self._code_challenge: str = ""
        self._state: str = ""
        # Monotonic timestamp recorded when state was generated; used for
        # expiry checks. 0.0 means "never started".
        self._state_created_at: float | None = None
        # Set to True once validate_state has accepted the state. Any
        # subsequent validation attempt with the same state is treated as a
        # replay and rejected.
        self._state_consumed: bool = False

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Generate a fresh PKCE pair and CSRF state for this flow.

        Resets any previously stored state and clears the replay flag so
        the flow can be re-run safely.
        """
        self._code_verifier, self._code_challenge = generate_pkce_pair()
        self._state = secrets.token_urlsafe(32)
        self._state_created_at = time.monotonic()
        self._state_consumed = False

    def validate_state(self, received_state: str | None) -> None:
        """Validate a ``state`` value returned by the IdP callback.

        Enforces four properties:

        1. **Presence** — the callback MUST carry a ``state`` parameter.
        2. **Freshness** — the flow's stored state must not be older than
           :attr:`state_ttl_seconds`.
        3. **Match** — the received value must byte-equal the stored
           state, compared in constant time via :func:`hmac.compare_digest`.
        4. **Single-use** — once a state has been accepted, any subsequent
           validation attempt is rejected as a replay.

        Args:
            received_state: The raw ``state`` value parsed from the callback
                query string, or ``None`` if absent.

        Raises:
            OAuthStateError: If the state is missing, expired, mismatched,
                or already consumed.
        """
        if not self._state or self._state_created_at is None:
            raise OAuthStateError("PKCE flow not started; cannot validate state before start()")
        if self._state_consumed:
            raise OAuthStateError("State replay detected: this state has already been consumed")
        if received_state is None or received_state == "":
            raise OAuthStateError("Missing state parameter in OAuth callback")

        age = time.monotonic() - self._state_created_at
        if age > self.state_ttl_seconds:
            raise OAuthStateError(f"State expired after {age:.1f}s (ttl={self.state_ttl_seconds:.1f}s)")

        # Constant-time comparison to defeat timing side-channels.
        expected = self._state.encode("ascii")
        received = received_state.encode("ascii")
        if not hmac.compare_digest(expected, received):
            raise OAuthStateError("State mismatch: received value does not match expected state")

        # Mark the state as consumed so replays are rejected.
        self._state_consumed = True

    def get_authorization_url(self) -> str:
        """Return the authorization URL the user must visit.

        Calls :meth:`start` automatically if not already called.
        """
        if not self._code_verifier:
            self.start()

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": self._state,
            "code_challenge": self._code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.authorization_endpoint}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> PKCETokens:
        """Exchange an authorization code for tokens.

        Sends ``code_verifier`` in the token request body so the IdP can
        verify the original PKCE challenge.

        Args:
            code: The authorization code received from the IdP callback.

        Returns:
            A :class:`PKCETokens` instance with the access token and
            optional refresh / id tokens.

        Raises:
            OAuthError: If the IdP token exchange fails.
            ValueError: If :meth:`start` or :meth:`get_authorization_url`
                has not been called yet.
        """
        if not self._code_verifier:
            raise ValueError("PKCE flow not started; call start() or get_authorization_url() first")

        payload: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": self._code_verifier,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.token_endpoint,
                data=payload,
                headers={"Accept": "application/json"},
                timeout=15.0,
            )

        if resp.status_code != 200:
            logger.error("PKCE token exchange failed: %s %s", resp.status_code, resp.text)
            raise OAuthError(f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:200]}")

        data: dict[str, Any] = resp.json()
        if "access_token" not in data:
            raise OAuthError(f"Token response missing access_token: {data}")

        return PKCETokens.from_response(data)

    # ------------------------------------------------------------------
    # Mode: automatic (browser)
    # ------------------------------------------------------------------

    async def run_automatic(self, open_browser: bool = True) -> PKCETokens:
        """Run the full PKCE flow by opening the system browser.

        Starts a one-shot local HTTP server on the redirect URI port to
        capture the authorization code, validates the returned CSRF state,
        then exchanges the code for tokens.

        Args:
            open_browser: Set ``False`` to skip browser opening (useful for
                testing or environments without a display).

        Returns:
            :class:`PKCETokens` on success.

        Raises:
            OAuthError: On authorization failure or timeout.
            OAuthStateError: If the returned ``state`` parameter is missing,
                expired, or does not match the one generated by :meth:`start`.
        """
        self.start()
        url = self.get_authorization_url()

        parsed = urllib.parse.urlparse(self.redirect_uri)
        port = parsed.port or 8099

        if open_browser:
            import webbrowser

            webbrowser.open(url)
        else:
            print(f"Open this URL in your browser:\n  {url}")

        code, received_state = _wait_for_callback(port=port)
        if not code:
            raise OAuthError("No authorization code received (timeout or user cancelled)")

        # CRITICAL: validate state BEFORE exchanging the code. A mismatched
        # state means the callback may have been forged by an attacker; we
        # must not forward an attacker-controlled code to the token endpoint.
        self.validate_state(received_state)

        return await self.exchange_code(code)

    # ------------------------------------------------------------------
    # Mode: manual (paste code)
    # ------------------------------------------------------------------

    async def run_manual(self, code: str, state: str | None = None) -> PKCETokens:
        """Exchange a manually pasted authorization code.

        Call :meth:`get_authorization_url` first to get the URL to give the
        user, then call this method with the code they paste back. When the
        caller also has access to the ``state`` parameter from the redirect
        URL it MUST be passed here so CSRF validation can run.

        Args:
            code: Authorization code copied from the IdP redirect URL.
            state: The ``state`` value copied from the same redirect URL.
                When provided it is validated via :meth:`validate_state`
                before the code is exchanged. ``None`` skips the check to
                preserve backward compatibility with callers that only have
                the code (e.g. out-of-band paste workflows).

        Returns:
            :class:`PKCETokens` on success.

        Raises:
            OAuthStateError: When ``state`` is supplied but fails validation.
        """
        if not self._code_verifier:
            self.start()
        if state is not None:
            self.validate_state(state)
        return await self.exchange_code(code)
