"""Dashboard-specific routes — file lock inspection endpoint."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()


def _runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


@router.get("/dashboard/file_locks")
async def file_locks_endpoint(request: Request) -> JSONResponse:
    """Return active file locks grouped by agent for the dashboard.

    Reads the persisted lock state from ``.sdd/runtime/file_locks.json`` and
    returns it in a dashboard-friendly format with both a flat list and an
    agent-grouped view.

    Returns:
        JSON with ``all_locks`` (flat list sorted by path), ``locks_by_agent``
        (dict keyed by agent_id with files list + task info + elapsed_s),
        ``count`` (total lock count), and ``ts`` (generation timestamp).
    """
    runtime_dir = _runtime_dir(request)
    locks_path = runtime_dir / "file_locks.json"

    now = time.time()
    all_locks: list[dict[str, Any]] = []
    locks_by_agent: dict[str, dict[str, Any]] = {}

    if locks_path.exists():
        try:
            raw = json.loads(locks_path.read_text(encoding="utf-8"))
            for entry in raw:
                file_path = str(entry.get("file_path", ""))
                agent_id = str(entry.get("agent_id", ""))
                task_id = str(entry.get("task_id", ""))
                task_title = str(entry.get("task_title", ""))
                locked_at = float(entry.get("locked_at", 0))
                elapsed_s = int(now - locked_at) if locked_at > 0 else 0

                all_locks.append(
                    {
                        "file_path": file_path,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "task_title": task_title,
                        "locked_at": locked_at,
                        "elapsed_s": elapsed_s,
                    }
                )

                if agent_id not in locks_by_agent:
                    locks_by_agent[agent_id] = {
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "task_title": task_title,
                        "locked_at": locked_at,
                        "elapsed_s": elapsed_s,
                        "files": [],
                    }
                cast("list[str]", locks_by_agent[agent_id]["files"]).append(file_path)
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass

    return JSONResponse(
        {
            "ts": now,
            "all_locks": sorted(all_locks, key=lambda x: str(x.get("file_path", ""))),
            "locks_by_agent": locks_by_agent,
            "count": len(all_locks),
        }
    )
