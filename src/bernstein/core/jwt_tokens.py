"""JWT session tokens with configurable expiry."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger(__name__)

_REFRESH_BUFFER_SECONDS: float = 300.0  # 5 minutes
_MAX_REFRESH_FAILURES: int = 3


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


class TokenRefreshFatalError(Exception):
    """Raised when max refresh failures are exceeded and the scheduler is dead."""


@dataclass
class TokenRefreshScheduler:
    """Proactively refresh a JWT token 5 minutes before expiry.

    Tracks a monotonic *generation* counter so that a refresh attempt which
    was issued against an already-superseded generation is silently dropped.
    After ``max_failures`` consecutive failures the scheduler enters a fatal
    state and all further calls to :meth:`refresh` raise
    :exc:`TokenRefreshFatalError`.

    Thread-safe: a single :class:`threading.Lock` serialises all mutations.
    """

    _manager: JWTManager
    _session_id: str
    _user_id: str | None = field(default=None)
    _scopes: list[str] = field(default_factory=list[str])
    _refresh_buffer: float = field(default=_REFRESH_BUFFER_SECONDS)
    _max_failures: int = field(default=_MAX_REFRESH_FAILURES)

    # mutable state — do not set externally
    _token: str = field(init=False, default="")
    _payload: JWTPayload = field(init=False)
    _generation: int = field(init=False, default=0)
    _fail_count: int = field(init=False, default=0)
    _lock: Lock = field(init=False, default_factory=Lock)

    def __post_init__(self) -> None:
        """Issue the initial token."""
        self._token, self._payload = self._issue()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def token(self) -> str:
        """Return the current JWT token string."""
        with self._lock:
            return self._token

    @property
    def generation(self) -> int:
        """Monotonic generation counter; increments on every successful refresh."""
        with self._lock:
            return self._generation

    def needs_refresh(self) -> bool:
        """Return True when the token is within the refresh buffer of expiry."""
        with self._lock:
            return self._payload.expires_at - time.time() <= self._refresh_buffer

    def is_fatal(self) -> bool:
        """Return True when the scheduler has exceeded max consecutive failures."""
        with self._lock:
            return self._fail_count >= self._max_failures

    def refresh(self, *, caller_generation: int | None = None) -> bool:
        """Attempt to issue a new token.

        If *caller_generation* is provided and no longer matches the current
        generation the call is a no-op (the token was already refreshed by
        another caller), returning ``True`` without incrementing the failure
        counter.

        Args:
            caller_generation: The generation value the caller observed before
                deciding to refresh.  Pass ``None`` to force a refresh
                regardless of generation.

        Returns:
            ``True`` if the token is now valid (either freshly issued or
            already refreshed by a concurrent caller). ``False`` if the
            attempt failed but the scheduler is still below ``max_failures``.

        Raises:
            TokenRefreshFatalError: When ``max_failures`` consecutive failures
                have occurred.
        """
        with self._lock:
            if self._fail_count >= self._max_failures:  # _is_fatal inline — no re-entrant lock
                raise TokenRefreshFatalError(
                    f"JWT refresh fatal: {self._fail_count} consecutive failures for session {self._session_id!r}"
                )

            # Stale-generation guard: another caller already refreshed.
            if caller_generation is not None and caller_generation != self._generation:
                logger.debug(
                    "JWT refresh skipped: caller_generation=%d current=%d",
                    caller_generation,
                    self._generation,
                )
                return True

            try:
                token, payload = self._issue()
            except Exception as exc:
                self._fail_count += 1
                logger.warning(
                    "JWT refresh failed (%d/%d): %s",
                    self._fail_count,
                    self._max_failures,
                    exc,
                )
                if self._fail_count >= self._max_failures:
                    raise TokenRefreshFatalError(
                        f"JWT refresh fatal after {self._fail_count} failures for session {self._session_id!r}"
                    ) from exc
                return False

            self._token = token
            self._payload = payload
            self._generation += 1
            self._fail_count = 0
            logger.debug(
                "JWT refreshed: session=%r generation=%d expires_at=%.0f",
                self._session_id,
                self._generation,
                self._payload.expires_at,
            )
            return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _issue(self) -> tuple[str, JWTPayload]:
        """Create a fresh token and return (token_str, payload)."""
        token = self._manager.create_token(
            session_id=self._session_id,
            user_id=self._user_id,
            scopes=list(self._scopes),
        )
        payload = self._manager.verify_token(token)
        if payload is None:
            raise ValueError("Newly created token failed verification")
        return token, payload
