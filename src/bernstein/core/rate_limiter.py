"""Per-endpoint rate limiting middleware."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Rate limit configuration for an endpoint."""

    requests_per_minute: int = 100
    burst: int = 20


@dataclass
class RateLimitState:
    """Current rate limit state for a client."""

    requests: list[float] = field(default_factory=lambda: list[float]())
    burst_remaining: int = 20


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-endpoint rate limiting middleware.

    Configures rate limits per endpoint pattern.
    Uses sliding window algorithm with burst allowance.

    Args:
        app: ASGI application.
        limits: Dictionary mapping endpoint patterns to RateLimitConfig.
    """

    def __init__(
        self,
        app: ASGIApp,
        limits: dict[str, RateLimitConfig] | None = None,
    ) -> None:
        super().__init__(app)
        self._limits = limits or {}
        self._client_state: dict[str, dict[str, RateLimitState]] = defaultdict(dict)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """Process request and check rate limits.

        Args:
            request: Incoming request.
            call_next: Next middleware/handler.

        Returns:
            Response from next handler or 429 if rate limited.
        """
        # Get client identifier
        client_id = self._get_client_id(request)
        endpoint = request.url.path

        # Get rate limit config for this endpoint
        config = self._get_config_for_endpoint(endpoint)

        # Check rate limit
        if not self._check_rate_limit(client_id, endpoint, config):
            logger.warning(
                "Rate limit exceeded for client %s on endpoint %s",
                client_id,
                endpoint,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after": 60,
                },
                headers={"Retry-After": "60"},
            )

        return await call_next(request)

    def _get_client_id(self, request: Request) -> str:
        """Get client identifier from request.

        Args:
            request: Incoming request.

        Returns:
            Client identifier string.
        """
        # Check API key first
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"api:{api_key}"

        # Fall back to IP
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"

        if request.client:
            return f"ip:{request.client.host}"

        return "unknown"

    def _get_config_for_endpoint(self, endpoint: str) -> RateLimitConfig:
        """Get rate limit config for an endpoint.

        Args:
            endpoint: Endpoint path.

        Returns:
            RateLimitConfig for the endpoint.
        """
        # Check for exact match first
        if endpoint in self._limits:
            return self._limits[endpoint]

        # Check for pattern matches
        for pattern, config in self._limits.items():
            if endpoint.startswith(pattern.rstrip("*")):
                return config

        # Default limit
        return RateLimitConfig()

    def _check_rate_limit(
        self,
        client_id: str,
        endpoint: str,
        config: RateLimitConfig,
    ) -> bool:
        """Check if request is within rate limit.

        Args:
            client_id: Client identifier.
            endpoint: Endpoint path.
            config: Rate limit config.

        Returns:
            True if within limit, False if exceeded.
        """
        now = time.time()
        window_start = now - 60  # 1 minute window

        # Get or create state
        if endpoint not in self._client_state[client_id]:
            self._client_state[client_id][endpoint] = RateLimitState(burst_remaining=config.burst)

        state = self._client_state[client_id][endpoint]

        # Remove old requests outside window
        state.requests = [ts for ts in state.requests if ts > window_start]

        # Check burst
        if state.burst_remaining > 0:
            state.burst_remaining -= 1
            state.requests.append(now)
            return True

        # Check sliding window
        if len(state.requests) < config.requests_per_minute:
            state.requests.append(now)
            return True

        return False

    def cleanup_old_state(self, max_age_seconds: int = 3600) -> None:
        """Clean up old rate limit state.

        Args:
            max_age_seconds: Maximum age of state to keep.
        """
        now = time.time()
        cutoff = now - max_age_seconds

        for client_id in list(self._client_state.keys()):
            for endpoint in list(self._client_state[client_id].keys()):
                state = self._client_state[client_id][endpoint]
                state.requests = [ts for ts in state.requests if ts > cutoff]

                ep_config = self._get_config_for_endpoint(endpoint)
                if not state.requests and state.burst_remaining >= ep_config.burst:
                    del self._client_state[client_id][endpoint]

            if not self._client_state[client_id]:
                del self._client_state[client_id]


def create_rate_limit_middleware(
    app: ASGIApp,
    tasks_per_minute: int = 100,
    auth_per_minute: int = 10,
    default_per_minute: int = 100,
) -> RateLimitMiddleware:
    """Create rate limit middleware with common configuration.

    Args:
        app: ASGI application.
        tasks_per_minute: Rate limit for /tasks endpoints.
        auth_per_minute: Rate limit for /auth endpoints.
        default_per_minute: Default rate limit for other endpoints.

    Returns:
        Configured RateLimitMiddleware.
    """
    limits = {
        "/tasks": RateLimitConfig(requests_per_minute=tasks_per_minute),
        "/auth": RateLimitConfig(requests_per_minute=auth_per_minute),
        "/": RateLimitConfig(requests_per_minute=default_per_minute),
    }

    return RateLimitMiddleware(app, limits=limits)
