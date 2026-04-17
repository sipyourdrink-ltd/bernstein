"""In-memory request rate limiting helpers for Bernstein.

Rate-limit buckets are keyed by the REAL TCP peer IP (``request.client.host``)
by default. Client-supplied headers such as ``X-Forwarded-For`` are **ignored**
unless upstream proxies are explicitly trusted via the
``BERNSTEIN_TRUSTED_PROXY_IPS`` environment variable (comma-separated list of
proxy IPs). When trusted, the limiter walks the ``X-Forwarded-For`` chain from
right to left and uses the right-most IP that is NOT a trusted proxy — i.e.
the closest original client address that the trusted proxy chain forwarded
for us.

Historically this module also honoured an ``X-Bernstein-Internal: true``
header to bypass rate limiting for loopback callers. That header was
attacker-controllable behind a reverse proxy on the same host and has been
removed. Internal callers must now reach the server from loopback WITHOUT
sending ``X-Forwarded-For``; proxied traffic is always rate-limited.
"""

from __future__ import annotations

import math
import os
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

# Environment variable used to declare IP addresses of reverse proxies whose
# ``X-Forwarded-For`` header we trust. Comma-separated list, e.g.
# ``BERNSTEIN_TRUSTED_PROXY_IPS=10.0.0.1,10.0.0.2``. Loopback (``127.0.0.1``,
# ``::1``) is NEVER implicitly trusted — an operator must opt in explicitly
# if they terminate a reverse proxy on the same host.
_TRUSTED_PROXY_ENV = "BERNSTEIN_TRUSTED_PROXY_IPS"


def _trusted_proxies() -> frozenset[str]:
    """Return the configured set of trusted-proxy peer IPs.

    Read lazily from the environment on every call so tests (and config
    reloads) can change the value without restarting the process.
    """
    raw = os.environ.get(_TRUSTED_PROXY_ENV, "")
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


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

    Clients are identified by the real TCP peer (``request.client.host``).
    ``X-Forwarded-For`` is only consulted when the direct peer itself is a
    trusted proxy declared via ``BERNSTEIN_TRUSTED_PROXY_IPS``.
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
        # The exemption applies ONLY when the TCP peer is loopback AND no
        # X-Forwarded-For header is present. If a request arrives on
        # loopback but carries XFF, it came through a local reverse proxy
        # and must be rate-limited like any other external caller.
        direct_ip = request.client.host if request.client else "unknown"
        if direct_ip in _LOOPBACK_HOSTS and not request.headers.get("X-Forwarded-For"):
            return await call_next(request)

        # Try seed_config buckets first
        seed_result = self._check_seed_bucket(request, path, method)
        if seed_result is not None:
            if isinstance(seed_result, JSONResponse):
                return seed_result
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

    def _check_seed_bucket(
        self,
        request: Request,
        path: str,
        method: str,
    ) -> JSONResponse | bool | None:
        """Check seed_config rate limit buckets. Returns JSONResponse, True, or None."""
        seed_config = getattr(request.app.state, "seed_config", None)
        rate_limit = getattr(seed_config, "rate_limit", None)
        if rate_limit is None or not hasattr(rate_limit, "match_request"):
            return None
        bucket = rate_limit.match_request(path, method)
        if bucket is None:
            return None
        client_id = _request_client_id(request)
        retry_after = self._limiter.check(bucket.name, client_id, bucket.requests, bucket.window_seconds)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded for bucket '{bucket.name}'",
                    "bucket": bucket.name,
                },
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
        return True


def _request_client_id(request: Request) -> str:
    """Return a stable rate-limit key for *request*.

    Default: the real TCP peer IP (``request.client.host``). Client-supplied
    headers are IGNORED — rotating ``X-Forwarded-For`` values must never let
    an attacker create unbounded buckets.

    Opt-in proxy mode: if ``BERNSTEIN_TRUSTED_PROXY_IPS`` is set and the
    direct peer IP is in that set, walk the ``X-Forwarded-For`` chain from
    right to left and return the right-most IP that is NOT itself a trusted
    proxy (i.e. the closest original client).
    """
    direct_client_ip = request.client.host if request.client else "unknown"
    trusted = _trusted_proxies()
    if direct_client_ip not in trusted:
        return direct_client_ip

    forwarded_for = request.headers.get("X-Forwarded-For")
    if not forwarded_for:
        return direct_client_ip
    # XFF is ordered: "client, proxy1, proxy2". Walk right-to-left past
    # trusted proxies and return the first non-trusted hop.
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    # Every hop was a trusted proxy — fall back to the direct peer.
    return direct_client_ip


# Shared instance used by the auth router dependency.
_auth_limiter = AuthRateLimiter()


def check_auth_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces the auth rate limit.

    Keys the bucket on the same peer identity used by the request middleware:
    the real TCP peer IP by default, or the right-most non-trusted hop from
    ``X-Forwarded-For`` when the direct peer is a configured trusted proxy.
    """
    ip = _request_client_id(request)
    retry_after = _auth_limiter.check(ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(int(retry_after))},
        )
