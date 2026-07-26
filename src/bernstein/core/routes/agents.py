"""Agent inspection routes - logs, kill signals, and SSE output streams."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from bernstein.core.routes._sse import SSE_RESPONSES
from bernstein.core.server import AgentKillResponse, AgentLogsResponse, read_log_tail

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

router = APIRouter()

# Maximum idle poll ticks before the SSE stream closes (prevents infinite hang in tests).
_MAX_IDLE_TICKS = 30
_POLL_INTERVAL = 1.0

# Session-id sanitiser - only allow chars that can legitimately appear in an
# agent session id (role, dashes, hex). Blocks path traversal payloads such as
# ``../../etc/passwd`` and absolute paths like ``/etc/shadow``.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# Synthetic session ids are emitted as "<role>-<task_short>" by the fallback
# path in :func:`list_agents` and never correspond to a real log file.
_SYNTHETIC_PREFIX = "synthetic-"

# Map TaskStatus → AgentSession-like status string the GUI understands.
_TASK_TO_AGENT_STATUS: dict[str, str] = {
    "open": "idle",
    "claimed": "spawning",
    "in_progress": "running",
    "done": "completed",
    "closed": "completed",
    "failed": "failed",
    "blocked": "stalled",
    "waiting_for_subtasks": "stalled",
    "cancelled": "dead",
    "orphaned": "dead",
    "pending_approval": "merging",
    "planned": "idle",
}


def _runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _validate_session_id(session_id: str) -> None:
    """Reject any session_id that could escape the runtime dir.

    Raises:
        HTTPException 400 when the value contains path separators or other
        characters not permitted in a session identifier.
    """
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")


def _is_synthetic(session_id: str) -> bool:
    return session_id.startswith(_SYNTHETIC_PREFIX)


# ---------------------------------------------------------------------------
# GET /agents - list of all known agent sessions (web GUI list view)
# ---------------------------------------------------------------------------


def _task_store(request: Request) -> Any:
    return request.app.state.store


def _cost_for_role(store: Any, role: str) -> float:
    """Best-effort role-aggregated cost lookup that never raises."""
    try:
        getter = getattr(store, "cost_by_role", None)
        if getter is None:
            return 0.0
        costs = getter()
        if isinstance(costs, dict):
            value = costs.get(role)
            if isinstance(value, (int, float)):
                return float(value)
    # bot-ack: pre-existing-1723 (best-effort cost lookup; never raise on GUI)
    except Exception:
        return 0.0
    return 0.0


def _serialize_agent(store: Any, sid: str, s: Any, now: float) -> dict[str, Any]:
    spawn_ts = getattr(s, "spawn_ts", 0.0) or 0.0
    duration_ms = max(0, int((now - spawn_ts) * 1000)) if spawn_ts else None
    task_ids = list(getattr(s, "task_ids", []) or [])
    current_task_title: str | None = None
    if task_ids and hasattr(store, "get_task"):
        try:
            t = store.get_task(task_ids[0])
            if t is not None:
                current_task_title = getattr(t, "title", None)
        # bot-ack: pre-existing-1723 (task lookup is best-effort enrichment)
        except Exception:
            current_task_title = None
    role = getattr(s, "role", "")
    tokens_used = int(getattr(s, "tokens_used", 0) or 0)
    tokens_in = int(getattr(s, "tokens_in", 0) or 0) or tokens_used
    tokens_out = int(getattr(s, "tokens_out", 0) or 0)
    return {
        "id": sid,
        "session_id": sid,
        "name": getattr(s, "name", None) or sid,
        "role": role,
        "status": getattr(s, "status", "starting"),
        "spawn_ts": spawn_ts,
        "heartbeat_ts": getattr(s, "heartbeat_ts", 0.0),
        "duration_ms": duration_ms,
        "tokens_used": tokens_used,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "context_utilization_pct": getattr(s, "context_utilization_pct", 0.0),
        "task_ids": task_ids,
        "current_task_id": task_ids[0] if task_ids else None,
        "current_task_title": current_task_title,
        "current_task": current_task_title,  # alias the GUI uses
        "model": getattr(getattr(s, "model_config", None), "name", None),
        "provider": getattr(s, "provider", None),
        "cost_usd": _cost_for_role(store, role),
        "exit_code": getattr(s, "exit_code", None),
        "synthetic": False,
    }


def _synthesize_agents_from_tasks(store: Any, now: float) -> list[dict[str, Any]]:
    """Fabricate one agent entry per claimed/in-progress task.

    Mock adapters never call ``TaskStore.heartbeat()``, so ``store.agents`` is
    empty even when the orchestrator has obviously claimed work. Without this
    fallback the GUI would render an empty grid and the "live" counter would
    perpetually read zero, despite the backlog clearly being in flight.
    """
    if not hasattr(store, "list_tasks"):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for status_value in ("claimed", "in_progress"):
        try:
            tasks = store.list_tasks(status=status_value)
        # bot-ack: pre-existing-1723 (best-effort task synthesis for GUI fallback)
        except Exception:
            tasks = []
        for t in tasks:
            role = getattr(t, "role", "") or "agent"
            task_id = str(getattr(t, "id", "") or "")
            if not task_id:
                continue
            short = task_id.replace("-", "")[:8]
            sid = f"{_SYNTHETIC_PREFIX}{role}-{short}"
            if sid in seen:
                continue
            seen.add(sid)
            claimed_at = getattr(t, "claimed_at", None) or getattr(t, "created_at", None) or 0.0
            duration_ms = max(0, int((now - claimed_at) * 1000)) if claimed_at else None
            agent_status = _TASK_TO_AGENT_STATUS.get(status_value, "running")
            out.append(
                {
                    "id": sid,
                    "session_id": sid,
                    "name": f"{role}-{short}",
                    "role": role,
                    "status": agent_status,
                    "spawn_ts": claimed_at,
                    "heartbeat_ts": 0.0,
                    "duration_ms": duration_ms,
                    "tokens_used": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "context_utilization_pct": 0.0,
                    "task_ids": [task_id],
                    "current_task_id": task_ids_first(t),
                    "current_task_title": getattr(t, "title", None),
                    "current_task": getattr(t, "title", None),
                    "model": getattr(t, "model", None),
                    "provider": None,
                    "cost_usd": _cost_for_role(store, role),
                    "exit_code": None,
                    "synthetic": True,
                }
            )
    return out


def task_ids_first(task: Any) -> str | None:
    return str(getattr(task, "id", "")) or None


@router.get("/agents")
def list_agents(request: Request) -> list[dict[str, Any]]:
    """Return a flat list of agent sessions for the web GUI grid.

    When ``TaskStore.agents`` is empty (e.g. only mock adapters spawned and
    they never heartbeat) we fall back to synthesising one entry per
    claimed/in-progress task, marked with ``"synthetic": true``. That keeps
    the GUI grid populated during demos and avoids the dreaded "0 sessions"
    empty state when work is obviously in flight.
    """
    store = _task_store(request)
    sessions = getattr(store, "agents", {}) or {}

    now = time.time()
    out: list[dict[str, Any]] = [_serialize_agent(store, sid, s, now) for sid, s in sessions.items()]

    if not out:
        out = _synthesize_agents_from_tasks(store, now)

    return out


# ---------------------------------------------------------------------------
# GET /agents/comparison - pairwise compare two sessions for the GUI overlay
# ---------------------------------------------------------------------------


@router.get("/agents/comparison")
def agents_comparison(
    request: Request,
    left: str | None = None,
    right: str | None = None,
) -> dict[str, Any]:
    """Return the {left, right, series} shape the GUI comparison overlay
    expects.

    The legacy version of this endpoint returned ``{"agents": [...]}``, which
    silently produced an empty render in the comparison drawer. We now look
    each session up by id, return the same agent dicts the grid uses, and
    emit a placeholder ``series`` (Phase 2 will populate it from metrics).
    """
    agents = list_agents(request)
    by_id = {a["session_id"]: a for a in agents}
    return {
        "left": by_id.get(left or ""),
        "right": by_id.get(right or ""),
        "series": [],
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# GET /agents/{session_id}/logs
# ---------------------------------------------------------------------------


@router.get(
    "/agents/{session_id}/logs",
    responses={404: {"description": "No log for session"}},
)
def agent_logs(
    request: Request,
    session_id: str,
    tail_bytes: int = 0,
) -> AgentLogsResponse:
    """Return the log content for a running or finished agent session.

    Args:
        session_id: Agent session identifier (e.g. ``backend-abc12345``).
        tail_bytes: When > 0, only the last *tail_bytes* of the file are
            returned (partial leading line is stripped).  0 means full file.

    Returns:
        JSON with ``session_id``, ``content`` string, and ``size`` in bytes.

    Raises:
        HTTPException: 400 when session_id contains illegal characters,
            404 when no log file exists for the session.
    """
    _validate_session_id(session_id)
    if _is_synthetic(session_id):
        # Synthetic agents have no on-disk log - return an empty payload
        # rather than 404 so the GUI can render a friendly placeholder.
        return AgentLogsResponse(session_id=session_id, content="", size=0)
    runtime_dir = _runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"No log for session {session_id!r}")

    size = log_path.stat().st_size
    offset = max(0, size - tail_bytes) if tail_bytes > 0 else 0
    content = read_log_tail(log_path, offset)

    return AgentLogsResponse(session_id=session_id, content=content, size=size)


# ---------------------------------------------------------------------------
# POST /agents/{session_id}/kill
# ---------------------------------------------------------------------------


@router.post("/agents/{session_id}/kill")
def agent_kill(request: Request, session_id: str) -> AgentKillResponse:
    """Request termination of an agent by writing a ``.kill`` signal file.

    The orchestrator polls for these files on each tick and calls
    ``spawner.kill(session)`` for the matching session.

    Args:
        session_id: Agent session to terminate.

    Returns:
        JSON with ``session_id`` and ``kill_requested: true``.

    Raises:
        HTTPException 400 when session_id contains illegal characters.
    """
    _validate_session_id(session_id)
    if _is_synthetic(session_id):
        # Synthetic sessions don't correspond to a real spawned process -
        # acknowledge the request so the UI doesn't show a hard failure,
        # but mark it as a no-op.
        return AgentKillResponse(session_id=session_id, kill_requested=False)
    runtime_dir = _runtime_dir(request)
    kill_file = runtime_dir / f"{session_id}.kill"
    kill_file.write_text(str(time.time()), encoding="utf-8")
    return AgentKillResponse(session_id=session_id, kill_requested=True)


# ---------------------------------------------------------------------------
# GET /agents/{session_id}/stream  (SSE)
# ---------------------------------------------------------------------------


@router.get("/agents/{session_id}/stream", response_class=StreamingResponse, responses=SSE_RESPONSES)
def agent_stream(request: Request, session_id: str) -> StreamingResponse:
    """Server-Sent Events stream of agent output for the given session.

    Replays all existing log content first, then tails the file for new
    lines.  Closes automatically after ``_MAX_IDLE_TICKS`` consecutive
    polls with no new data (or no log file).

    For synthetic agents (no real log file) the stream emits a single
    ``unavailable`` event and closes immediately, avoiding an infinite
    reconnect loop in the browser.

    SSE event format::

        data: {"type": "connected", "session_id": "<id>"}

        data: {"line": "<log line>"}

        data: {"type": "unavailable", "reason": "synthetic"}

    Raises:
        HTTPException 400 when session_id contains illegal characters.
    """
    _validate_session_id(session_id)
    runtime_dir = _runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"
    synthetic = _is_synthetic(session_id)

    async def _generate() -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'type': 'connected', 'session_id': session_id})}\n\n"

        if synthetic:
            yield (
                "event: unavailable\n"
                f"data: {json.dumps({'type': 'unavailable', 'reason': 'synthetic', 'session_id': session_id})}\n\n"
            )
            return

        offset = 0
        idle_ticks = 0

        while idle_ticks < _MAX_IDLE_TICKS:
            if await request.is_disconnected():
                break

            if not log_path.exists():
                await asyncio.sleep(_POLL_INTERVAL)
                idle_ticks += 1
                continue

            content = read_log_tail(log_path, offset)
            # Recompute the offset *after* reading so we never skip bytes
            # written between ``stat()`` and ``read()``.
            new_offset = offset + len(content.encode("utf-8")) if content else log_path.stat().st_size

            if not content:
                idle_ticks += 1
                await asyncio.sleep(_POLL_INTERVAL)
                continue

            for line in content.splitlines():
                if line:
                    yield f"data: {json.dumps({'line': line})}\n\n"
            offset = new_offset
            idle_ticks = 0

            await asyncio.sleep(_POLL_INTERVAL)

    return StreamingResponse(_generate(), media_type="text/event-stream")
