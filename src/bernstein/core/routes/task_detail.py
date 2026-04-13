"""WEB-012: Dashboard task detail view with live log streaming via SSE.

GET /dashboard/tasks/{task_id} — task detail JSON
GET /dashboard/tasks/{task_id}/logs/stream — SSE log stream
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from bernstein.core.server import TaskResponse, TaskStore, read_log_tail, task_to_response

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

router = APIRouter()

_POLL_INTERVAL: float = 1.0
_MAX_IDLE_TICKS: int = 120  # Stop after 2 minutes of no new content


class TaskDetailResponse(BaseModel):
    """Detailed task view including log tail and progress."""

    task: TaskResponse
    log_tail: str
    log_size: int
    progress_entries: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    agent_status: str = ""


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _get_agent_log_path(runtime_dir: Path, session_id: str) -> Path | None:
    """Resolve the log file path for an agent session."""
    log_dir = runtime_dir / "logs"
    if not log_dir.exists():
        return None
    # Try exact match
    log_path = log_dir / f"{session_id}.log"
    if log_path.exists():
        return log_path
    # Try glob match
    matches = list(log_dir.glob(f"{session_id}*.log"))
    if matches:
        return matches[0]
    return None


@router.get("/dashboard/tasks/{task_id}", responses={404: {"description": "Task not found"}})
def task_detail(request: Request, task_id: str) -> TaskDetailResponse:
    """Return detailed task view including log tail and progress.

    Args:
        task_id: Task identifier.
    """
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    runtime_dir = _get_runtime_dir(request)
    log_tail = ""
    log_size = 0
    agent_status = ""

    if task.assigned_agent:
        log_path = _get_agent_log_path(runtime_dir, task.assigned_agent)
        if log_path is not None:
            log_tail = read_log_tail(log_path)
            log_size = len(log_tail.encode("utf-8"))
        agent_status = "assigned"

    # Build progress entries from task.progress_log (list[dict[str, Any]])
    progress_entries: list[dict[str, Any]] = []
    for entry in task.progress_log:
        progress_entries.append(
            {
                "message": str(entry.get("message", "")),
                "percent": int(entry.get("percent", 0)),
                "timestamp": float(entry.get("timestamp", 0)),
            }
        )

    return TaskDetailResponse(
        task=task_to_response(task),
        log_tail=log_tail,
        log_size=log_size,
        progress_entries=progress_entries,
        agent_status=agent_status,
    )


@router.get("/dashboard/tasks/{task_id}/logs/stream", responses={404: {"description": "Task not found"}})
async def task_log_stream(request: Request, task_id: str) -> StreamingResponse:
    """Stream agent logs for a task via Server-Sent Events.

    The stream sends new log content as ``log`` events and closes
    after the task completes or ``_MAX_IDLE_TICKS`` seconds of no new data.
    """
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    runtime_dir = _get_runtime_dir(request)

    async def _stream_logs() -> AsyncGenerator[str, None]:
        last_size = 0
        idle_ticks = 0
        session_id = task.assigned_agent or ""

        while idle_ticks < _MAX_IDLE_TICKS:
            if await request.is_disconnected():
                return

            # Re-read task status to detect completion
            current_task = store.get_task(task_id)
            if current_task is not None and current_task.status.value in ("done", "failed", "cancelled"):
                yield f'event: complete\ndata: {{"status": "{current_task.status.value}"}}\n\n'
                break

            if session_id:
                log_path = _get_agent_log_path(runtime_dir, session_id)
                if log_path is not None and log_path.exists():
                    current_size = log_path.stat().st_size
                    if current_size > last_size:
                        with open(log_path, encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            new_content = f.read()
                        last_size = current_size
                        idle_ticks = 0
                        # Escape newlines for SSE data field
                        for line in new_content.splitlines():
                            yield f"event: log\ndata: {line}\n\n"
                        continue

            idle_ticks += 1
            yield f'event: ping\ndata: {{"ts": {time.time()}}}\n\n'
            await asyncio.sleep(_POLL_INTERVAL)

        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(
        _stream_logs(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
