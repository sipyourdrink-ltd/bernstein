"""Walk a FastAPI application's route table into its effective path templates.

Up to FastAPI 0.136, ``include_router`` copied a sub-router's routes into
``app.router.routes`` with the mount prefix already baked into each ``path``,
so reading ``route.path`` off every top-level entry enumerated the whole
surface. From 0.137 it appends a ``fastapi.routing._IncludedRouter`` wrapper
instead: the sub-router's routes stay behind ``wrapper.original_router``, the
prefix is carried on ``wrapper.include_context``, and the wrapper itself has
no ``path``. The same flat walk then sees opaque objects and enumerates
nothing - which is how a security gate derived from the route table (#4023)
went from "matches every per-task route" to "matches none" without a single
line of its own changing.

The walk here descends through those wrappers and re-applies each prefix, so
it yields the same templates on both sides of that change. Wrappers are
recognised by shape rather than by importing the private class, so a rename
downgrades this to the pre-0.137 behaviour instead of raising on import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = ["iter_route_paths", "route_path_templates"]


def _included_routes(route: Any) -> tuple[str, Sequence[Any]] | None:
    """Return ``(prefix, routes)`` if ``route`` wraps an included router."""
    original = getattr(route, "original_router", None)
    if original is None:
        return None
    prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
    return prefix, getattr(original, "routes", ()) or ()


def _walk(routes: Sequence[Any], prefix: str) -> Iterator[tuple[str, Any]]:
    for route in routes:
        included = _included_routes(route)
        if included is not None:
            sub_prefix, sub_routes = included
            yield from _walk(sub_routes, prefix + sub_prefix)
            continue
        path = getattr(route, "path", "")
        if path:
            yield prefix + path, route


def iter_route_paths(app: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(path_template, route)`` for every route reachable from ``app``.

    Args:
        app: A FastAPI/Starlette application, or a router.

    Yields:
        The effective path template and the route object that serves it.
        Routes mounted through ``include_router`` carry the mount prefix, so a
        template reads the same as the URL a client would call.
    """
    router = getattr(app, "router", None)
    yield from _walk(getattr(router if router is not None else app, "routes", ()) or (), "")


def route_path_templates(app: Any) -> list[str]:
    """Return every registered path template on ``app``, sorted and deduped."""
    return sorted({path for path, _ in iter_route_paths(app)})
