"""Operator-facing aggregation of supervisor signals.

The orchestrator already classifies stalls in three places:

* :mod:`bernstein.core.orchestration.stalled_manager` for manager
  sessions that never produced child tasks,
* :mod:`bernstein.core.orchestration.watchdog` for paused sessions
  awaiting a prompt,
* :class:`bernstein.core.agents.spawn_supervisor.SpawnSupervisor` for
  sessions that exhausted their respawn budget.

Each detector writes its findings to a well-known location under
``.sdd/runtime/``. This module reads those locations and surfaces a
single ``WorkerSupervisionSnapshot`` row per live worker so the
``bernstein supervisor status`` command, ``bernstein status`` /
``bernstein fleet`` summary line, and the TUI pane all consume the same
struct.

Design constraints:

* Pure read aggregator - never mutates orchestrator state.
* No network IO; no wall-clock arithmetic that affects the recommended
  action (the receipt module owns determinism).
* Tolerates missing inputs - a stale ``.sdd/runtime/`` directory still
  yields an empty :class:`SupervisorSnapshot` rather than raising.
"""

from __future__ import annotations

import json
import logging
import operator
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.orchestration.supervisor_receipt import (
    DEFAULT_RECEIPT_AUDIT_WINDOW,
    RecommendedAction,
    StallReason,
    recommend_action,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


__all__ = [
    "AGGREGATOR_SCHEMA_VERSION",
    "ParkedSessions",
    "SupervisorSnapshot",
    "WorkerSupervisionSnapshot",
    "aggregator_snapshot",
    "format_summary_line",
    "load_agents_snapshot",
    "load_heartbeat",
    "load_parked_sessions",
    "load_recent_failures",
    "snapshot_to_dict",
]


#: Schema version embedded in the aggregator's JSON output.
AGGREGATOR_SCHEMA_VERSION: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class WorkerSupervisionSnapshot:
    """One live worker's supervisor-facing view."""

    worker_id: str
    session_id: str
    role: str
    task_id: str
    worktree_id: str
    last_heartbeat_age_s: float | None
    is_stuck: bool
    stall_reason: StallReason
    recommended_action: RecommendedAction
    respawn_budget_remaining: int
    stuck_since_ts: float | None = None
    details: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    """Aggregated view across every live worker."""

    schema_version: str
    generated_ts: float
    workers: tuple[WorkerSupervisionSnapshot, ...]
    parked_available: bool = True

    @property
    def stuck_count(self) -> int:
        """Number of workers the aggregator classifies as stuck."""
        return sum(1 for w in self.workers if w.is_stuck)

    @property
    def oldest_stall_age_s(self) -> float | None:
        """Age of the oldest currently-stuck worker, in seconds.

        ``None`` when no worker is stuck or when ``stuck_since_ts`` is
        unavailable on every stuck row.
        """
        stuck = [w for w in self.workers if w.is_stuck and w.stuck_since_ts is not None]
        if not stuck:
            return None
        oldest = min(w.stuck_since_ts for w in stuck if w.stuck_since_ts is not None)
        return max(self.generated_ts - oldest, 0.0)


# ---------------------------------------------------------------------------
# Filesystem readers - each isolates one input so the aggregator can
# survive a missing or malformed file.
# ---------------------------------------------------------------------------


def load_agents_snapshot(workdir: Path) -> list[dict[str, Any]]:
    """Return the agent rows from ``.sdd/runtime/agents.json``.

    Returns an empty list when the file is missing, unparseable, or has
    no ``agents`` array. Never raises.
    """
    path = workdir / ".sdd" / "runtime" / "agents.json"
    if not path.exists():
        return []
    try:
        payload_any: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload_any, dict):
        return []
    raw = cast(dict[str, Any], payload_any).get("agents")
    if not isinstance(raw, list):
        return []
    raw_list = cast(list[Any], raw)
    return [cast(dict[str, Any], item) for item in raw_list if isinstance(item, dict)]


