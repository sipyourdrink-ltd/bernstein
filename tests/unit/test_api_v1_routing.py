"""WEB-007: Tests for API versioning under /api/v1/."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from httpx import ASGITransport, AsyncClient
from starlette.routing import WebSocketRoute

from bernstein.core.server import create_app

# Paths that FastAPI/Starlette register on the root app for infrastructure
# (OpenAPI schema, interactive docs, root landing redirect). These are
# intentionally not mirrored under /api/v1/ because they describe the whole
# app rather than a specific API surface.
_INFRASTRUCTURE_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        # The versioned router mount point itself shows up as a bare "/api/v1"
        # route — its own counterpart would be "/api/v1/api/v1", which is
        # nonsense. Skip it from both sides of the diff.
        "/api/v1",
    }
)


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestAPIVersioning:
    """Test /api/v1/ route prefix."""

    @pytest.mark.anyio()
    async def test_v1_tasks_endpoint(self, client: AsyncClient) -> None:
        """GET /api/v1/tasks should work alongside GET /tasks."""
        resp = await client.get("/api/v1/tasks")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_legacy_tasks_still_works(self, client: AsyncClient) -> None:
        """Legacy GET /tasks should still respond."""
        resp = await client.get("/tasks")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_health_deps(self, client: AsyncClient) -> None:
        """GET /api/v1/health/deps should return health info."""
        resp = await client.get("/api/v1/health/deps")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "dependencies" in data

    @pytest.mark.anyio()
    async def test_v1_status_endpoint(self, client: AsyncClient) -> None:
        """GET /api/v1/status should return status data."""
        resp = await client.get("/api/v1/status")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_grafana_dashboard(self, client: AsyncClient) -> None:
        """GET /api/v1/grafana/dashboard should return dashboard JSON."""
        resp = await client.get("/api/v1/grafana/dashboard")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_v1_export_tasks(self, client: AsyncClient) -> None:
        """GET /api/v1/export/tasks should work."""
        resp = await client.get("/api/v1/export/tasks")
        assert resp.status_code == 200


def _collect_paths(app: FastAPI) -> tuple[set[str], set[str]]:
    """Return (root_paths, v1_relative_paths) registered on ``app``.

    ``root_paths`` holds every non-versioned path. ``v1_relative_paths`` holds
    the same paths with the ``/api/v1`` prefix stripped so the two sets can
    be compared directly.
    """
    root: set[str] = set()
    v1_relative: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute | APIWebSocketRoute | WebSocketRoute):
            continue
        path = route.path
        if path.startswith("/api/v1/"):
            v1_relative.add(path[len("/api/v1") :])
        elif path == "/api/v1":
            # Mount point itself — recorded in _INFRASTRUCTURE_PATHS.
            continue
        else:
            root.add(path)
    return root, v1_relative


class TestVersionedRoutesParity:
    """AUDIT-126: every root-mounted route must also exist under /api/v1/*."""

    def test_every_root_route_has_v1_counterpart(self, app: FastAPI) -> None:
        """Fail if a route is reachable at /foo but not at /api/v1/foo.

        This guards against the drift described in audit-126, where new
        routers were added to the outer app but never copied into the
        api_v1_router, leaving versioned clients with silent 404s.
        """
        root_paths, v1_relative = _collect_paths(app)

        missing = {path for path in root_paths if path not in _INFRASTRUCTURE_PATHS and path not in v1_relative}

        assert not missing, (
            "Routes registered on the root app are missing under /api/v1/. "
            "Add them to the `all_routers` list in "
            "bernstein.core.server.server_app.create_app, or extend "
            "_INFRASTRUCTURE_PATHS if they are intentionally unversioned. "
            f"Missing: {sorted(missing)}"
        )

    def test_every_v1_route_has_root_counterpart(self, app: FastAPI) -> None:
        """The /api/v1 surface must not contain orphan paths.

        Parity is bidirectional: versioned routes are always re-mounts of
        root routers, never new endpoints. A v1-only path would be an
        accidental divergence.
        """
        root_paths, v1_relative = _collect_paths(app)

        orphans = v1_relative - root_paths
        assert not orphans, f"Routes exist only under /api/v1/* but not on the root app. Orphans: {sorted(orphans)}"

    def test_versioned_router_covers_meaningful_surface(self, app: FastAPI) -> None:
        """Sanity check: the versioned surface is non-trivial.

        If someone inadvertently unplugs the ``all_routers`` loop, the
        parity check above still passes vacuously. This guards against
        that regression by requiring at least 40 mirrored paths.
        """
        _root_paths, v1_relative = _collect_paths(app)
        assert len(v1_relative) >= 40, (
            f"Only {len(v1_relative)} paths under /api/v1 — the routers list may have been truncated."
        )
