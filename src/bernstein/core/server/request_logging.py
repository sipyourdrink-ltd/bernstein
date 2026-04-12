"""WEB-010: API request/response logging middleware.

Logs method, path, status code, and duration for every request.
Verbosity is configurable via ``BERNSTEIN_REQUEST_LOG_LEVEL`` env var.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    type _CallNext = Any  # Callable[[Request], Awaitable[Response]]

logger = logging.getLogger("bernstein.request_log")

# Configurable log levels: debug, info, warning
_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "none": logging.CRITICAL + 1,
}

# Paths that are too noisy to log at default level
_NOISY_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/live",
        "/ready",
        "/alive",
        "/events",
    }
)


def _resolve_log_level() -> int:
    """Resolve the logging level from the environment."""
    raw = os.environ.get("BERNSTEIN_REQUEST_LOG_LEVEL", "info").lower()
    return _LEVEL_MAP.get(raw, logging.INFO)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log HTTP request method, path, status code, and response time.

    Verbosity modes:
    - ``debug``: all requests including health checks.
    - ``info`` (default): skip noisy health-check paths.
    - ``warning``: only slow requests (>1s).
    - ``none``: disabled.
    """

    def __init__(
        self,
        app: Any,
        *,
        log_level: int | None = None,
        slow_threshold_s: float = 1.0,
    ) -> None:
        super().__init__(app)
        self._level = log_level if log_level is not None else _resolve_log_level()
        self._slow_threshold_s = slow_threshold_s

    async def dispatch(self, request: Request, call_next: _CallNext) -> StarletteResponse:
        start = time.monotonic()
        response: StarletteResponse = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        path = request.url.path
        method = request.method
        status = response.status_code

        # Always log slow requests at WARNING regardless of configured level
        if duration_ms > self._slow_threshold_s * 1000:
            logger.warning(
                "%s %s %d %.1fms (slow)",
                method,
                path,
                status,
                duration_ms,
            )
        elif self._level <= logging.DEBUG or (self._level <= logging.INFO and path not in _NOISY_PATHS):
            logger.log(
                self._level,
                "%s %s %d %.1fms",
                method,
                path,
                status,
                duration_ms,
            )

        return response
