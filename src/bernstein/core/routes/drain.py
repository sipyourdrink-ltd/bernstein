"""Graceful drain endpoints — freeze/unfreeze task claiming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from bernstein.core.task_store import TaskStore

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _count_claimed(store: TaskStore) -> int:
    """Return the number of tasks currently in 'claimed' status."""
    return len(store.list_tasks(status="claimed"))


@router.post("/drain")
def drain_start(request: Request) -> JSONResponse:
    """Begin draining -- stop accepting new task claims."""
    request.app.state.draining = True  # type: ignore[attr-defined]
    active = _count_claimed(_get_store(request))
    return JSONResponse({"status": "draining", "active_agents": active})


@router.post("/drain/cancel")
def drain_cancel(request: Request) -> JSONResponse:
    """Cancel drain -- resume accepting claims."""
    request.app.state.draining = False  # type: ignore[attr-defined]
    return JSONResponse({"status": "cancelled"})


@router.get("/drain")
def drain_status(request: Request) -> JSONResponse:
    """Check drain status."""
    draining: bool = request.app.state.draining  # type: ignore[attr-defined]
    active = _count_claimed(_get_store(request))
    return JSONResponse({"draining": draining, "active_agents": active})
