"""Hook receiver routes for Claude Code hook events.

Receives HTTP POST callbacks from Claude Code's built-in hook system
(PostToolUse, Stop, PreCompact, SubagentStart, SubagentStop).  The
adapter injects these hooks into ``.claude/settings.local.json`` before
spawning so every tool invocation and lifecycle event is reported in
real time.

The endpoint is intentionally public (no auth required) because hooks
fire from the same localhost where the agent runs.  Because the path
parameter is attacker-controllable when the server is reachable from the
network, every ``session_id`` is run through
:func:`bernstein.core.hooks_receiver.validate_session_id` before any
filesystem operation happens.  See audit-114 for the traversal threat
model this guards against.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.hooks_receiver import (
    InvalidSessionIdError,
    parse_hook_event,
    process_hook_event,
    validate_session_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_workdir(request: Request) -> Path:
    """Return the repository root from application state."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


def _reject(detail: str, status_code: int = 400) -> JSONResponse:
    """Return a 400-style rejection with a consistent error envelope."""
    return JSONResponse(
        content={"status": "error", "detail": detail},
        status_code=status_code,
    )


@router.post("/hooks/{session_id}")
async def receive_hook(session_id: str, request: Request) -> JSONResponse:
    """Receive a hook event from Claude Code.

    Claude Code sends structured JSON with at minimum a ``hook_event_name``
    field.  The event is parsed, persisted to a JSONL sidecar, and triggers
    side effects (heartbeat touch, completion markers, etc.).

    Security (audit-114):
        ``session_id`` is strictly validated before anything else.  Values
        containing path separators, ``..``, null bytes, or that otherwise
        fall outside ``^[A-Za-z0-9_-]{1,128}$`` are rejected with 400.
        This blocks URL-encoded traversal such as
        ``/hooks/..%2F..%2Fruntime%2Fsignals%2FSHUTDOWN`` from forging
        completion markers or clobbering runtime state.

    Args:
        session_id: Agent session identifier from the URL path.
        request: The incoming FastAPI request.

    Returns:
        JSON response with status and action taken, or a 400 error if
        the ``session_id`` is unsafe or the body is not valid JSON.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as exc:
        logger.warning(
            "Rejected hook POST with unsafe session_id: %s",
            exc,
        )
        return _reject(f"invalid session_id: {exc}")

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return _reject("invalid JSON body")

    workdir = _get_workdir(request)
    try:
        event = parse_hook_event(session_id, body)
        result = process_hook_event(event, workdir)
    except InvalidSessionIdError as exc:
        # Defense in depth — primary validation above should have
        # already caught this, but we re-map any downstream rejection
        # from the receiver to a 400 rather than a 500.
        logger.warning("Hook receiver rejected session_id downstream: %s", exc)
        return _reject(f"invalid session_id: {exc}")
    return JSONResponse(content=result, status_code=200)
