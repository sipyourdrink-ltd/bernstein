"""JWT session tokens with configurable expiry."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass


@dataclass
class JWTPayload:
    """JWT token payload."""

    session_id: str
    user_id: str | None
    issued_at: float
    expires_at: float
    scopes: list[str]


class JWTManager:
    """Manage JWT session tokens."""

    def __init__(
        self,
        secret: str,
        expiry_hours: int = 24,
        algorithm: str = "HS256",
    ) -> None:
        """Initialize JWT manager.

        Args:
            secret: Secret key for signing tokens.
            expiry_hours: Token expiry in hours (default 24).
            algorithm: Signing algorithm (default HS256).
        """
        self._secret = secret.encode("utf-8")
        self._expiry_seconds = expiry_hours * 3600
        self._algorithm = algorithm

    def create_token(
        self,
        session_id: str,
        user_id: str | None = None,
        scopes: list[str] | None = None,
    ) -> str:
        """Create a new JWT token.

        Args:
            session_id: Session identifier.
            user_id: Optional user identifier.
            scopes: Optional list of scopes.

        Returns:
            Signed JWT token string.
        """
        now = time.time()
        payload = JWTPayload(
            session_id=session_id,
            user_id=user_id,
            issued_at=now,
            expires_at=now + self._expiry_seconds,
            scopes=scopes or [],
        )

        return self._encode(payload)

    def verify_token(self, token: str) -> JWTPayload | None:
        """Verify and decode a JWT token.

        Args:
            token: JWT token string.

        Returns:
            JWTPayload if valid, None if invalid or expired.
        """
        try:
            payload = self._decode(token)
            if payload.expires_at < time.time():
                return None
            return payload
        except Exception:
            return None

    def _encode(self, payload: JWTPayload) -> str:
        """Encode payload to JWT token.

        Args:
            payload: JWT payload.

        Returns:
            Signed JWT token string.
        """
        # Header
        header = {"alg": self._algorithm, "typ": "JWT"}

        # Payload
        payload_dict = {
            "session_id": payload.session_id,
            "user_id": payload.user_id,
            "iat": payload.issued_at,
            "exp": payload.expires_at,
            "scopes": payload.scopes,
        }

        # Encode header and payload
        header_b64 = self._base64url_encode(json.dumps(header).encode())
        payload_b64 = self._base64url_encode(json.dumps(payload_dict).encode())

        # Create signature
        message = f"{header_b64}.{payload_b64}"
        signature = hmac.new(self._secret, message.encode(), hashlib.sha256).digest()
        signature_b64 = self._base64url_encode(signature)

        return f"{header_b64}.{payload_b64}.{signature_b64}"

    def _decode(self, token: str) -> JWTPayload:
        """Decode JWT token to payload.

        Args:
            token: JWT token string.

        Returns:
            Decoded JWTPayload.

        Raises:
            ValueError: If token is invalid.
        """
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token format")

        header_b64, payload_b64, signature_b64 = parts

        # Verify signature
        message = f"{header_b64}.{payload_b64}"
        expected_signature = hmac.new(self._secret, message.encode(), hashlib.sha256).digest()
        actual_signature = self._base64url_decode(signature_b64)

        if not hmac.compare_digest(expected_signature, actual_signature):
            raise ValueError("Invalid signature")

        # Decode payload
        payload_dict = json.loads(self._base64url_decode(payload_b64))

        return JWTPayload(
            session_id=payload_dict["session_id"],
            user_id=payload_dict.get("user_id"),
            issued_at=payload_dict["iat"],
            expires_at=payload_dict["exp"],
            scopes=payload_dict.get("scopes", []),
        )

    def _base64url_encode(self, data: bytes) -> str:
        """Base64url encode data."""
        import base64

        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

    def _base64url_decode(self, data: str) -> bytes:
        """Base64url decode data."""
        import base64

        # Add padding if needed
        padding = 4 - len(data) % 4
        if padding != 4:
            data += "=" * padding
        return base64.urlsafe_b64decode(data)
