"""OAuth 2.0 Authorization Code + PKCE flow for Bernstein web dashboard.

Implements RFC 7636 — Proof Key for Code Exchange — to protect the
authorization code grant against interception attacks.

Quickstart (automatic browser mode)::

    from bernstein_sdk.oauth import OAuthPKCEClient

    client = OAuthPKCEClient(
        client_id="my-dashboard",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        redirect_uri="http://localhost:8080/callback",
    )
    tokens = client.authorize()          # opens browser, waits for callback
    print(tokens["access_token"])

Manual mode (paste the code yourself)::

    tokens = client.authorize(auto=False)  # prints URL, prompts for code
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

_CODE_VERIFIER_LENGTH = 128  # per spec, 43–128 chars; 128 maximises entropy


@dataclass
class PKCEChallenge:
    """A PKCE code_verifier / code_challenge pair.

    The ``code_verifier`` is a 128-character URL-safe random string.
    The ``code_challenge`` is ``BASE64URL(SHA-256(code_verifier))``
    with ``code_challenge_method = "S256"``.

    Generate one with :meth:`generate`::

        challenge = PKCEChallenge.generate()
        assert len(challenge.code_verifier) == 128
    """

    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"

    @classmethod
    def generate(cls) -> PKCEChallenge:
        """Generate a fresh PKCE challenge pair.

        Uses :func:`secrets.token_urlsafe` for cryptographically secure
        randomness.  The verifier is truncated / padded to exactly 128
        URL-safe characters (``[A-Za-z0-9_-]``).

        Returns:
            A new :class:`PKCEChallenge` instance.
        """
        # token_urlsafe(n) returns ceil(n * 4/3) base64url chars; we need
        # exactly 128 chars so we generate a bit more and slice.
        raw = secrets.token_urlsafe(96)[:_CODE_VERIFIER_LENGTH]
        verifier = raw

        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return cls(code_verifier=verifier, code_challenge=challenge)


@dataclass
class TokenResponse:
    """Parsed OAuth token response.

    Args:
        access_token: Bearer token for API calls.
        token_type: Always ``"Bearer"`` for standard OAuth.
        expires_in: Lifetime in seconds (``None`` if not provided).
        refresh_token: Long-lived token for silent renewal (may be ``None``).
        scope: Space-separated list of granted scopes (may be ``None``).
        raw: Full JSON response dict for extension fields.
    """

    access_token: str
    token_type: str
    expires_in: int | None
    refresh_token: str | None
    scope: str | None
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TokenResponse:
        """Deserialize from a token endpoint JSON response."""
        return cls(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_in=data.get("expires_in"),
            refresh_token=data.get("refresh_token"),
            scope=data.get("scope"),
            raw=data,
        )


class OAuthPKCEClient:
    """OAuth 2.0 Authorization Code + PKCE client.

    Supports two authorization modes:

    - **Automatic** (``auto=True``, default): opens the system browser and
      prints the redirect URI with the authorization code for the caller to
      supply.  In a full server integration the redirect URI would be caught
      by a local HTTP callback server; here the caller pastes the full
      redirect URL or just the code.

    - **Manual** (``auto=False``): prints the authorization URL and prompts
      the user to paste the authorization code from the browser's address bar.

    Args:
        client_id: Registered OAuth application client ID.
        authorization_endpoint: Authorization server's ``/authorize`` URL.
        token_endpoint: Authorization server's ``/token`` URL.
        redirect_uri: Must be registered with the authorization server.
        scopes: OAuth scopes to request (default: ``["openid"]``).
        timeout: HTTP timeout for the token exchange request.
    """

    def __init__(
        self,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.redirect_uri = redirect_uri
        self.scopes = scopes or ["openid"]
        self._timeout = timeout
        self._http = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_authorization_url(self, challenge: PKCEChallenge) -> tuple[str, str]:
        """Construct the authorization URL and state nonce.

        Args:
            challenge: The PKCE challenge pair generated for this request.

        Returns:
            A ``(url, state)`` tuple where ``state`` is a random nonce that
            the caller should verify in the callback to prevent CSRF.
        """
        state = secrets.token_urlsafe(32)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge.code_challenge,
            "code_challenge_method": challenge.code_challenge_method,
        }
        url = self.authorization_endpoint + "?" + urllib.parse.urlencode(params)
        return url, state

    def exchange_code(
        self,
        code: str,
        code_verifier: str,
    ) -> TokenResponse:
        """Exchange an authorization code for tokens.

        Sends a POST to ``token_endpoint`` with the ``code_verifier``.
        The server validates that ``SHA-256(code_verifier)`` matches the
        ``code_challenge`` it received earlier.

        Args:
            code: The authorization code from the callback.
            code_verifier: The raw verifier string (not the challenge hash).

        Returns:
            Parsed :class:`TokenResponse`.

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx from the token endpoint.
        """
        resp = self._http.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return TokenResponse.from_json(resp.json())

    def authorize(self, auto: bool = True) -> TokenResponse:
        """Run the complete PKCE authorization flow.

        In automatic mode the system browser is opened and the user is
        prompted to paste the authorization code (or the full redirect URL
        containing ``?code=...``).

        In manual mode the authorization URL is printed to stdout and the
        user pastes the code.

        Args:
            auto: ``True`` to open the browser automatically (default).

        Returns:
            A :class:`TokenResponse` with the access token and metadata.
        """
        challenge = PKCEChallenge.generate()
        url, _state = self.build_authorization_url(challenge)

        if auto:
            log.debug("Opening browser for OAuth authorization: %s", url)
            webbrowser.open(url)
            print(f"\nIf the browser did not open, visit:\n  {url}\n")
        else:
            print(f"\nVisit this URL to authorize:\n  {url}\n")

        raw_input = input(
            "Paste the authorization code (or the full redirect URL): "
        ).strip()

        code = _extract_code(raw_input)
        return self.exchange_code(code, challenge.code_verifier)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> OAuthPKCEClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_code(value: str) -> str:
    """Extract the authorization code from a URL or raw code string.

    Args:
        value: Either a raw code (no ``?``) or a full redirect URL
            containing ``code=<value>`` as a query parameter.

    Returns:
        The authorization code string.

    Raises:
        ValueError: If the code cannot be found.
    """
    if "?" not in value and "#" not in value:
        return value  # raw code pasted directly

    parsed = urllib.parse.urlparse(value)
    qs = urllib.parse.parse_qs(parsed.query or parsed.fragment)
    codes = qs.get("code", [])
    if not codes:
        raise ValueError(f"No 'code' parameter found in redirect URL: {value!r}")
    return codes[0]
