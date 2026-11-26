"""Trace timeline endpoint for the Bernstein dashboard.

GET /dashboard/tasks/{task_id}/trace - return a chronologically-ordered list
of trace events for *task_id*, flattened across every retry/spawn captured in
``.sdd/traces/{task_id}.jsonl``.

The dashboard renders this as a vertical timeline on the Trace tab. We do not
mutate the underlying JSONL store: this is a read-only view over whatever the
orchestrator emitted at runtime.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bernstein.core.task_store import TaskStore

router = APIRouter()
logger = logging.getLogger(__name__)


# A single timeline event as seen by the frontend.
class TraceTimelineEvent(BaseModel):
    """One event card on the Trace tab timeline.

    Attributes:
        id: Stable per-task event identifier (``"{trace_idx}:{step_idx}"`` for
            steps; ``"{trace_idx}:meta"`` for the synthetic trace-level summary).
        ts: Unix timestamp (seconds, float). 0.0 means unknown.
        kind: Event kind - mirrors the TUI vocabulary
            (``spawn|orient|plan|edit|verify|complete|fail|compact|trace_meta``).
        actor: Best-effort attribution string - usually ``{role}/{model}`` or
            ``{session_id}``. Empty when unknown.
        summary: One-line human-readable description.
        outcome: ``success | failed | unknown | neutral`` - drives colour coding.
        trace_id: Owning trace id (so the FE can group events from the same spawn).
        session_id: Owning session id (mirrors the agent log filename).
        payload: Full event payload for the expandable JSON card.
    """

    id: str
    ts: float
    kind: str
    actor: str = ""
    summary: str = ""
    outcome: str = "neutral"
    trace_id: str = ""
    session_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict[str, Any])


class TraceTimelineResponse(BaseModel):
    """Container returned by ``GET /dashboard/tasks/{task_id}/trace``."""

    task_id: str
    events: list[TraceTimelineEvent]
    total: int
    cursor: int | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    has_open_trace: bool = False


def _get_store(request: Request) -> TaskStore:
    return cast("TaskStore", request.app.state.store)


def _get_workdir(request: Request) -> Path:
    """Resolve the project root associated with the running server."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    runtime_dir = getattr(request.app.state, "runtime_dir", None)
    if isinstance(runtime_dir, Path):
        return runtime_dir.parent
    return Path.cwd()


def _read_trace_lines(traces_dir: Path, task_id: str) -> list[dict[str, Any]]:
    """Read every JSONL trace record for *task_id*.

    Returns the parsed dicts in file order. Lines that fail to parse are
    skipped silently - a malformed line should not break the whole tab.
    """
    path = traces_dir / f"{task_id}.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _classify_outcome(kind: str, trace_outcome: str) -> str:
    """Map a step kind + parent trace outcome onto a colour bucket."""
    if kind == "fail":
        return "failed"
    if kind == "complete":
        return "success" if trace_outcome != "failed" else "failed"
    if kind in ("spawn", "compact"):
        return "neutral"
    if trace_outcome == "failed":
        return "neutral"
    return "neutral"


