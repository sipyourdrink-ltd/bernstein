"""Inbound auth for the A2A JSON-RPC server surface (#2609).

A callable node advertises two auth schemes in its agent card and enforces
them on every JSON-RPC call:

* **API key** - a static per-caller secret sent as ``X-API-Key``. The header
  value maps to a named caller so the accepting path can anchor *who* called
  in the audit chain.
* **OAuth2 client-credentials** - a machine-to-machine grant. A client
  exchanges ``client_id`` / ``client_secret`` at the token endpoint for a
  short-lived bearer token; the node validates the token offline on each call.

Both mechanisms are declared in the card's ``securitySchemes`` so a peer
negotiates them before sending work, and rejections follow the wire shapes
the A2A spec inherits (HTTP 401 with an RFC 6750 ``WWW-Authenticate``
challenge; OAuth2 §5.2 ``invalid_client`` at the token endpoint).

Stateless by construction
-------------------------
Issued tokens carry their own claims and an HMAC tag; the node verifies them
without a session store, so tokens survive a restart and two processes behind
one config validate each other's tokens. When the operator does not pin a
signing secret, one is *derived deterministically* from the configured client
set, so a rebuilt authenticator - or a restarted process - validates tokens it
issued before. The secret never leaves the process and is never advertised.

This module is transport-agnostic: it takes a header mapping and returns an
:class:`AuthenticatedCaller` (or raises :class:`A2AAuthError`), which the
FastAPI route in :mod:`bernstein.core.routes.a2a_jsonrpc` translates to a
response. Keeping it pure makes the reject-per-spec behaviour unit-testable
without standing up a server.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "A2A_API_KEY_HEADER",
    "A2AAuthError",
    "A2AServerAuth",
    "AuthenticatedCaller",
    "IssuedToken",
]

#: Header carrying the static API key. Named in the advertised card scheme so
#: a peer knows exactly where to place it.
A2A_API_KEY_HEADER = "X-API-Key"

#: Environment configuration.
_API_KEYS_ENV = "BERNSTEIN_A2A_API_KEYS"
_OAUTH_CLIENTS_ENV = "BERNSTEIN_A2A_OAUTH_CLIENTS"
_SIGNING_SECRET_ENV = "BERNSTEIN_A2A_OAUTH_SIGNING_SECRET"

#: Client-credentials token lifetime (seconds).
_TOKEN_TTL_SECONDS = 3600

#: Scheme ids advertised in the card. Stable so a cached card keeps resolving.
_API_KEY_SCHEME_ID = "a2a-api-key"
_OAUTH2_SCHEME_ID = "a2a-oauth2"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    """The identity behind an authenticated A2A request.

    Attributes:
        caller_id: Stable name of the caller - the API-key owner or the OAuth2
            ``client_id``. Recorded in the audit chain on the accepting path.
        scheme: ``"apiKey"`` or ``"oauth2"``.
    """

    caller_id: str
    scheme: str


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A client-credentials access token, shaped as an OAuth2 token response."""

    access_token: str
    token_type: str
    expires_in: int

    def to_response(self) -> dict[str, Any]:
        """Return the RFC 6749 §5.1 token-endpoint success body."""
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }


