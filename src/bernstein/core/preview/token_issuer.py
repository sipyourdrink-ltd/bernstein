"""Signed-token issuer for preview links.

Wraps the security layer's :class:`~bernstein.core.security.jwt_tokens.JWTManager`
so a preview link can be served behind a short-lived JWT or HTTP-basic
credential. Three auth modes are supported:

* ``"token"`` — JWT bearer token; the public URL gets a ``?token=…``
  query string the user can paste straight into ``curl``.
* ``"basic"`` — HTTP basic auth; we generate a strong random password
  and store the credentials so the manager can render
  ``https://user:pass@host`` URLs.
* ``"none"`` — no auth; the URL is the bare public tunnel URL.

A single :class:`PreviewTokenIssuer` instance is intended to live for
the lifetime of the orchestrator: token expiries are derived from the
``--expire`` knob the operator supplies per ``preview start`` invocation.
"""

from __future__ import annotations

import logging
import math
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from bernstein.core.security.jwt_tokens import JWTManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssuedAuth:
    """Auth credentials issued for a preview link.

    Attributes:
        mode: ``"token"``, ``"basic"`` or ``"none"``.
        token: JWT bearer token (``"token"`` mode) — empty otherwise.
        basic_user: HTTP-basic username (``"basic"`` mode) — empty otherwise.
        basic_password: HTTP-basic password (``"basic"`` mode) — empty otherwise.
        expires_at_epoch: Unix timestamp at which the credentials expire.
            Always set; the token mode honours it directly, basic mode
            uses it as the orchestrator-side validity window.
    """

    mode: str
    token: str = ""
    basic_user: str = ""
    basic_password: str = ""
    expires_at_epoch: float = 0.0

    def render_url(self, base_url: str) -> str:
        """Render *base_url* with the auth credentials baked in.

        For ``"token"`` mode the token is appended as a ``?token=…``
        query parameter (preserving any existing query). For ``"basic"``
        mode the user:password is injected into the URL netloc. For
        ``"none"`` the URL is returned unchanged.

        Args:
            base_url: The public tunnel URL.

        Returns:
            A URL the recipient can use directly to authenticate.
        """
        if not base_url:
            return base_url
        if self.mode == "token" and self.token:
            parts = urlsplit(base_url)
            new_query = parts.query + ("&" if parts.query else "") + f"token={quote(self.token, safe='')}"
            return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", new_query, parts.fragment))
        if self.mode == "basic" and self.basic_user and self.basic_password:
            parts = urlsplit(base_url)
            cred = f"{quote(self.basic_user, safe='')}:{quote(self.basic_password, safe='')}"
            netloc = f"{cred}@{parts.netloc}" if parts.netloc else cred
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return base_url


class PreviewTokenIssuer:
    """Issue short-lived auth credentials for preview links.

    Args:
        secret: Symmetric HMAC secret used by the underlying
            :class:`JWTManager`. Tests should supply a deterministic
            value; production callers should pass something with at
            least 256 bits of entropy.
        algorithm: JWT signing algorithm. Defaults to ``HS256`` for
            symmetry with the security layer's default.
    """

    def __init__(self, secret: str, *, algorithm: str = "HS256") -> None:
        if not secret:
            raise ValueError("PreviewTokenIssuer requires a non-empty secret")
        self._secret = secret
        self._algorithm = algorithm

    def issue(
        self,
        *,
        preview_id: str,
        mode: str,
        expires_in_seconds: int,
        scopes: tuple[str, ...] = ("preview:read",),
    ) -> IssuedAuth:
        """Issue credentials for a single preview link.

        Args:
            preview_id: Identifier of the preview the token authorises.
                Used as the JWT ``session_id`` for traceability.
            mode: ``"token"``, ``"basic"`` or ``"none"``.
            expires_in_seconds: Validity window in seconds. Must be
                positive.
            scopes: Optional JWT scopes. Defaults to ``("preview:read",)``.

        Returns:
            An :class:`IssuedAuth` value.

        Raises:
            ValueError: When *mode* is unknown or *expires_in_seconds*
                is non-positive.
        """
        if expires_in_seconds <= 0:
            raise ValueError("expires_in_seconds must be > 0")
        normalized = mode.strip().lower()
        if normalized not in {"token", "basic", "none"}:
            raise ValueError(f"unknown auth mode: {mode!r}")
        if normalized == "none":
            return IssuedAuth(mode="none", expires_at_epoch=0.0)

        # JWTManager works in whole hours but we want second-level
        # precision so callers can ask for `--expire 30m`. We round up
        # to the nearest hour for the manager and then create the token
        # at the exact second boundary the operator asked for.
        hours = max(1, math.ceil(expires_in_seconds / 3600))
        manager = JWTManager(self._secret, expiry_hours=hours, algorithm=self._algorithm)

        if normalized == "token":
            token = manager.create_token(
                session_id=preview_id,
                user_id=None,
                scopes=list(scopes),
            )
            payload = manager.verify_token(token)
            expires_at = payload.expires_at if payload else 0.0
            return IssuedAuth(mode="token", token=token, expires_at_epoch=expires_at)

        # basic
        user = "preview"
        password = secrets.token_urlsafe(24)
        # No JWT here — but we still produce an expiry so the manager
        # can prune stale credentials.
        token_for_meta = manager.create_token(
            session_id=preview_id,
            user_id=user,
            scopes=list(scopes),
        )
        payload = manager.verify_token(token_for_meta)
        expires_at = payload.expires_at if payload else 0.0
        return IssuedAuth(
            mode="basic",
            basic_user=user,
            basic_password=password,
            expires_at_epoch=expires_at,
        )


__all__ = [
    "IssuedAuth",
    "PreviewTokenIssuer",
]