def _flatten_trace_events(traces: list[dict[str, Any]]) -> list[TraceTimelineEvent]:
    """Expand a list of stored trace dicts into individual timeline events."""
    events: list[TraceTimelineEvent] = []
    for trace_idx, trace in enumerate(traces):
        trace_id = str(trace.get("trace_id", f"trace-{trace_idx}"))
        session_id = str(trace.get("session_id", ""))
        role = str(trace.get("agent_role", ""))
        model = str(trace.get("model", ""))
        actor = f"{role}/{model}" if role or model else session_id
        trace_outcome = str(trace.get("outcome", "unknown"))
        spawn_ts_raw = trace.get("spawn_ts", 0.0)
        end_ts_raw = trace.get("end_ts")
        try:
            spawn_ts = float(spawn_ts_raw) if spawn_ts_raw is not None else 0.0
        except (TypeError, ValueError):
            spawn_ts = 0.0

        # Synthetic trace-meta header so the FE can render a session marker
        # even if the trace recorded zero steps yet.
        meta_payload: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "agent_role": role,
            "model": model,
            "effort": trace.get("effort", ""),
            "spawn_ts": spawn_ts,
            "end_ts": end_ts_raw,
            "outcome": trace_outcome,
            "task_ids": trace.get("task_ids", []),
            "total_consumed": trace.get("total_consumed", 0),
            "total_allocated_budget": trace.get("total_allocated_budget", 0),
            "turn_count": trace.get("turn_count", 0),
        }
        events.append(
            TraceTimelineEvent(
                id=f"{trace_idx}:meta",
                ts=spawn_ts,
                kind="trace_meta",
                actor=actor,
                summary=f"Spawn {role or 'agent'}"
                + (f" ({model})" if model else "")
                + (" - running" if end_ts_raw is None else f" - {trace_outcome}"),
                outcome="failed" if trace_outcome == "failed" else "neutral",
                trace_id=trace_id,
                session_id=session_id,
                payload=meta_payload,
            )
        )

        steps_raw = trace.get("steps", [])
        if not isinstance(steps_raw, list):
            continue
        for step_idx, step in enumerate(cast("list[Any]", steps_raw)):
            if not isinstance(step, dict):
                continue
            kind = str(step.get("type", "plan"))
            try:
                ts = float(step.get("timestamp", 0.0) or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            detail = str(step.get("detail", ""))
            files = step.get("files", []) or []
            summary = detail
            if not summary and isinstance(files, list) and files:
                summary = f"{kind}: {', '.join(str(f) for f in files[:3])}"
                if len(files) > 3:
                    summary += f" (+{len(files) - 3} more)"
            if not summary:
                summary = kind
            events.append(
                TraceTimelineEvent(
                    id=f"{trace_idx}:{step_idx}",
                    ts=ts,
                    kind=kind,
                    actor=actor,
                    summary=summary,
                    outcome=_classify_outcome(kind, trace_outcome),
                    trace_id=trace_id,
                    session_id=session_id,
                    payload=dict(step),
                )
            )

    # Stable chronological ordering. Tie-breaker: a trace_meta header for a
    # given spawn always sorts before its own steps when timestamps collide,
    # then by the original insertion order encoded in the event id.
    def _sort_key(ev: TraceTimelineEvent) -> tuple[float, int, str]:
        meta_first = 0 if ev.kind == "trace_meta" else 1
        return (ev.ts, meta_first, ev.id)

    events.sort(key=_sort_key)
    return events


@router.get(
    "/tasks/{task_id}/trace",
    responses={404: {"description": "Task not found"}},
    include_in_schema=False,
)
@router.get(
    "/dashboard/tasks/{task_id}/trace",
    responses={404: {"description": "Task not found"}},
)
def task_trace(
    request: Request,
    task_id: str,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    cursor: Annotated[int, Query(ge=0)] = 0,
) -> TraceTimelineResponse:
    """Return the timeline of trace events for *task_id*.

    The endpoint is read-only and idempotent. A missing task returns 404; a
    valid task with no trace returns 200 + an empty events list (the FE
    renders an empty-state card in that case).
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    from bernstein.core.routes.task_crud import _require_task_access

    _require_task_access(task, request)

    workdir = _get_workdir(request)
    traces_dir = workdir / ".sdd" / "traces"
    traces = _read_trace_lines(traces_dir, task_id)
    events = _flatten_trace_events(traces)

    total = len(events)
    sliced = events[cursor : cursor + limit]
    next_cursor: int | None = cursor + len(sliced) if cursor + len(sliced) < total else None

    first_ts = events[0].ts if events else None
    last_ts = events[-1].ts if events else None
    has_open_trace = any(t.get("end_ts") is None for t in traces)

    return TraceTimelineResponse(
        task_id=task_id,
        events=sliced,
        total=total,
        cursor=next_cursor,
        first_ts=first_ts,
        last_ts=last_ts,
        has_open_trace=has_open_trace,
    )
