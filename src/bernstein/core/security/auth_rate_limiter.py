"""In-memory request rate limiting helpers for Bernstein.

Rate-limit buckets are keyed by the REAL TCP peer IP (``request.client.host``)
by default. Client-supplied headers such as ``X-Forwarded-For`` are **ignored**
unless upstream proxies are explicitly trusted via the
``BERNSTEIN_TRUSTED_PROXY_IPS`` environment variable (comma-separated list of
proxy IPs). When trusted, the limiter walks the ``X-Forwarded-For`` chain from
right to left and uses the right-most IP that is NOT a trusted proxy - i.e.
the closest original client address that the trusted proxy chain forwarded
for us.

Historically this module also honoured an ``X-Bernstein-Internal: true``
header to bypass rate limiting for loopback callers. That header was
attacker-controllable behind a reverse proxy on the same host and has been
removed. Internal callers must now reach the server from loopback WITHOUT
sending ``X-Forwarded-For``; proxied traffic is always rate-limited.
"""

from __future__ import annotations

import logging
import math
import os
import re
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
    from starlette.types import ASGIApp, Receive, Scope, Send

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Environment variable used to declare IP addresses of reverse proxies whose
# ``X-Forwarded-For`` header we trust. Comma-separated list, e.g.
# ``BERNSTEIN_TRUSTED_PROXY_IPS=10.0.0.1,10.0.0.2``. Loopback (``127.0.0.1``,
# ``::1``) is NEVER implicitly trusted - an operator must opt in explicitly
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
    """Per-client sliding-window attempt limiter with exponential lockout.

    Exhausted windows start a lockout of one window, doubling on subsequent
    exhaustion up to 15 minutes (or one window when configured longer).
    Failed credentials can also start a backoff of 1, 2, 4, ... seconds.
    Backoff history expires 15 minutes after the lockout ends.

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
        self._windows: dict[tuple[str, str], int] = {}
        self._backoff: dict[tuple[str, str], tuple[int, float]] = {}

    def check(
        self,
        ip: str,
        *,
        bucket: str = "auth",
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> float | None:
        """Check whether *ip* is within the rate limit for a bucket.

        Returns ``None`` if the request is allowed, or the number of
        seconds until the next request slot opens (for ``Retry-After``).
        """
        now = time.monotonic()
        self._call_count += 1
        if self._call_count % self.cleanup_every == 0:
            self._cleanup(now)

        key = (bucket, ip)
        window = self.window_seconds if window_seconds is None else window_seconds
        limit = self.max_requests if max_requests is None else max_requests
        strikes, until = self._backoff.get(key, (0, 0.0))
        if strikes and now < until:
            return max(until - now, 1.0)

        self._windows[key] = window
        cutoff = now - window
        timestamps = [t for t in self._hits[key] if t > cutoff]
        self._hits[key] = timestamps
        if len(timestamps) >= limit:
            return self.record_failure(ip, bucket=bucket, base_delay=window)

        timestamps.append(now)
        return None

    def record_failure(self, ip: str, *, bucket: str = "auth", base_delay: float = 1.0) -> float:
        """Start a bounded exponential backoff for this client and bucket."""
        now = time.monotonic()
        key = (bucket, ip)
        strikes, until = self._backoff.get(key, (0, 0.0))
        if now >= until + 900:
            strikes = 0
        delay = min(base_delay * 2 ** min(strikes, 10), max(900.0, base_delay))
        self._backoff[key] = (min(strikes + 1, 10), now + delay)
        return delay

    def record_success(self, ip: str, *, bucket: str = "auth") -> None:
        """Clear only this client's backoff; retain its attempt counter."""
        self._backoff.pop((bucket, ip), None)

    def _cleanup(self, now: float) -> None:
        """Remove expired counters and idle backoff history."""
        for key, (_, until) in list(self._backoff.items()):
            if now >= until + 900:
                del self._backoff[key]
        for key, timestamps in list(self._hits.items()):
            window = self._windows.get(key, self.window_seconds)
            if not timestamps or timestamps[-1] <= now - window:
                del self._hits[key]
                self._windows.pop(key, None)


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
#: SSE endpoints that share the concurrent-connection cap.
_SSE_LIMITED_PATHS = frozenset({"/events", "/events/cost", "/events/thread"})
DEFAULT_HEARTBEAT_RPM = 600

