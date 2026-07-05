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


def build_router() -> APIRouter:
    """Return a fresh ``/api/v1`` router for a single application instance.

    ``create_app`` includes every route group into this router. Using a
    module-level router for that would make the mutation global: each
    ``create_app`` call would append another full copy of the v1 route set
    to the shared object, so every subsequent app instance grows by ~220
    routes. In a long-lived process that builds many apps (the test suite
    creates one per test) the route table grows without bound, RSS climbs
    with it, and app startup eventually fails with ``RecursionError``.
    Each app must therefore get its own router instance.
    """
    return APIRouter(prefix="/api/v1", route_class=_VersionedRoute)


# Kept for backward compatibility with external importers. Application code
# must not mutate this shared instance; ``create_app`` builds a fresh router
# via ``build_router()``.
router = build_router()
