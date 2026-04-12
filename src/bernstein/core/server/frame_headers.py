"""Iframe embedding policy middleware for the web dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class FrameEmbeddingPolicy:
    """Response-header policy controlling iframe embedding."""

    frame_ancestors: str = "'self'"
    x_frame_options: str | None = "SAMEORIGIN"


def load_frame_embedding_policy() -> FrameEmbeddingPolicy:
    """Load the frame embedding policy from environment variables."""
    raw_ancestors = os.environ.get("BERNSTEIN_FRAME_ANCESTORS")
    if raw_ancestors is None or not raw_ancestors.strip():
        return FrameEmbeddingPolicy()

    frame_ancestors = raw_ancestors.strip()
    if frame_ancestors == "'self'":
        return FrameEmbeddingPolicy(frame_ancestors=frame_ancestors, x_frame_options="SAMEORIGIN")
    return FrameEmbeddingPolicy(frame_ancestors=frame_ancestors, x_frame_options=None)


class FrameHeadersMiddleware(BaseHTTPMiddleware):
    """Attach X-Frame-Options and CSP frame-ancestors headers."""

    def __init__(self, app: ASGIApp, policy: FrameEmbeddingPolicy) -> None:
        super().__init__(app)
        self._policy = policy

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", f"frame-ancestors {self._policy.frame_ancestors}")
        if self._policy.x_frame_options is not None:
            response.headers.setdefault("X-Frame-Options", self._policy.x_frame_options)
        else:
            if "X-Frame-Options" in response.headers:
                del response.headers["X-Frame-Options"]
        return response
