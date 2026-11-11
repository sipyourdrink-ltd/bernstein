"""SSE events, badge, memory audit, and broadcast routes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from bernstein.core.replay.journal import JournalPathError, run_journal_path
from bernstein.core.replay.thread_projection import project_journal
from bernstein.core.routes._sse import SSE_RESPONSES

_SDD_NOT_CONFIGURED = "sdd_dir not configured"

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from bernstein.core.server import SSEBus, TaskStore

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_workdir(request: Request) -> Path:
    """Return the best-known repository root for runtime metadata."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


@router.get("/events", response_class=StreamingResponse, responses=SSE_RESPONSES)
def sse_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time dashboard updates.

    Includes disconnect detection via heartbeat pings and connection
    timeout handling to prevent leaked subscriber queues.
    """
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    # Timeout for individual queue.get() calls - if no message arrives
    # within this window (including heartbeats), the connection is dead.
    _READ_TIMEOUT_S = 60.0

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Send initial connection event
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            sse_bus.mark_read(queue)
            while True:
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected, closing stream")
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=_READ_TIMEOUT_S)
                except TimeoutError:
                    # No message (not even a heartbeat) in _READ_TIMEOUT_S - client likely disconnected
                    logger.debug("SSE client timed out after %.0fs, closing", _READ_TIMEOUT_S)
                    break
                sse_bus.mark_read(queue)
                yield message
        finally:
            sse_bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/tasks", response_class=StreamingResponse, responses=SSE_RESPONSES)
async def task_events(
    request: Request,
    role: str = Query(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"),
) -> StreamingResponse:
    """Stream future task.created notifications for an authorized role and tenant.

    Initial heartbeat acknowledges registration. No replay; disconnect ends the subscription.
    """
    from bernstein.core.routes.task_crud import _resolve_request_tenant_scope  # pyright: ignore[reportPrivateUsage]

    tenant = _resolve_request_tenant_scope(request)
    identity = getattr(request.state, "agent_identity", None)
    if identity is not None and identity.role != role:
        raise HTTPException(status_code=403, detail="Agent may subscribe only to its own role")
    bus = _get_sse_bus(request)
    try:
        queue = bus.subscribe(
            client_ip=request.client.host if request.client else "unknown",
            task_role=role,
            tenant_id=tenant,
            task_ids=frozenset(getattr(identity, "task_ids", None) or ()),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail="SSE reconnect rate limit exceeded") from exc

    async def generate() -> AsyncGenerator[str, None]:
        try:
            yield 'event: heartbeat\ndata: {"connected": true}\n\n'
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    message = "event: heartbeat\ndata: {}\n\n"
                bus.mark_read(queue)
                yield message
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


#: Seconds between journal checks on an open ``/events/thread`` stream.
THREAD_STREAM_POLL_S: float = 0.5
#: Idle seconds after which ``/events/thread`` sends an id-less heartbeat.
THREAD_STREAM_HEARTBEAT_S: float = 15.0
#: A ``Last-Event-ID`` is a journal index: a short non-negative ASCII decimal.
_JOURNAL_INDEX_RE = re.compile(r"[0-9]{1,18}")
_THREAD_HEARTBEAT = "event: heartbeat\ndata: {}\n\n"


def _get_sdd_dir(request: Request) -> Path:
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path):
        return sdd_dir
    return _get_workdir(request) / ".sdd"


def _last_event_index(request: Request) -> int | None:
    raw = request.headers.get("last-event-id", "").strip()
    if not raw:
        return None
    if _JOURNAL_INDEX_RE.fullmatch(raw) is None:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a journal index")
    return int(raw)


@router.get("/events/thread", response_class=StreamingResponse, responses=SSE_RESPONSES)
async def thread_events(
    request: Request,
    run: str = Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"),
) -> StreamingResponse:
    """Stream one run's journal as hash-anchored ``thread.step`` frames.

    Serves :func:`~bernstein.core.replay.thread_projection.project_journal`
    for ``.sdd/runs/<run>/journal.jsonl`` over SSE. Each frame's ``id`` is
    the row's journal index, so a client that reconnects with
    ``Last-Event-ID`` resumes after the last row it saw, across server
    restarts, for as long as the run journal is on disk. ``/events`` is
    unchanged; this is a sibling stream pinned to one run per connection.

    Ids sent on a connection are strictly increasing: a row whose index is
    not above the last one sent (a duplicate or out-of-order index) is not
    sent. Rows the journal reader cannot decode, including a partly
    written last line, are skipped until they become decodable. Heartbeats
    carry no ``id`` and never move a client's cursor. The stream ends if
    the journal file disappears (for example after retention pruning).
    """
    try:
        journal_path = run_journal_path(_get_sdd_dir(request), run)
    except JournalPathError as exc:
        raise HTTPException(status_code=400, detail="invalid run id") from exc
    if not journal_path.is_file():
        raise HTTPException(status_code=404, detail="run journal not found")
    after = _last_event_index(request)

    async def generate() -> AsyncGenerator[str, None]:
        cursor = after
        seen: tuple[int, int, int] | None = None
        idle = 0.0
        yield 'event: heartbeat\ndata: {"connected": true}\n\n'
        while not await request.is_disconnected():
            try:
                stat = journal_path.stat()
            except FileNotFoundError:
                return
            token = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if token != seen:
                seen = token
                events = await asyncio.to_thread(project_journal, journal_path, after_index=cursor)
                for event in events:
                    if cursor is not None and event.journal_index <= cursor:
                        continue
                    cursor = event.journal_index
                    idle = 0.0
                    yield event.to_sse()
            if idle >= THREAD_STREAM_HEARTBEAT_S:
                idle = 0.0
                yield _THREAD_HEARTBEAT
            await asyncio.sleep(THREAD_STREAM_POLL_S)
            idle += THREAD_STREAM_POLL_S

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _read_avg_quality_score(sdd_dir: Any) -> float:
    """Read average quality score from the JSONL metrics file."""
    quality_file = sdd_dir / "metrics" / "quality_scores.jsonl"
    if not quality_file.exists():
        return 0.0
    scores: list[int] = []
    for line in quality_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        with suppress(ValueError):
            data: dict[str, Any] = json.loads(line)
            if "total" in data:
                scores.append(int(data["total"]))
    return sum(scores) / len(scores) if scores else 0.0


def _completion_rate_color(completed: int, failed: int) -> str:
    """Return a shields.io color string based on completion rate."""
    total = completed + failed
    if total == 0:
        return "lightgrey"
    rate = completed / total
    if rate >= 0.9:
        return "brightgreen"
    if rate >= 0.7:
        return "yellowgreen"
    if rate >= 0.5:
        return "yellow"
    return "red"


@router.get("/badge.json")
def get_badge(request: Request) -> JSONResponse:
    """Return dynamic badge data for GitHub shields.io integration.

    Shows tasks completed, total cost, and quality score.
    Usage: https://img.shields.io/endpoint?url=<server>/badge.json
    """
    from bernstein.core.cost_tracker import CostTracker
    from bernstein.core.models import TaskStatus
    from bernstein.core.routes.task_crud import _resolve_request_tenant_scope

    store = _get_store(request)
    workdir = _get_workdir(request)
    sdd_dir = workdir / ".sdd"

    # Task counts.  The badge publishes a figure rather than a row, but the
    # figure is still computed over whichever rows this reads, so the read is
    # narrowed to the caller's tenant scope.
    tasks = store.list_tasks(tenant_id=_resolve_request_tenant_scope(request))
    completed = sum(1 for t in tasks if t.status == TaskStatus.DONE)
    failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)

    # Cost
    total_cost = 0.0
    costs_dir = sdd_dir / "runtime" / "costs"
    if costs_dir.exists():
        cost_files = sorted(costs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cost_files:
            tracker = CostTracker.load(sdd_dir, cost_files[0].stem)
            if tracker:
                total_cost = tracker.spent_usd

    # Quality score
    quality_score = _read_avg_quality_score(sdd_dir)

    # Determine color based on completion rate
    color = _completion_rate_color(completed, failed)

    return JSONResponse(
        content={
            "schemaVersion": 1,
            "label": "Bernstein",
            "message": f"{completed} done | ${total_cost:.2f} | {quality_score:.0f}%",
            "color": color,
        }
    )


# ---------------------------------------------------------------------------
# Memory provenance audit
# ---------------------------------------------------------------------------


@router.get("/memory/audit")
def memory_audit(request: Request) -> JSONResponse:
    """Audit the lesson memory provenance chain (OWASP ASI06 2026).

    Returns chain integrity status and a per-entry provenance trail.
    Detects tampering, insertion, deletion, and reordering attacks.
    """
    from bernstein.core.memory_integrity import audit_provenance, verify_chain

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return JSONResponse(content={"error": _SDD_NOT_CONFIGURED}, status_code=500)

    lessons_path = sdd_dir / "memory" / "lessons.jsonl"

    if not lessons_path.exists():
        return JSONResponse(
            content={
                "valid": True,
                "entries_checked": 0,
                "errors": [],
                "broken_at": -1,
                "trail": [],
            }
        )

    chain_result = verify_chain(lessons_path)
    trail = audit_provenance(lessons_path)

    return JSONResponse(
        content={
            "valid": chain_result.valid,
            "entries_checked": chain_result.entries_checked,
            "errors": chain_result.errors,
            "broken_at": chain_result.broken_at,
            "trail": [
                {
                    "line_number": e.line_number,
                    "lesson_id": e.lesson_id,
                    "filed_by_agent": e.filed_by_agent,
                    "task_id": e.task_id,
                    "created_iso": e.created_iso,
                    "content_hash": e.content_hash[:16] + "\u2026" if e.content_hash else "",
                    "chain_hash": e.chain_hash[:16] + "\u2026" if e.chain_hash else "",
                    "hash_valid": e.hash_valid,
                    "chain_position_valid": e.chain_position_valid,
                }
                for e in trail
            ],
        }
    )


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class BroadcastRequest(BaseModel):
    """Body for ``POST /broadcast``.

    Typing the body with a model lets FastAPI reject non-object or
    malformed JSON with a ``422`` instead of letting ``dict.get`` (or a
    ``JSONDecodeError``) raise an unhandled exception. ``message``
    defaults to an empty string so a missing field still funnels into
    the existing ``400 message is required`` path.
    """

    message: str = ""


@router.post("/broadcast")
async def broadcast_command(request: Request, payload: BroadcastRequest) -> JSONResponse:
    """Send a message to all running agents via fastest available channel.

    Uses stdin pipe where available (sub-second delivery), falls back
    to file-based COMMAND signal for agents without pipe support.

    Expects JSON body: ``{"message": "some instruction"}``.
    """
    from bernstein.core.agent_ipc import broadcast_message

    message = payload.message
    if not message:
        return JSONResponse(content={"error": "message is required"}, status_code=400)

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return JSONResponse(content={"error": _SDD_NOT_CONFIGURED}, status_code=500)

    workdir = sdd_dir.parent
    results = broadcast_message(message, workdir=workdir)

    pipe_count = sum(1 for v in results.values() if v == "pipe")
    file_count = sum(1 for v in results.values() if v == "file")

    return JSONResponse(
        content={
            "status": "broadcast_sent",
            "recipients": len(results),
            "via_pipe": pipe_count,
            "via_file": file_count,
        }
    )
