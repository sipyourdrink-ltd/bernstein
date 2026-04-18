"""Structured API access logging for the task server."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.runtime_state import rotate_log_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)

# Rotation debouncing: check at most once per interval OR when the in-memory
# byte counter suggests we might be near the 10 MiB cap. A conservative
# threshold ensures ``os.stat`` is only called when a rotation is actually
# plausible, not per-request.
_ROTATE_CHECK_INTERVAL_SECONDS: float = 60.0
_ROTATE_BYTES_THRESHOLD: int = 10 * 1024 * 1024  # mirrors _LOG_ROTATE_BYTES


@dataclass(frozen=True)
class AccessLogEntry:
    """Structured record for a single API request.

    Args:
        timestamp: ISO8601 UTC timestamp for the response completion.
        request_id: Stable request identifier propagated through request.state.
        tenant_id: Tenant namespace for the request. Defaults to ``"default"``.
        actor: Authenticated actor label or ``"anonymous"``.
        method: HTTP method.
        path: Request URL path.
        status: HTTP status code.
        duration_ms: End-to-end request duration in milliseconds.
        remote_ip: Best-effort client IP.
        user_agent: Request user agent string.
    """

    timestamp: str
    request_id: str
    tenant_id: str
    actor: str
    method: str
    path: str
    status: int
    duration_ms: float
    remote_ip: str
    user_agent: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the entry to a JSON-safe mapping."""
        return asdict(self)


def extract_tenant_id(request: Request) -> str:
    """Return the tenant ID for *request*.

    Prefers an already-derived ``request.state.tenant_id`` value. Falls back to
    the ``X-Tenant-ID`` header, then finally ``"default"``.
    """

    state_tenant = getattr(request.state, "tenant_id", None)
    if isinstance(state_tenant, str) and state_tenant.strip():
        return state_tenant.strip()
    header_tenant = request.headers.get("x-tenant-id", "").strip()
    return header_tenant or "default"


def extract_request_actor(request: Request) -> str:
    """Return the best available actor label for *request*."""

    state_actor = getattr(request.state, "auth_actor", None)
    if isinstance(state_actor, str) and state_actor.strip():
        return state_actor.strip()
    auth_header = request.headers.get("authorization", "").strip()
    return "authenticated" if auth_header else "anonymous"


def extract_remote_ip(request: Request) -> str:
    """Return the best-effort client IP for *request*."""

    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


class StructuredAccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured JSONL access record for every API response.

    Performance notes (audit-080):
        Rotation is debounced — we call :func:`rotate_log_file` (which stats
        the file) at most once per :data:`_ROTATE_CHECK_INTERVAL_SECONDS`
        OR after cumulative in-memory byte writes cross
        :data:`_ROTATE_BYTES_THRESHOLD`. The append-mode file handle is kept
        open across requests; POSIX small-line appends are atomic so
        concurrent requests do not interleave within a single JSON line.
        Callers should invoke :meth:`aclose` on shutdown to flush and close
        the handle.
    """

    def __init__(
        self,
        app: Any,
        *,
        log_path: Path,
        rotate_interval_seconds: float = _ROTATE_CHECK_INTERVAL_SECONDS,
        rotate_bytes_threshold: int = _ROTATE_BYTES_THRESHOLD,
    ) -> None:
        super().__init__(app)
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_interval_seconds = rotate_interval_seconds
        self._rotate_bytes_threshold = rotate_bytes_threshold
        self._bytes_written: int = 0
        self._last_rotate_check: float = 0.0
        self._log_fh: IO[str] | None = None
        self._fh_lock = threading.Lock()

    def _ensure_handle(self) -> IO[str] | None:
        """Return a reusable append-mode handle, opening it on first use."""
        if self._log_fh is not None:
            return self._log_fh
        try:
            self._log_fh = self._log_path.open("a", encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to open access log %s: %s", self._log_path, exc)
            self._log_fh = None
        return self._log_fh

    def _maybe_rotate(self, now: float) -> None:
        """Call :func:`rotate_log_file` when the debounce policy allows it.

        The check fires when either enough wall-clock time has elapsed since
        the last probe, or the cumulative unflushed byte count has crossed
        the rotation threshold. On rotation, the persistent handle is closed
        and reopened so subsequent writes go to the fresh file.
        """
        first_call = self._last_rotate_check == 0.0
        time_elapsed = now - self._last_rotate_check >= self._rotate_interval_seconds
        size_exceeded = self._bytes_written >= self._rotate_bytes_threshold
        if not (first_call or time_elapsed or size_exceeded):
            return

        self._last_rotate_check = now
        try:
            rotated = rotate_log_file(self._log_path)
        except OSError as exc:  # defensive; rotate_log_file itself handles OSError
            logger.warning("Rotation probe failed for %s: %s", self._log_path, exc)
            return

        if rotated:
            # File was moved: close the stale handle so we reopen the fresh path.
            with self._fh_lock:
                if self._log_fh is not None:
                    try:
                        self._log_fh.close()
                    except OSError as exc:
                        logger.debug("Error closing rotated access log handle: %s", exc)
                    finally:
                        self._log_fh = None
            self._bytes_written = 0
        elif size_exceeded:
            # Threshold reached but no rotation (e.g. file shorter than thought):
            # reset counter so we don't probe every single subsequent request.
            self._bytes_written = 0

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        request_id = request.headers.get("x-request-id", "").strip() or uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.tenant_id = extract_tenant_id(request)
        started = time.perf_counter()
        response: StarletteResponse = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        entry = AccessLogEntry(
            timestamp=datetime.now(tz=UTC).isoformat(),
            request_id=request_id,
            tenant_id=str(request.state.tenant_id),
            actor=extract_request_actor(request),
            method=request.method,
            path=request.url.path,
            status=int(response.status_code),
            duration_ms=duration_ms,
            remote_ip=extract_remote_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
        payload = json.dumps(entry.to_dict(), sort_keys=True) + "\n"
        try:
            self._maybe_rotate(time.monotonic())
            with self._fh_lock:
                handle = self._ensure_handle()
                if handle is not None:
                    handle.write(payload)
                    handle.flush()
                    # POSIX small-line appends are atomic; tracking by encoded
                    # length gives a close-enough byte count for debounce.
                    self._bytes_written += len(payload.encode("utf-8"))
        except OSError as exc:
            logger.warning("Failed to write access log %s: %s", self._log_path, exc)
        response.headers.setdefault("x-request-id", request_id)
        return response

    async def aclose(self) -> None:
        """Close the persistent file handle on application shutdown."""
        with self._fh_lock:
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except OSError as exc:
                    logger.debug("Error closing access log handle: %s", exc)
                finally:
                    self._log_fh = None