def load_heartbeat(workdir: Path, session_id: str) -> dict[str, Any] | None:
    """Return the latest heartbeat record for ``session_id``.

    Heartbeats are written by the wrapper script at
    ``.sdd/runtime/heartbeats/<session_id>.json``. Returns ``None`` when
    the file is missing or unreadable.
    """
    if not session_id:
        return None
    path = workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        payload_any: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload_any, dict):
        return None
    return cast(dict[str, Any], payload_any)


@dataclass(frozen=True, slots=True)
class ParkedSessions:
    """Result of :func:`load_parked_sessions`.

    ``available`` is False only when this run found no record of the
    spawn supervisor's parked-session store at all (the marker file is
    absent and the failures-log fallback found nothing), so a caller
    can render "0 parked" and "parked state unavailable" differently
    instead of treating both as the same silent zero.
    """

    available: bool
    session_ids: frozenset[str]

    def __bool__(self) -> bool:
        return bool(self.session_ids)

    def __len__(self) -> int:
        return len(self.session_ids)

    def __iter__(self) -> Iterator[str]:
        return iter(self.session_ids)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self.session_ids


def load_parked_sessions(workdir: Path) -> ParkedSessions:
    """Return the ids of sessions the spawn supervisor parked.

    Reads the marker file at
    ``.sdd/runtime/spawn_supervisor/parked.json`` written by the
    in-process supervisor. Falls back to the lifecycle-event log when
    the marker file is absent. ``ParkedSessions.available`` reflects
    whether either source found a run of this workspace's supervisor at
    all, not just whether it found any parked ids.
    """
    parked: set[str] = set()
    available = False
    marker = workdir / ".sdd" / "runtime" / "spawn_supervisor" / "parked.json"
    if marker.exists():
        try:
            payload_any: Any = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload_any = None
        else:
            available = True
        if isinstance(payload_any, dict):
            payload = cast(dict[str, Any], payload_any)
            ids = payload.get("session_ids")
            if isinstance(ids, list):
                ids_list = cast(list[Any], ids)
                parked.update(str(i) for i in ids_list if isinstance(i, str))
    # Lifecycle events fallback: scan the failures/ records.
    failures_dir = workdir / ".sdd" / "runtime" / "failures"
    if failures_dir.exists():
        with suppress(OSError):
            for entry in sorted(failures_dir.glob("*.json")):
                with suppress(OSError, json.JSONDecodeError):
                    record_any: Any = json.loads(entry.read_text(encoding="utf-8"))
                    if isinstance(record_any, dict):
                        record = cast(dict[str, Any], record_any)
                        if record.get("kind") == "respawn_exhausted":
                            sid = record.get("session_id")
                            if isinstance(sid, str):
                                available = True
                                parked.add(sid)
    return ParkedSessions(available=available, session_ids=frozenset(parked))


