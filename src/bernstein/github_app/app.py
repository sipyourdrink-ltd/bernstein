"""GitHub App authentication: JWT creation and installation token exchange.

Implements minimal JWT signing with stdlib only (no PyJWT dependency).
GitHub App authentication uses RS256 JWTs, but since we only need to
create JWTs (not verify them), we use the ``cryptography`` approach
avoided here in favour of stdlib-only HMAC for webhook verification.

For installation tokens, we shell out to ``gh`` CLI or use httpx if
available, keeping dependencies minimal.

Note: Full RS256 JWT signing requires an RSA private key operation that
cannot be done with stdlib ``hmac`` alone.  This module provides the
JWT assembly and a hook for signing, plus a practical ``gh``-based
fallback for obtaining installation tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubAppConfig:
    """Configuration for a GitHub App installation.

    Attributes:
        app_id: The GitHub App ID.
        private_key: PEM-encoded RSA private key for JWT signing.
        webhook_secret: Shared secret for webhook HMAC verification.
    """

    app_id: str
    private_key: str
    webhook_secret: str

    @classmethod
    def from_env(cls) -> GitHubAppConfig:
        """Read configuration from environment variables.

        Expected variables:
            - ``GITHUB_APP_ID``
            - ``GITHUB_APP_PRIVATE_KEY`` (PEM string or path to PEM file)
            - ``GITHUB_WEBHOOK_SECRET``

        Returns:
            Populated ``GitHubAppConfig``.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        app_id = os.environ.get("GITHUB_APP_ID", "")
        if not app_id:
            msg = "GITHUB_APP_ID environment variable is required"
            raise ValueError(msg)

        private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
        if not private_key:
            msg = "GITHUB_APP_PRIVATE_KEY environment variable is required"
            raise ValueError(msg)

        # Support file path for the private key
        if not private_key.startswith("-----") and os.path.isfile(private_key):
            with open(private_key) as f:
                private_key = f.read()

        webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not webhook_secret:
            msg = "GITHUB_WEBHOOK_SECRET environment variable is required"
            raise ValueError(msg)

        return cls(
            app_id=app_id,
            private_key=private_key,
            webhook_secret=webhook_secret,
        )


def _base64url_encode(data: bytes) -> str:
    """Base64url-encode bytes without padding (per RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _build_jwt_parts(app_id: str, now: float | None = None) -> tuple[str, str]:
    """Build the unsigned JWT header.payload string for GitHub App auth.

    Args:
        app_id: The GitHub App ID (used as the ``iss`` claim).
        now: Current timestamp override for testing.

    Returns:
        Tuple of (header_b64, payload_b64) ready for signing.
    """
    if now is None:
        now = time.time()

    header: dict[str, str] = {"alg": "RS256", "typ": "JWT"}
    payload: dict[str, Any] = {
        "iat": int(now) - 60,  # Issued 60s ago to account for clock drift
        "exp": int(now) + 600,  # Expires in 10 minutes (GitHub max)
        "iss": app_id,
    }

    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    return header_b64, payload_b64


def _sign_rs256_or_fallback(message: str, private_key_pem: str) -> bytes:
    """Sign a JWT message using RS256, falling back to HMAC-SHA256.

    Uses ``cryptography`` for real RS256 signing if available.  Otherwise
    falls back to HMAC-SHA256 (which GitHub will reject, but allows
    testing the JWT structure).
    """
    try:
        import importlib

        _primitives: Any = importlib.import_module("cryptography.hazmat.primitives")
        _asymmetric: Any = importlib.import_module("cryptography.hazmat.primitives.asymmetric")

        private_key: Any = _primitives.serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        result: bytes = private_key.sign(
            message.encode("utf-8"),
            _asymmetric.padding.PKCS1v15(),
            _primitives.hashes.SHA256(),
        )
        return result
    except ImportError:
        # Fallback: HMAC-SHA256 for testing (GitHub will reject this)
        logger.warning(
            "cryptography package not available; using HMAC-SHA256 fallback (JWT will not be accepted by GitHub)"
        )
        return hmac.new(
            private_key_pem.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()


def create_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """Create a signed JWT for GitHub App authentication.

    This uses RS256 (RSA + SHA-256) as required by GitHub.  Since stdlib
    does not support RSA signing natively, we attempt to import
    ``cryptography`` for the actual signing.  If unavailable, falls back
    to an HMAC-SHA256 placeholder (which GitHub will reject, but allows
    testing the JWT structure).

    Args:
        app_id: The GitHub App ID.
        private_key_pem: PEM-encoded RSA private key.
        now: Current timestamp override for testing.

    Returns:
        Signed JWT string.
    """
    header_b64, payload_b64 = _build_jwt_parts(app_id, now)
    message = f"{header_b64}.{payload_b64}"

    signature = _sign_rs256_or_fallback(message, private_key_pem)
    signature_b64 = _base64url_encode(signature)
    return f"{message}.{signature_b64}"


def create_installation_token(config: GitHubAppConfig, installation_id: int) -> str:
    """Create a GitHub installation access token.

    Tries the ``gh`` CLI first (simplest, no extra deps), then falls back
    to direct API call using the JWT.

    Args:
        config: GitHub App configuration.
        installation_id: The GitHub App installation ID for the target repo.

    Returns:
        Installation access token string.

    Raises:
        RuntimeError: If token creation fails via all methods.
    """
    # Method 1: Try gh CLI (works if the app is installed and gh is authed)
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"/app/installations/{installation_id}/access_tokens",
                "--method",
                "POST",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            data: dict[str, Any] = json.loads(result.stdout)
            token = data.get("token", "")
            if token:
                logger.info("Created installation token via gh CLI")
                return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.debug("gh CLI not available, falling back to JWT method")

    # Method 2: Use JWT + httpx
    jwt = create_jwt(config.app_id, config.private_key)
    try:
        import httpx

        response = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token", "")
        if token:
            logger.info("Created installation token via JWT + httpx")
            return token
    except Exception as exc:
        logger.warning("JWT-based token creation failed: %s", exc)

    msg = f"Failed to create installation token for installation {installation_id}"
    raise RuntimeError(msg)
