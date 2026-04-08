"""WEB-007: API versioning under /api/v1/.

Mounts all existing route groups under /api/v1/ while preserving
backward compatibility on the original unprefixed paths.

Version negotiation:
- All /api/v1/ responses include ``X-API-Version: 1`` header.
- Clients may send ``Accept-Version: 1`` to explicitly request v1.
- Root (unprefixed) paths remain available for one major version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

_CURRENT_VERSION = "1"


class _VersionedRoute(APIRoute):
    """Route subclass that appends ``X-API-Version`` to every response."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response: Response = await original(request)
            response.headers["X-API-Version"] = _CURRENT_VERSION
            return response

        return handler


router = APIRouter(prefix="/api/v1", route_class=_VersionedRoute)