def load_recent_failures(
    workdir: Path,
    session_id: str,
    *,
    limit: int = DEFAULT_RECEIPT_AUDIT_WINDOW,
) -> list[dict[str, Any]]:
    """Return the trailing ``limit`` failure records for ``session_id``.

    The failures dir is the canonical sink for stalled-manager and
    spawn-supervisor diagnostics. The function reads at most ``limit``
    records so callers can feed the result straight into the receipt
    assembly path.
    """
    failures_dir = workdir / ".sdd" / "runtime" / "failures"
    if not failures_dir.exists() or not session_id:
        return []
    records: list[dict[str, Any]] = []
    try:
        entries = sorted(failures_dir.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            payload_any: Any = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload_any, dict):
            continue
        payload = cast(dict[str, Any], payload_any)
        if payload.get("session_id") != session_id:
            continue
        records.append(payload)
    return records[-limit:]


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _classify_stall(
    *,
    agent: dict[str, Any],
    heartbeat: dict[str, Any] | None,
    is_parked: bool,
    heartbeat_stale_s: float,
    now: float,
) -> tuple[bool, StallReason, float | None, float | None]:
    """Return ``(is_stuck, stall_reason, age, stuck_since_ts)``.

    Pure heuristic over the supplied snapshot inputs. The function does
    *not* read from disk or the clock beyond the ``now`` argument the
    caller provides; the upstream detector still owns the authoritative
    stall classification - the aggregator merely surfaces it.
    """
    if is_parked:
        return True, StallReason.RESPAWN_EXHAUSTED, None, None

    last_hb = heartbeat.get("timestamp") if isinstance(heartbeat, dict) else None
    age: float | None = None
    if isinstance(last_hb, (int, float)) and last_hb > 0:
        age = max(now - float(last_hb), 0.0)

    status = str(agent.get("status", ""))
    if status == "parked":
        return True, StallReason.RESPAWN_EXHAUSTED, age, _coerce_ts(agent.get("parked_ts"))

    # Specific detector signals win over the generic heartbeat threshold:
    # the upstream detectors classify the stall with more context than the
    # liveness probe (e.g. a stalled manager has *also* gone heartbeat-
    # stale, but the operator wants the structural reason on the wire).
    role = str(agent.get("role", ""))
    if role == "manager":
        manager_diagnostic_any: Any = agent.get("stalled_manager")
        if isinstance(manager_diagnostic_any, dict):
            manager_diagnostic = cast(dict[str, Any], manager_diagnostic_any)
            stuck_since = _coerce_ts(manager_diagnostic.get("detected_at"))
            return True, StallReason.MANAGER_NO_CHILDREN, age, stuck_since

    awaiting_prompt = agent.get("awaiting_model_question")
    if awaiting_prompt:
        return True, StallReason.WATCHDOG_MODEL_QUESTION, age, _coerce_ts(agent.get("paused_ts"))

    if age is not None and age >= heartbeat_stale_s:
        return True, StallReason.HEARTBEAT_STALE, age, float(last_hb) if last_hb else None

    if status == "no_progress":
        return True, StallReason.NO_PROGRESS, age, _coerce_ts(agent.get("no_progress_since"))

    return False, StallReason.UNKNOWN, age, None


