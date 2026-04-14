"""SSE events, badge, memory audit, and broadcast routes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

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


@router.get("/events")
def sse_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time dashboard updates.

    Includes disconnect detection via heartbeat pings and connection
    timeout handling to prevent leaked subscriber queues.
    """
    sse_bus = _get_sse_bus(request)
    queue = sse_bus.subscribe()

    # Timeout for individual queue.get() calls — if no message arrives
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
                    # No message (not even a heartbeat) in _READ_TIMEOUT_S — client likely disconnected
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


def _read_avg_quality_score(sdd_dir: Any) -> float:
    """Read average quality score from the JSONL metrics file."""
    quality_file = sdd_dir / "metrics" / "quality_scores.jsonl"
    if not quality_file.exists():
        return 0.0
    scores: list[int] = []
    for line in quality_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data: dict[str, Any] = json.loads(line)
            if "total" in data:
                scores.append(int(data["total"]))
        except ValueError:
            pass
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

    store = _get_store(request)
    workdir = _get_workdir(request)
    sdd_dir = workdir / ".sdd"

    # Task counts
    tasks = store.list_tasks()
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


@router.post("/broadcast")
async def broadcast_command(request: Request) -> JSONResponse:
    """Send a message to all running agents via fastest available channel.

    Uses stdin pipe where available (sub-second delivery), falls back
    to file-based COMMAND signal for agents without pipe support.

    Expects JSON body: ``{"message": "some instruction"}``.
    """
    from bernstein.core.agent_ipc import broadcast_message

    body: Any = await request.json()
    message: str = body.get("message", "")
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
