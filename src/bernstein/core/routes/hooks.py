"""Hook receiver routes for Claude Code hook events.

Receives HTTP POST callbacks from Claude Code's built-in hook system
(PostToolUse, Stop, PreCompact, SubagentStart, SubagentStop).  The
adapter injects these hooks into ``.claude/settings.local.json`` before
spawning so every tool invocation and lifecycle event is reported in
real time.

The endpoint is intentionally public (no auth required) because hooks
fire from the same localhost where the agent runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.hooks_receiver import parse_hook_event, process_hook_event

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


@router.post("/hooks/{session_id}")
async def receive_hook(session_id: str, request: Request) -> JSONResponse:
    """Receive a hook event from Claude Code.

    Claude Code sends structured JSON with at minimum a ``hook_event_name``
    field.  The event is parsed, persisted to a JSONL sidecar, and triggers
    side effects (heartbeat touch, completion markers, etc.).

    Args:
        session_id: Agent session identifier from the URL path.
        request: The incoming FastAPI request.

    Returns:
        JSON response with status and action taken.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(
            content={"status": "error", "detail": "invalid JSON body"},
            status_code=400,
        )

    workdir = _get_workdir(request)
    event = parse_hook_event(session_id, body)
    result = process_hook_event(event, workdir)
    return JSONResponse(content=result, status_code=200)