def _coerce_ts(value: Any) -> float | None:
    """Best-effort coercion of a JSON value into a unix timestamp."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def aggregator_snapshot(
    workdir: Path,
    *,
    now: float | None = None,
    heartbeat_stale_s: float = 120.0,
    default_respawn_budget: int = 3,
) -> SupervisorSnapshot:
    """Return the cross-worker supervisor snapshot.

    Args:
        workdir: Workspace root (the directory containing ``.sdd/``).
        now: Optional unix-timestamp override (tests). Defaults to
            :func:`time.time`. The snapshot embeds this value in
            :attr:`SupervisorSnapshot.generated_ts`.
        heartbeat_stale_s: Threshold above which a heartbeat age makes
            the worker count as stuck. Pulled from
            :mod:`bernstein.core.defaults` by callers; supplied here so
            tests can stub it without monkey-patching.
        default_respawn_budget: Respawn budget assumed when an agent
            row does not carry an explicit remaining counter.

    Returns:
        A :class:`SupervisorSnapshot` carrying one row per live worker.
    """
    resolved_now = now if now is not None else time.time()
    agents = load_agents_snapshot(workdir)
    parked = load_parked_sessions(workdir)

    rows: list[WorkerSupervisionSnapshot] = []
    for agent in agents:
        session_id = str(agent.get("id", ""))
        if not session_id:
            continue
        if str(agent.get("status", "")) == "dead":
            continue
        heartbeat = load_heartbeat(workdir, session_id)
        is_parked = session_id in parked
        is_stuck, stall_reason, age, stuck_since = _classify_stall(
            agent=agent,
            heartbeat=heartbeat,
            is_parked=is_parked,
            heartbeat_stale_s=heartbeat_stale_s,
            now=resolved_now,
        )

        # Respawn budget: the agent row may carry an explicit override.
        budget_raw = agent.get("respawn_budget_remaining")
        if isinstance(budget_raw, int) and budget_raw >= 0:
            budget = budget_raw
        elif is_parked:
            budget = 0
        else:
            budget = default_respawn_budget

        # Audit slice for the deterministic recommended action: failures
        # already classified for this session.
        slice_entries: list[dict[str, Any]] = [
            {
                "event_type": str(record.get("kind", "")),
                "session_id": session_id,
                "details": record,
            }
            for record in load_recent_failures(workdir, session_id)
        ]

        action = recommend_action(
            stall_reason if is_stuck else StallReason.UNKNOWN,
            slice_entries,
            respawn_budget_remaining=budget,
        )

        rows.append(
            WorkerSupervisionSnapshot(
                worker_id=str(agent.get("worker_id", session_id[:12])),
                session_id=session_id,
                role=str(agent.get("role", "")),
                task_id=_extract_task_id(agent),
                worktree_id=str(agent.get("worktree_id", "")),
                last_heartbeat_age_s=age,
                is_stuck=is_stuck,
                stall_reason=stall_reason,
                recommended_action=action,
                respawn_budget_remaining=budget,
                stuck_since_ts=stuck_since,
                details={"status": str(agent.get("status", ""))},
            )
        )

    rows.sort(key=operator.attrgetter("session_id"))
    return SupervisorSnapshot(
        schema_version=AGGREGATOR_SCHEMA_VERSION,
        generated_ts=resolved_now,
        workers=tuple(rows),
        parked_available=parked.available,
    )


def _extract_task_id(agent: dict[str, Any]) -> str:
    raw = agent.get("task_ids")
    if isinstance(raw, list) and raw:
        raw_list = cast(list[Any], raw)
        first = raw_list[0]
        if isinstance(first, str):
            return first
    direct = agent.get("task_id")
    if isinstance(direct, str):
        return direct
    return ""


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def snapshot_to_dict(snapshot: SupervisorSnapshot) -> dict[str, Any]:
    """Return the canonical JSON dict view of a snapshot.

    The shape matches ``docs/api/supervisor.md``.
    """
    return {
        "schema_version": snapshot.schema_version,
        "generated_ts": snapshot.generated_ts,
        "stuck_count": snapshot.stuck_count,
        "oldest_stall_age_s": snapshot.oldest_stall_age_s,
        "parked_available": snapshot.parked_available,
        "workers": [
            {
                "worker_id": w.worker_id,
                "session_id": w.session_id,
                "role": w.role,
                "task_id": w.task_id,
                "worktree_id": w.worktree_id,
                "last_heartbeat_age_s": w.last_heartbeat_age_s,
                "is_stuck": w.is_stuck,
                "stall_reason": w.stall_reason.value,
                "recommended_action": w.recommended_action.value,
                "respawn_budget_remaining": w.respawn_budget_remaining,
                "stuck_since_ts": w.stuck_since_ts,
                "details": w.details,
            }
            for w in snapshot.workers
        ],
    }


def format_summary_line(snapshot: SupervisorSnapshot) -> str:
    """Return a one-line summary for ``bernstein status`` / ``fleet``.

    Format::

        supervisor: 2 stuck (oldest 95s)
        supervisor: 0 stuck
        supervisor: 0 stuck, parked state unavailable

    The ``parked state unavailable`` suffix appears whenever this run
    found no record of the spawn supervisor's parked-session store, so
    "0 stuck" alone never has to stand in for "never checked".
    """
    stuck = snapshot.stuck_count
    suffix = "" if snapshot.parked_available else ", parked state unavailable"
    if stuck == 0:
        return f"supervisor: 0 stuck{suffix}"
    oldest = snapshot.oldest_stall_age_s
    if oldest is None:
        return f"supervisor: {stuck} stuck{suffix}"
    return f"supervisor: {stuck} stuck (oldest {int(oldest)}s){suffix}"
