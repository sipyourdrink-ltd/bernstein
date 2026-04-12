"""Shared HMAC-SHA256 helpers for inbound webhook verification."""

from __future__ import annotations

import hashlib
import hmac
import re

_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sign_hmac_sha256(secret: str, payload: bytes, *, prefix: str = "sha256=") -> str:
    """Return an HMAC-SHA256 signature string for a webhook payload.

    Args:
        secret: Shared webhook secret.
        payload: Raw bytes to sign.
        prefix: Optional signature prefix such as ``"sha256="`` or ``"v0="``.

    Returns:
        The prefixed hexadecimal HMAC digest.
    """

    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


def verify_hmac_sha256(payload: bytes, signature: str, secret: str, *, prefix: str = "sha256=") -> bool:
    """Verify a prefixed HMAC-SHA256 webhook signature.

    Args:
        payload: Raw bytes that were signed.
        signature: Signature header value received from the sender.
        secret: Shared webhook secret.
        prefix: Required signature prefix, for example ``"sha256="``.

    Returns:
        ``True`` when the signature matches, otherwise ``False``.
    """

    if not secret:
        return False

    normalized = signature.strip().lower()
    if prefix:
        if not normalized.startswith(prefix.lower()):
            return False
        normalized = normalized[len(prefix) :]

    if not _HEX_SHA256_RE.fullmatch(normalized):
        return False

    expected = sign_hmac_sha256(secret, payload, prefix="")
    return hmac.compare_digest(expected, normalized)