class A2AAuthError(Exception):
    """A rejected A2A request, carrying the wire shape of the rejection.

    Attributes:
        status_code: HTTP status the route should return (401 for the RPC
            surface, 400 for the token endpoint).
        error: OAuth2 error code (e.g. ``invalid_client``, ``invalid_token``).
        headers: Extra response headers, notably ``WWW-Authenticate``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 401,
        error: str = "invalid_token",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error = error
        self.headers = headers or {}


def _parse_pairs(raw: str) -> dict[str, str]:
    """Parse ``a=1, b=2`` into a mapping, tolerating whitespace and blanks."""
    pairs: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip()
        if name and value:
            pairs[name] = value
    return pairs


@dataclass
class A2AServerAuth:
    """Authenticator for the inbound A2A server surface.

    Args:
        api_keys: ``caller_id -> api_key`` map. A request presenting a known
            key authenticates as the mapped caller.
        oauth_clients: ``client_id -> client_secret`` map for the
            client-credentials grant.
        signing_secret: HMAC key for issued tokens. When empty, a deterministic
            secret is derived from ``oauth_clients`` so a rebuilt authenticator
            validates previously issued tokens.
    """

    api_keys: dict[str, str] = field(default_factory=dict)
    oauth_clients: dict[str, str] = field(default_factory=dict)
    signing_secret: bytes = b""

    def __post_init__(self) -> None:
        if not self.signing_secret:
            self.signing_secret = self._derive_secret(self.oauth_clients)

    @staticmethod
    def _derive_secret(oauth_clients: dict[str, str]) -> bytes:
        """Derive a stable HMAC key from the client set.

        Deterministic so a restart or a second process behind the same config
        validates tokens issued earlier, without persisting a secret. The
        client secrets are already private, so folding them into the key adds
        no new exposure and binds token validity to the configured clients.
        """
        material = "\x00".join(f"{cid}={sec}" for cid, sec in sorted(oauth_clients.items()))
        return hashlib.sha256(b"bernstein-a2a-oauth\x00" + material.encode("utf-8")).digest()

    @property
    def is_configured(self) -> bool:
        """Return ``True`` when at least one credential is configured."""
        return bool(self.api_keys or self.oauth_clients)

    # -- Classmethod constructor -----------------------------------------

    @classmethod
    def from_env(cls) -> A2AServerAuth:
        """Build an authenticator from ``BERNSTEIN_A2A_*`` environment vars."""
        api_keys = _parse_pairs(os.environ.get(_API_KEYS_ENV, ""))
        oauth_clients = _parse_pairs(os.environ.get(_OAUTH_CLIENTS_ENV, ""))
        secret_raw = os.environ.get(_SIGNING_SECRET_ENV, "").strip()
        signing_secret = secret_raw.encode("utf-8") if secret_raw else b""
        return cls(api_keys=api_keys, oauth_clients=oauth_clients, signing_secret=signing_secret)

    # -- Request authentication ------------------------------------------

    def authenticate(self, headers: dict[str, str], *, now: float | None = None) -> AuthenticatedCaller:
        """Authenticate a request from its headers, or raise.

        Header lookups are case-insensitive. The API key is checked first
        (cheapest), then the bearer token. A request that presents neither is
        rejected with an RFC 6750 challenge naming both accepted schemes.

        Raises:
            A2AAuthError: On missing or invalid credentials.
        """
        lower = {k.lower(): v for k, v in headers.items()}

        api_key = lower.get(A2A_API_KEY_HEADER.lower())
        if api_key is not None:
            caller = self._match_api_key(api_key)
            if caller is None:
                raise A2AAuthError("invalid API key", status_code=401, headers=self._challenge())
            return AuthenticatedCaller(caller_id=caller, scheme="apiKey")

        auth_header = lower.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()
            return self._verify_token(token, now=self._resolve_now(now))

        raise A2AAuthError(
            "missing credentials: present an X-API-Key header or an OAuth2 bearer token",
            status_code=401,
            error="invalid_request",
            headers=self._challenge(),
        )

    def _match_api_key(self, presented: str) -> str | None:
        """Return the caller id for a presented key, in constant time."""
        for caller_id, key in self.api_keys.items():
            if hmac.compare_digest(key, presented):
                return caller_id
        return None

    # -- Client-credentials grant ----------------------------------------

    def issue_client_credentials_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        now: float | None = None,
    ) -> IssuedToken:
        """Issue a bearer token for a valid client, or raise ``invalid_client``.

        The token is a self-describing ``<payload>.<mac>`` string: the payload
        names the client and an absolute expiry, and the MAC binds both to the
        node's signing secret. Verification is offline and stateless.
        """
        expected = self.oauth_clients.get(client_id)
        if expected is None or not hmac.compare_digest(expected, client_secret):
            raise A2AAuthError(
                "unknown client or bad secret",
                status_code=401,
                error="invalid_client",
                headers={"WWW-Authenticate": 'Basic realm="a2a"'},
            )
        issued = int(self._resolve_now(now))
        payload = {"sub": client_id, "exp": issued + _TOKEN_TTL_SECONDS, "iat": issued}
        access_token = self._encode_token(payload)
        return IssuedToken(access_token=access_token, token_type="Bearer", expires_in=_TOKEN_TTL_SECONDS)

    def _encode_token(self, payload: dict[str, Any]) -> str:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_seg = _b64url(payload_bytes)
        mac = hmac.new(self.signing_secret, payload_seg.encode("ascii"), hashlib.sha256).digest()
        return f"{payload_seg}.{_b64url(mac)}"

    def _verify_token(self, token: str, *, now: float) -> AuthenticatedCaller:
        parts = token.split(".")
        if len(parts) != 2:
            raise A2AAuthError("malformed bearer token", status_code=401, headers=self._challenge())
        payload_seg, mac_seg = parts
        expected_mac = hmac.new(self.signing_secret, payload_seg.encode("ascii"), hashlib.sha256).digest()
        try:
            presented_mac = _b64url_decode(mac_seg)
        except (ValueError, binascii.Error):
            raise A2AAuthError("malformed bearer token", status_code=401, headers=self._challenge()) from None
        if not hmac.compare_digest(expected_mac, presented_mac):
            raise A2AAuthError("bad token signature", status_code=401, headers=self._challenge())
        try:
            payload = json.loads(_b64url_decode(payload_seg))
        except (ValueError, binascii.Error):
            raise A2AAuthError("malformed bearer token", status_code=401, headers=self._challenge()) from None
        if not isinstance(payload, dict):
            raise A2AAuthError("malformed bearer token", status_code=401, headers=self._challenge())
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or now > exp:
            raise A2AAuthError("token expired", status_code=401, headers=self._challenge())
        client_id = payload.get("sub")
        if not isinstance(client_id, str) or client_id not in self.oauth_clients:
            raise A2AAuthError("token subject is not a known client", status_code=401, headers=self._challenge())
        return AuthenticatedCaller(caller_id=client_id, scheme="oauth2")

    @staticmethod
    def _resolve_now(now: float | None) -> float:
        return time.time() if now is None else now

    @staticmethod
    def _challenge() -> dict[str, str]:
        """RFC 6750 challenge naming the two accepted schemes."""
        return {"WWW-Authenticate": 'Bearer realm="a2a", error="invalid_token"'}

    # -- Card advertisement ----------------------------------------------

    def security_schemes(self, *, token_url: str) -> list[dict[str, Any]]:
        """Return the A2A card ``securitySchemes`` entries for both mechanisms.

        Args:
            token_url: Absolute URL of the client-credentials token endpoint.
        """
        return [
            {
                "id": _API_KEY_SCHEME_ID,
                "type": "apiKey",
                "in": "header",
                "name": A2A_API_KEY_HEADER,
                "description": "Static per-caller API key.",
            },
            {
                "id": _OAUTH2_SCHEME_ID,
                "type": "oauth2",
                "description": "OAuth2 client-credentials grant for machine callers.",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": token_url,
                        "scopes": {},
                    }
                },
            },
        ]
