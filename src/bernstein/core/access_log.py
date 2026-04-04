"""Structured API access logging for the task server."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.runtime_state import rotate_log_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)


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
    """Emit one structured JSONL access record for every API response."""

    def __init__(self, app: Any, *, log_path: Path) -> None:
        super().__init__(app)
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

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
        try:
            rotate_log_file(self._log_path)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("Failed to write access log %s: %s", self._log_path, exc)
        response.headers.setdefault("x-request-id", request_id)
        return response
