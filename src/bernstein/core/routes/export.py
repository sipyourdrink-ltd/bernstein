"""WEB-008: Data export endpoints for tasks and agents.

GET /export/tasks?format=csv|json
GET /export/agents?format=csv|json
"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import Response

if TYPE_CHECKING:
    from bernstein.core.server import TaskStore

router = APIRouter()

_TASK_CSV_FIELDS = [
    "id",
    "title",
    "description",
    "role",
    "priority",
    "status",
    "assigned_agent",
    "created_at",
    "completed_at",
]

_AGENT_CSV_FIELDS = [
    "id",
    "role",
    "status",
    "task_id",
    "started_at",
]


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _task_to_export_dict(task: Any) -> dict[str, Any]:
    """Convert a task object to an export-friendly dict."""
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": task.priority,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "assigned_agent": task.assigned_agent or "",
        "created_at": task.created_at,
        "completed_at": task.completed_at or "",
    }


def _agents_snapshot(request: Request) -> list[dict[str, Any]]:
    """Read the current agents snapshot from disk."""
    from pathlib import Path

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if not isinstance(sdd_dir, Path):
        return []

    path = sdd_dir / "runtime" / "agents.json"
    if not path.exists():
        return []

    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    agents_raw: list[dict[str, Any]] = []
    raw_list: list[Any] = payload.get("agents", [])
    for raw_agent in raw_list:
        if isinstance(raw_agent, dict):
            agent_dict = cast("dict[str, Any]", raw_agent)
            agents_raw.append(
                {
                    "id": str(agent_dict.get("id", "")),
                    "role": str(agent_dict.get("role", "")),
                    "status": str(agent_dict.get("status", "")),
                    "task_id": str(agent_dict.get("task_id", "")),
                    "started_at": agent_dict.get("started_at", ""),
                }
            )
    return agents_raw


def _make_csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    """Render a list of dicts as CSV text."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


@router.get("/export/tasks")
def export_tasks(request: Request, format: str = "json") -> Response:
    """Export all tasks as CSV or JSON.

    Query params:
        format: ``csv`` or ``json`` (default ``json``).
    """
    store = _get_store(request)
    all_tasks = store.list_tasks()
    rows = [_task_to_export_dict(t) for t in all_tasks]

    if format == "csv":
        content = _make_csv(rows, _TASK_CSV_FIELDS)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=tasks_{int(time.time())}.csv"},
        )

    return Response(
        content=json.dumps(rows, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=tasks_{int(time.time())}.json"},
    )


@router.get("/export/agents")
def export_agents(request: Request, format: str = "json") -> Response:
    """Export agent snapshots as CSV or JSON.

    Query params:
        format: ``csv`` or ``json`` (default ``json``).
    """
    agents = _agents_snapshot(request)

    if format == "csv":
        content = _make_csv(agents, _AGENT_CSV_FIELDS)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=agents_{int(time.time())}.csv"},
        )

    return Response(
        content=json.dumps(agents, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=agents_{int(time.time())}.json"},
    )
