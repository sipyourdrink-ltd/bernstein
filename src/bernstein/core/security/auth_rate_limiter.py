"""In-memory request rate limiting helpers for Bernstein."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of an endpoint rate-limit check."""

    bucket: str
    retry_after_seconds: float


class AuthRateLimiter:
    """Per-IP sliding-window rate limiter.

    Args:
        max_requests: Maximum requests allowed within the window.
        window_seconds: Size of the sliding window in seconds.
        cleanup_every: Purge expired entries every N calls to ``check``.
    """

    def __init__(
        self,
        max_requests: int = 10,
        window_seconds: int = 60,
        cleanup_every: int = 100,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.cleanup_every = cleanup_every
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._call_count = 0

    def check(self, ip: str, *, bucket: str = "auth") -> float | None:
        """Check whether *ip* is within the rate limit for a bucket.

        Returns ``None`` if the request is allowed, or the number of
        seconds until the next request slot opens (for ``Retry-After``).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Trim timestamps outside the window
        key = (bucket, ip)
        timestamps = self._hits[key]
        self._hits[key] = timestamps = [t for t in timestamps if t > cutoff]

        self._call_count += 1
        if self._call_count % self.cleanup_every == 0:
            self._cleanup(now)

        if len(timestamps) >= self.max_requests:
            # Earliest timestamp that counts — retry after it expires
            retry_after = timestamps[0] - cutoff
            return max(retry_after, 1.0)

        timestamps.append(now)
        return None

    def _cleanup(self, now: float) -> None:
        """Remove entries with no timestamps in the current window."""
        cutoff = now - self.window_seconds
        empty_keys = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for k in empty_keys:
            del self._hits[k]


class RequestRateLimiter:
    """Generic sliding-window rate limiter keyed by endpoint bucket and client."""

    def __init__(self, cleanup_every: int = 100) -> None:
        self._cleanup_every = cleanup_every
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._call_count = 0

    def check(self, bucket: str, client_id: str, requests: int, window_seconds: int) -> float | None:
        """Check whether a client is within the configured bucket limit."""
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (bucket, client_id)
        timestamps = self._hits[key]
        self._hits[key] = timestamps = [timestamp for timestamp in timestamps if timestamp > cutoff]
        self._call_count += 1
        if self._call_count % self._cleanup_every == 0:
            self._cleanup(now, window_seconds)
        if len(timestamps) >= requests:
            retry_after = timestamps[0] - cutoff
            return max(retry_after, 1.0)
        timestamps.append(now)
        return None

    def _cleanup(self, now: float, window_seconds: int) -> None:
        """Drop stale bucket/client counters."""
        cutoff = now - window_seconds
        empty_keys = [key for key, values in self._hits.items() if not values or values[-1] <= cutoff]
        for key in empty_keys:
            del self._hits[key]


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Default rate limits per HTTP method group (per minute)
DEFAULT_WRITE_RPM = 30
DEFAULT_READ_RPM = 300
DEFAULT_SSE_MAX_CONCURRENT = 10


class RequestRateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce configured per-endpoint request limits from ``bernstein.yaml``.

    When no seed_config bucket matches, applies sensible defaults:
    - POST/PUT/DELETE: 30 requests/minute per client
    - GET: 300 requests/minute per client
    - /events SSE: max 10 concurrent connections
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: RequestRateLimiter | None = None,
        *,
        write_rpm: int = DEFAULT_WRITE_RPM,
        read_rpm: int = DEFAULT_READ_RPM,
        sse_max_concurrent: int = DEFAULT_SSE_MAX_CONCURRENT,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter or RequestRateLimiter()
        self._write_rpm = write_rpm
        self._read_rpm = read_rpm
        self._sse_max_concurrent = sse_max_concurrent
        self._sse_connections: int = 0

    @property
    def sse_connections(self) -> int:
        """Current number of active SSE connections."""
        return self._sse_connections

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        path = request.url.path
        method = request.method.upper()

        # SSE concurrency limit for /events endpoints
        if path in ("/events", "/events/cost") and method == "GET":
            if self._sse_connections >= self._sse_max_concurrent:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Too many concurrent SSE connections",
                        "bucket": "sse",
                        "max_concurrent": self._sse_max_concurrent,
                    },
                    headers={"Retry-After": "5"},
                )
            self._sse_connections += 1
            try:
                return await call_next(request)
            finally:
                self._sse_connections = max(0, self._sse_connections - 1)

        # Exempt loopback clients (orchestrator, spawner, agents) from rate
        # limiting — they are internal components, not external callers.
        # Also exempt requests that carry the X-Bernstein-Internal header from
        # loopback — these are spawner/lifecycle calls (e.g. retry task
        # creation) that must never be 429'd even when X-Forwarded-For is set.
        direct_ip = request.client.host if request.client else "unknown"
        if direct_ip in _LOOPBACK_HOSTS and (
            not request.headers.get("X-Forwarded-For") or request.headers.get("X-Bernstein-Internal") == "true"
        ):
            return await call_next(request)

        # Try seed_config buckets first
        seed_config = getattr(request.app.state, "seed_config", None)
        rate_limit = getattr(seed_config, "rate_limit", None)
        if rate_limit is not None and hasattr(rate_limit, "match_request"):
            bucket = rate_limit.match_request(path, method)
            if bucket is not None:
                client_id = _request_client_id(request)
                retry_after = self._limiter.check(bucket.name, client_id, bucket.requests, bucket.window_seconds)
                if retry_after is not None:
                    retry_after_header = str(math.ceil(retry_after))
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": f"Rate limit exceeded for bucket '{bucket.name}'",
                            "bucket": bucket.name,
                        },
                        headers={"Retry-After": retry_after_header},
                    )
                return await call_next(request)

        # Default method-based rate limits
        client_id = _request_client_id(request)
        if method in _WRITE_METHODS:
            bucket_name = "default_write"
            rpm = self._write_rpm
        elif method in _READ_METHODS:
            bucket_name = "default_read"
            rpm = self._read_rpm
        else:
            return await call_next(request)

        retry_after = self._limiter.check(bucket_name, client_id, rpm, 60)
        if retry_after is not None:
            retry_after_header = str(math.ceil(retry_after))
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded for bucket '{bucket_name}'",
                    "bucket": bucket_name,
                },
                headers={"Retry-After": retry_after_header},
            )

        return await call_next(request)


def _request_client_id(request: Request) -> str:
    """Return a stable request client identifier.

    Trust forwarded headers only when the direct peer is local.
    """
    direct_client_ip = request.client.host if request.client else "unknown"
    if direct_client_ip in _LOOPBACK_HOSTS:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",", maxsplit=1)[0].strip()
    return direct_client_ip


# Shared instance used by the auth router dependency.
_auth_limiter = AuthRateLimiter()


def check_auth_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces the auth rate limit."""
    ip = request.client.host if request.client else "unknown"
    retry_after = _auth_limiter.check(ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(int(retry_after))},
        )