_WRITE_RPM_ENV = "BERNSTEIN_RATE_LIMIT_WRITE_RPM"
_READ_RPM_ENV = "BERNSTEIN_RATE_LIMIT_READ_RPM"
_HEARTBEAT_RPM_ENV = "BERNSTEIN_RATE_LIMIT_HEARTBEAT_RPM"

# Header that a proxied worker sets to identify itself. When the request also
# carries a cluster bearer token, rate-limit buckets are keyed by this id
# rather than the peer IP. This lets many workers behind one NAT/proxy each
# get their own per-minute budget (#6609).
_AGENT_ID_HEADER = "x-bernstein-agent-id"

# Maximum length accepted for an agent-id header value. IDs longer than this
# are rejected to prevent bucket-key inflation attacks.
_AGENT_ID_MAX_LEN = 128

logger = logging.getLogger(__name__)

# Heartbeat routes get their own bucket keyed by the node/agent id in the
# path, so many workers behind one proxy IP do not share a single budget.
_HEARTBEAT_PATH_RE = re.compile(r"^(?:/api/v1)?/(?:cluster/nodes|agents)/(?P<id>[^/]+)/heartbeat/?$")


def _env_rpm(name: str, default: int) -> int:
    """Read a positive integer requests-per-minute value from the environment.

    Missing, non-integer or non-positive values fall back to *default*.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r; using default %d", name, raw, default)
        return default
    return value


def _agent_bucket_key(request: Request) -> str | None:
    """Return ``"agent:<id>"`` when the request should be keyed by agent id.

    A proxied worker may sit behind a NAT or load-balancer where many workers
    share one peer IP.  When a request carries both a cluster bearer token
    *and* a well-formed ``x-bernstein-agent-id`` header we switch the bucket
    key from the peer IP to the agent id so each worker gets its own budget.

    Anonymous requests and those without the header keep the peer-IP key,
    preserving the existing behaviour and preventing attacker-controlled
    headers from influencing the rate-limit key.

    Returns ``None`` when the request should fall back to the peer-IP key.
    """
    auth = request.headers.get("Authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    agent_id = request.headers.get(_AGENT_ID_HEADER, "").strip()
    if not agent_id or len(agent_id) > _AGENT_ID_MAX_LEN:
        return None
    return f"agent:{agent_id}"


def _heartbeat_key(request: Request, path: str, method: str) -> str | None:
    """Return the heartbeat bucket key for *request*, or ``None`` if not a heartbeat.

    The node/agent id from the path is used as the key only when the request
    carries credentials (``Authorization`` header). Anonymous requests keep
    the peer-IP key so rotating ids cannot mint unbounded buckets.
    """
    if method != "POST":
        return None
    match = _HEARTBEAT_PATH_RE.match(path)
    if match is None:
        return None
    if request.headers.get("Authorization"):
        return f"id:{match.group('id')}"
    return f"ip:{_request_client_id(request)}"


class RequestRateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce configured per-endpoint request limits from ``bernstein.yaml``.

    When no seed_config bucket matches, applies sensible defaults:
    - POST/PUT/DELETE: 30 requests/minute per client
    - GET: 300 requests/minute per client
    - POST heartbeat routes: 600 requests/minute per node/agent id
      (authenticated) or per client (anonymous)

    Defaults are overridable via ``BERNSTEIN_RATE_LIMIT_WRITE_RPM``,
    ``BERNSTEIN_RATE_LIMIT_READ_RPM`` and ``BERNSTEIN_RATE_LIMIT_HEARTBEAT_RPM``.
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
        write_rpm: int | None = None,
        read_rpm: int | None = None,
        sse_max_concurrent: int = DEFAULT_SSE_MAX_CONCURRENT,
        heartbeat_rpm: int | None = None,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter or RequestRateLimiter()
        self._write_rpm = write_rpm if write_rpm is not None else _env_rpm(_WRITE_RPM_ENV, DEFAULT_WRITE_RPM)
        self._read_rpm = read_rpm if read_rpm is not None else _env_rpm(_READ_RPM_ENV, DEFAULT_READ_RPM)
        self._heartbeat_rpm = (
            heartbeat_rpm if heartbeat_rpm is not None else _env_rpm(_HEARTBEAT_RPM_ENV, DEFAULT_HEARTBEAT_RPM)
        )
        self._sse_max_concurrent = sse_max_concurrent
        self._sse_connections: int = 0

    @property
    def sse_connections(self) -> int:
        """Current number of active SSE connections."""
        return self._sse_connections

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        # Versioned aliases share the canonical route's limits.
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1") :]

        if path in _SSE_LIMITED_PATHS and request.method.upper() == "GET":
            if self._sse_connections >= self._sse_max_concurrent:
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Too many concurrent SSE connections",
                        "bucket": "sse",
                        "max_concurrent": self._sse_max_concurrent,
                    },
                    headers={"Retry-After": "5"},
                )
                await response(scope, receive, send)
                return
            self._sse_connections += 1
            try:
                # call_next returns at headers; the ASGI app runs until the
                # body closes, including disconnects, cancellation and errors.
                await self.app(scope, receive, send)
            finally:
                self._sse_connections -= 1
            return
        await super().__call__(scope, receive, send)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        path = request.url.path
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1") :]
        method = request.method.upper()

        # Exempt loopback clients (orchestrator, spawner, agents) from rate
        # limiting - they are internal components, not external callers.
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
        client_id = _agent_bucket_key(request) or _request_client_id(request)
        heartbeat_key = _heartbeat_key(request, path, method)
        if heartbeat_key is not None:
            bucket_name = "heartbeat"
            rpm = self._heartbeat_rpm
            client_id = heartbeat_key
        elif method in _WRITE_METHODS:
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
        # Canonical rules cover aliases; retain versioned-only configurations.
        bucket = rate_limit.match_request(path, method)
        if bucket is None and path != request.url.path:
            bucket = rate_limit.match_request(request.url.path, method)
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
    headers are IGNORED - rotating ``X-Forwarded-For`` values must never let
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
    # Every hop was a trusted proxy - fall back to the direct peer.
    return direct_client_ip


def _auth_limiter_for_request(request: Request) -> AuthRateLimiter:
    """Keep authentication counters local to the application."""
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if not isinstance(limiter, AuthRateLimiter):
        limiter = AuthRateLimiter()
        request.app.state.auth_rate_limiter = limiter
    return limiter


def _auth_bucket(request: Request) -> tuple[str, int | None, int | None]:
    """Resolve existing seed buckets, treating aliases like canonical routes."""
    path = request.url.path
    if path.startswith("/api/v1/"):
        path = path[len("/api/v1") :]
    seed_config = getattr(request.app.state, "seed_config", None)
    rate_limit = getattr(seed_config, "rate_limit", None)
    if rate_limit is not None:
        bucket = rate_limit.match_request(path, request.method)
        if bucket is None and path != request.url.path:
            bucket = rate_limit.match_request(request.url.path, request.method)
        if bucket is not None:
            return f"seed:{bucket.name}", bucket.requests, bucket.window_seconds
    return "auth", None, None


async def check_auth_rate_limit(request: Request) -> None:
    """FastAPI dependency enforcing per-client authentication attempt limits.

    Uses the trusted-proxy-aware identity from the request middleware, including
    for loopback clients. All protected auth routes share the default bucket.
    Matching seed buckets override the default attempt count and window.
    Runs on the event loop so checks and app-local initialization are atomic.
    """
    limiter = _auth_limiter_for_request(request)
    bucket, max_requests, window_seconds = _auth_bucket(request)
    retry_after = limiter.check(
        _request_client_id(request), bucket=bucket, max_requests=max_requests, window_seconds=window_seconds
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


def record_auth_result(request: Request, *, success: bool) -> None:
    """Apply a dashboard credential result to this client's auth backoff."""
    limiter = _auth_limiter_for_request(request)
    bucket, _, _ = _auth_bucket(request)
    client_id = _request_client_id(request)
    if success:
        limiter.record_success(client_id, bucket=bucket)
    else:
        limiter.record_failure(client_id, bucket=bucket)
