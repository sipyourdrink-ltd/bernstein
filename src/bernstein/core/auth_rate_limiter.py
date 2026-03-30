"""In-memory rate limiter for authentication endpoints.

Limits requests per source IP using a sliding window of timestamps.
State resets on server restart (acceptable for a dev tool).
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request


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
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._call_count = 0

    def check(self, ip: str) -> float | None:
        """Check whether *ip* is within the rate limit.

        Returns ``None`` if the request is allowed, or the number of
        seconds until the next request slot opens (for ``Retry-After``).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Trim timestamps outside the window
        timestamps = self._hits[ip]
        self._hits[ip] = timestamps = [t for t in timestamps if t > cutoff]

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


# Shared instance used by the auth router dependency.
_auth_limiter = AuthRateLimiter()


async def check_auth_rate_limit(request: Request) -> None:
    """FastAPI dependency that enforces the auth rate limit."""
    ip = request.client.host if request.client else "unknown"
    retry_after = _auth_limiter.check(ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(int(retry_after))},
        )
