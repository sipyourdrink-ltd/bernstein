"""The route-table walk, and the matcher floors that depend on it (#4023).

The walk exists because ``include_router`` changed shape: FastAPI used to copy
a sub-router's routes into the app with the prefix already baked into each
``path``, and from 0.137 it keeps a wrapper object instead. A gate compiled
from the route table went empty on that change without any of its own code
moving, which is the failure the last test here is a floor against.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI

from bernstein.core.routes.route_table import iter_route_paths, route_path_templates
from bernstein.core.security.auth_middleware import (
    task_collection_route_patterns,
    task_id_route_patterns,
)


def _app_with_included_router() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.post("/{task_id}/cancel")
    def _cancel(task_id: str) -> dict[str, str]:  # pragma: no cover - never called
        return {"task_id": task_id}

    app.include_router(router, prefix="/tasks")
    return app


def test_an_included_routers_route_carries_its_mount_prefix() -> None:
    """The template reads as the URL a client calls, not as the sub-router wrote it."""
    assert "/tasks/{task_id}/cancel" in route_path_templates(_app_with_included_router())


def test_a_nested_include_accumulates_every_prefix() -> None:
    """Two levels of ``include_router`` compose; neither prefix is dropped."""
    inner = APIRouter()

    @inner.get("/health")
    def _health() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    middle = APIRouter()
    middle.include_router(inner, prefix="/artifacts")
    app = FastAPI()
    app.include_router(middle, prefix="/api/v1")

    assert "/api/v1/artifacts/health" in route_path_templates(app)


def test_a_route_registered_straight_on_the_app_is_still_enumerated() -> None:
    """Descending into wrappers must not lose the routes that never had one."""
    app = FastAPI()

    @app.get("/bulletin")
    def _bulletin() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    assert "/bulletin" in route_path_templates(app)


def test_the_walk_yields_the_route_object_beside_its_path() -> None:
    """Callers gate on ``methods`` too, so the route itself has to come along."""
    paths = dict(iter_route_paths(_app_with_included_router()))
    assert paths["/tasks/{task_id}/cancel"].methods == {"POST"}


def test_an_object_that_is_not_a_wrapper_is_read_as_a_plain_route() -> None:
    """Wrappers are recognised by shape - a rename degrades, it does not raise."""

    class _Opaque:
        path = "/opaque"

    app = FastAPI()
    app.router.routes.append(_Opaque())  # type: ignore[arg-type]
    assert "/opaque" in route_path_templates(app)


def test_the_real_apps_task_scope_matchers_are_not_empty(tmp_path: Path) -> None:
    """The floor: an empty matcher set makes the whole task-scope gate vacuous.

    Both sets are derived from the live route table, so a framework change to
    how routes are registered can empty them while every other assertion in
    the suite still passes - the gate simply stops matching anything, and
    per-task routes lose their check while collection routes lose their
    exemption. This test fails instead.
    """
    from bernstein.core.server import create_app

    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")

    assert len(task_id_route_patterns(app)) >= 20
    assert len(task_collection_route_patterns(app)) >= 5
