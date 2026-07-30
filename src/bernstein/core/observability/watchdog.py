"""Three-tier watchdog orchestration for live agent incidents.

Tier 1 uses deterministic runtime signals (heartbeat, log growth, progress
stalls). Tier 2 creates a reviewer task for AI triage. Tier 3 escalates the
incident to humans via the existing notification and bulletin channels.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.defaults import AGENT
from bernstein.core.heartbeat import HeartbeatMonitor, compute_stall_profile
from bernstein.core.tasks.auto_spawn_guard import AutoSpawnGuard

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

WatchdogSeverity = Literal["medium", "high", "critical"]
WatchdogSource = Literal["heartbeat", "log_growth", "progress_stall"]


# Shared cast-type constants to avoid string duplication (Sonar S1192).
type _CAST_DICT_STR_OBJ = dict[str, object]

# Heartbeat phases that mean the agent is still producing its FIRST turn. A
# non-heartbeat adapter (consumes_heartbeat_dir=False) has only its spawn-time
# heartbeat on disk during this phase, so heartbeat age == wall-clock since
# spawn; a slow/free model that deliberates past the normal stale threshold
# must be judged on log/git liveness, not reaped on heartbeat age (issue #3012).
_STARTING_PHASES: frozenset[str] = frozenset(
    {"starting", "start", "init", "initializing", "spawning", "spawn", "boot", "booting"}
)


def _is_starting_phase(phase: str | None) -> bool:
    return phase is not None and phase.strip().lower() in _STARTING_PHASES


def _log_signal_is_fresh(log_age_s: float | None) -> bool:
    """Whether a log mtime proves the agent is alive within the grace window.

    A log/git-tree write fresher than ``AGENT.liveness_grace_s`` is a positive
    liveness signal that SUPPRESSES the heartbeat-staleness incident (issue
    #3012): the agent is demonstrably alive regardless of heartbeat age.
    """
    return log_age_s is not None and log_age_s < AGENT.liveness_grace_s


@dataclass(frozen=True)
class WatchdogFinding:
    """A mechanical incident detected for a live agent/task pair."""

    key: str
    session_id: str
    task_id: str
    source: WatchdogSource
    severity: WatchdogSeverity
    summary: str
    detail: str
    role: str = "reviewer"
    # Title of the task this finding is about. Used by AutoSpawnGuard to
    # compute ancestry depth (e.g. a finding about a task whose title already
    # starts with "Watchdog triage:" is itself depth-1, so a new triage task
    # about it would be depth 2 and gets refused).
    task_title: str = ""


@dataclass
class WatchdogIncident:
    """Persisted state for one active watchdog incident."""

    key: str
    session_id: str
    task_id: str
    source: WatchdogSource
    severity: WatchdogSeverity
    summary: str
    detail: str
    count: int
    first_seen_ts: float
    last_seen_ts: float
    triage_task_id: str | None = None
    escalated: bool = False


def _check_session_findings(
    session_id: str,
    task_id: str,
    task: Any,
    runtime_s: float,
    hb_status: Any,
    timeout_s: float,
    current_line: int,
    no_growth_ticks: int,
    stall_counts: dict[str, int],
    findings: dict[str, WatchdogFinding],
    *,
    log_age_s: float | None = None,
    starting_timeout_s: float | None = None,
) -> None:
    """Check a single session for watchdog-worthy findings."""
    stall_count = int(stall_counts.get(task_id, 0))
    if stall_count > 0:
        profile = compute_stall_profile(task, hb_status, None)
        if stall_count >= profile.wakeup_threshold:
            severity: WatchdogSeverity = "medium"
            if stall_count >= profile.shutdown_threshold:
                severity = "high"
            if stall_count >= profile.kill_threshold:
                severity = "critical"
            title = task.title if task is not None else task_id
            findings[f"progress_stall:{session_id}:{task_id}"] = WatchdogFinding(
                key=f"progress_stall:{session_id}:{task_id}",
                session_id=session_id,
                task_id=task_id,
                source="progress_stall",
                severity=severity,
                summary=f"Agent stalled on task {title}",
                detail=(
                    f"Tier-1 watchdog saw {stall_count} identical progress snapshots for task {task_id}. "
                    f"Adaptive profile: wakeup={profile.wakeup_threshold}, "
                    f"shutdown={profile.shutdown_threshold}, kill={profile.kill_threshold} ({profile.reason})."
                ),
                task_title=str(title),
            )
            return

    _check_heartbeat_findings(
        session_id,
        task_id,
        task,
        runtime_s,
        hb_status,
        timeout_s,
        current_line,
        findings,
        log_age_s=log_age_s,
        starting_timeout_s=starting_timeout_s,
    )
    _check_log_growth_findings(session_id, task_id, task, runtime_s, current_line, no_growth_ticks, findings)


def _check_heartbeat_findings(
    session_id: str,
    task_id: str,
    task: Any,
    runtime_s: float,
    hb_status: Any,
    timeout_s: float,
    current_line: int,
    findings: dict[str, WatchdogFinding],
    *,
    log_age_s: float | None = None,
    starting_timeout_s: float | None = None,
) -> None:
    """Check heartbeat-related findings for a session.

    A log/git mtime fresher than ``AGENT.liveness_grace_s`` is treated as a
    POSITIVE liveness signal that suppresses the heartbeat-staleness incident
    entirely -- not merely defers it: the agent is demonstrably alive even if
    its heartbeat is stale (issue #3012). While the agent is still in the
    ``starting`` phase the (larger, configurable) ``starting_timeout_s`` is
    used so a slow/free model's first turn is not a hard 120s cap.
    """
    # Fresh log/git write within the grace window -> the agent is alive; never
    # raise a heartbeat-staleness incident against a demonstrably-live agent.
    if _log_signal_is_fresh(log_age_s):
        return

    # A `starting` agent (only its spawn-time heartbeat on disk for a
    # non-heartbeat adapter) is judged against the larger starting-phase
    # window, not the general stale threshold.
    effective_timeout = timeout_s
    if starting_timeout_s is not None and _is_starting_phase(getattr(hb_status, "phase", None)):
        effective_timeout = max(timeout_s, starting_timeout_s)

    if hb_status.last_heartbeat is None:
        if runtime_s >= max(effective_timeout / 2.0, 60.0) and current_line == 0:
            title = task.title if task is not None else task_id
            findings[f"heartbeat:{session_id}:{task_id}"] = WatchdogFinding(
                key=f"heartbeat:{session_id}:{task_id}",
                session_id=session_id,
                task_id=task_id,
                source="heartbeat",
                severity="high",
                summary=f"Agent silent on task {title}",
                detail=(
                    f"Tier-1 watchdog found no heartbeat file and no log activity for {runtime_s:.0f}s "
                    f"while task {task_id} remains active."
                ),
                task_title=str(title),
            )
        return

    if hb_status.age_seconds >= max(effective_timeout / 2.0, 60.0):
        severity: WatchdogSeverity = "critical" if hb_status.age_seconds >= effective_timeout else "high"
        title = task.title if task is not None else task_id
        findings[f"heartbeat:{session_id}:{task_id}"] = WatchdogFinding(
            key=f"heartbeat:{session_id}:{task_id}",
            session_id=session_id,
            task_id=task_id,
            source="heartbeat",
            severity=severity,
            summary=f"Heartbeat stale for task {title}",
            detail=(
                f"Tier-1 watchdog observed heartbeat age {hb_status.age_seconds:.0f}s "
                f"for task {task_id} (timeout={effective_timeout:.0f}s, phase={hb_status.phase or 'unknown'})."
            ),
            task_title=str(title),
        )


def _check_log_growth_findings(
    session_id: str,
    task_id: str,
    task: Any,
    runtime_s: float,
    current_line: int,
    no_growth_ticks: int,
    findings: dict[str, WatchdogFinding],
) -> None:
    """Check log-growth-related findings for a session."""
    if runtime_s < 60.0 or current_line <= 0 or no_growth_ticks < 3:
        return
    severity: WatchdogSeverity = "high" if no_growth_ticks >= 5 else "medium"
    title = task.title if task is not None else task_id
    findings[f"log_growth:{session_id}:{task_id}"] = WatchdogFinding(
        key=f"log_growth:{session_id}:{task_id}",
        session_id=session_id,
        task_id=task_id,
        source="log_growth",
        severity=severity,
        summary=f"Agent log stopped growing for task {title}",
        detail=(
            f"Tier-1 watchdog saw no new log lines for {no_growth_ticks} consecutive ticks "
            f"(current_line={current_line}, runtime={runtime_s:.0f}s) on task {task_id}."
        ),
        task_title=str(title),
    )


def collect_watchdog_findings(orch: Any) -> list[WatchdogFinding]:
    """Collect live watchdog findings from the orchestrator state."""
    workdir = getattr(orch, "_workdir", None)
    if not isinstance(workdir, Path):
        return []

    agents_raw = getattr(orch, "_agents", {})
    if not isinstance(agents_raw, dict):
        return []
    agents = cast("dict[str, Any]", agents_raw)

    config = getattr(orch, "_config", None)
    timeout_s = float(getattr(config, "heartbeat_timeout_s", 120))
    starting_timeout_s = float(getattr(config, "heartbeat_starting_timeout_s", AGENT.heartbeat_starting_timeout_s))
    monitor = HeartbeatMonitor(workdir, timeout_s=timeout_s)
    logs = AgentLogAggregator(workdir)
    now = time.time()

    latest_tasks_raw = getattr(orch, "_latest_tasks_by_id", {})
    latest_tasks = cast("dict[str, Task]", latest_tasks_raw) if isinstance(latest_tasks_raw, dict) else {}

    log_state_raw = getattr(orch, "_watchdog_log_state", None)
    if not isinstance(log_state_raw, dict):
        log_state_raw = {}
        cast("dict[str, Any]", orch.__dict__)["_watchdog_log_state"] = log_state_raw
    log_state = cast(_CAST_DICT_STR_OBJ, log_state_raw)

    stall_counts_raw = getattr(orch, "_stall_counts", {})
    stall_counts = cast("dict[str, int]", stall_counts_raw) if isinstance(stall_counts_raw, dict) else {}

    active_session_ids: set[str] = set()
    findings: dict[str, WatchdogFinding] = {}

    for session in agents.values():
        if getattr(session, "status", "") == "dead":
            continue
        session_id = str(getattr(session, "id", ""))
        if not session_id:
            continue
        active_session_ids.add(session_id)

        task_ids_raw = getattr(session, "task_ids", [])
        if not isinstance(task_ids_raw, list) or not task_ids_raw:
            continue
        task_ids = cast("list[object]", task_ids_raw)
        task_id = str(task_ids[0])
        if not task_id:
            continue

        runtime_s = max(now - float(getattr(session, "spawn_ts", now)), 0.0)
        hb_status = monitor.check(session_id)
        log_summary = logs.parse_log(session_id)
        log_age_s = logs.log_age_s(session_id, now)
        task = latest_tasks.get(task_id)

        prev_line, prev_no_growth = _coerce_log_state(log_state.get(session_id))
        current_line = log_summary.last_activity_line
        no_growth_ticks = prev_no_growth + 1 if current_line > 0 and current_line <= prev_line else 0
        log_state[session_id] = (current_line, no_growth_ticks)

        _check_session_findings(
            session_id,
            task_id,
            task,
            runtime_s,
            hb_status,
            timeout_s,
            current_line,
            no_growth_ticks,
            stall_counts,
            findings,
            log_age_s=log_age_s,
            starting_timeout_s=starting_timeout_s,
        )

    for session_id in list(log_state.keys()):
        if session_id not in active_session_ids:
            log_state.pop(session_id, None)

    return list(findings.values())


class WatchdogManager:
    """Persist and escalate three-tier watchdog incidents."""

    def __init__(
        self,
        workdir: Path,
        client: httpx.Client,
        server_url: str,
        *,
        notify: Callable[..., None] | None = None,
        post_bulletin: Callable[[str, str], None] | None = None,
        max_auto_spawns_per_run: int = 3,
    ) -> None:
        self._workdir = workdir
        self._client = client
        self._server_url = server_url.rstrip("/")
        self._notify = notify
        self._post_bulletin = post_bulletin
        self._state_path = workdir / ".sdd" / "runtime" / "watchdog_state.json"
        self._events_path = workdir / ".sdd" / "runtime" / "watchdog_incidents.jsonl"
        self._auto_spawn_guard = AutoSpawnGuard(workdir, max_auto_spawns_per_run=max_auto_spawns_per_run)

    def sync(self, findings: list[WatchdogFinding]) -> None:
        """Sync current findings into persisted incident state."""
        state = self._load_state()
        active: dict[str, WatchdogIncident] = {}
        now = time.time()

        deduped = {finding.key: finding for finding in findings}
        for finding in deduped.values():
            incident = state.get(finding.key)
            if incident is None:
                incident = WatchdogIncident(
                    key=finding.key,
                    session_id=finding.session_id,
                    task_id=finding.task_id,
                    source=finding.source,
                    severity=finding.severity,
                    summary=finding.summary,
                    detail=finding.detail,
                    count=1,
                    first_seen_ts=now,
                    last_seen_ts=now,
                )
                self._append_event("detected", incident)
                logger.warning(
                    "watchdog TIER1 detected: key=%s source=%s severity=%s task=%s session=%s summary=%s",
                    incident.key,
                    incident.source,
                    incident.severity,
                    incident.task_id,
                    incident.session_id,
                    incident.summary,
                )
            else:
                incident.count += 1
                incident.last_seen_ts = now
                incident.source = finding.source
                incident.severity = finding.severity
                incident.summary = finding.summary
                incident.detail = finding.detail

            if incident.triage_task_id is None:
                # Bug fix (2026-07-04): dedupe must also cover incidents
                # created EARLIER IN THIS SAME sync() call. Those live only
                # in ``active`` (not yet persisted to ``state`` - that
                # happens once, after the loop, via ``self._save_state``),
                # so scanning ``state.values()`` alone missed same-pass
                # duplicates and could create two "Watchdog triage: ..."
                # tasks with the same title in one sync.
                existing_open_titles = [
                    f"Watchdog triage: {other.summary}"
                    for other in itertools.chain(state.values(), active.values())
                    if other.triage_task_id is not None and other.key != finding.key
                ]
                triage_task_id = self._create_triage_task(finding, existing_open_titles)
                if triage_task_id:
                    incident.triage_task_id = triage_task_id
                    self._append_event("triage_created", incident)

            if not incident.escalated and incident.count >= _human_escalation_threshold(incident.severity):
                threshold = _human_escalation_threshold(incident.severity)
                logger.warning(
                    "watchdog TIER3 escalating to human: key=%s count=%d threshold=%d severity=%s "
                    "task=%s session=%s triage_task_id=%s",
                    incident.key,
                    incident.count,
                    threshold,
                    incident.severity,
                    incident.task_id,
                    incident.session_id,
                    incident.triage_task_id or "n/a",
                )
                self._escalate_human(incident)
                incident.escalated = True
                self._append_event("human_escalated", incident)

            active[finding.key] = incident

        self._save_state(active)

    def _create_triage_task(self, finding: WatchdogFinding, existing_open_titles: list[str]) -> str | None:
        """Create a Tier-2 reviewer task for AI triage.

        Gated by :class:`AutoSpawnGuard` to prevent the "watchdog triage of
        watchdog triage" recursion and unbounded triage-task growth observed
        in production (see module docstring of ``auto_spawn_guard``).
        """
        title = f"Watchdog triage: {finding.summary}"
        decision = self._auto_spawn_guard.evaluate(
            kind="watchdog_triage",
            title=title,
            source_title=finding.task_title,
            existing_open_titles=existing_open_titles,
        )
        if not decision.allowed:
            logger.info(
                "watchdog TIER2 triage suppressed by AutoSpawnGuard: key=%s title=%r reason=%s",
                finding.key,
                title,
                getattr(decision, "reason", "<no reason attr>"),
            )
            return None
        payload = {
            "title": title,
            "description": (
                "Tier-2 watchdog triage.\n\n"
                "A Tier-1 mechanical watchdog detected a live agent problem before merge.\n"
                "Inspect the task, agent output, and recent logs. Decide whether the work should be retried, "
                "failed, or escalated further.\n\n"
                f"Session: {finding.session_id}\n"
                f"Task: {finding.task_id}\n"
                f"Source: {finding.source}\n"
                f"Severity: {finding.severity}\n"
                f"Summary: {finding.summary}\n"
                f"Detail: {finding.detail}\n"
            ),
            "role": finding.role,
            "priority": _priority_for_severity(finding.severity),
            "scope": "small",
            "complexity": "medium",
            "estimated_minutes": 15,
            "model": "sonnet",
            "effort": "medium",
            "batch_eligible": False,
        }
        try:
            resp = self._client.post(f"{self._server_url}/tasks", json=payload)
            resp.raise_for_status()
            raw: Any = resp.json()
        except Exception as exc:
            logger.warning("Watchdog failed to create triage task for %s: %s", finding.key, exc)
            return None
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not task_id:
            return None
        logger.info("Watchdog created triage task %s for %s", task_id, finding.key)
        return task_id

    def _escalate_human(self, incident: WatchdogIncident) -> None:
        """Escalate a repeated/critical incident to humans."""
        alert = (
            f"Watchdog escalation [{incident.severity}] {incident.summary} "
            f"(task={incident.task_id}, session={incident.session_id}, triage={incident.triage_task_id or 'n/a'})"
        )
        if self._post_bulletin is not None:
            self._post_bulletin("alert", alert)
        if self._notify is not None:
            self._notify(
                "approval.needed",
                "Watchdog escalation",
                incident.detail,
                severity=incident.severity,
                source=incident.source,
                task_id=incident.task_id,
                session_id=incident.session_id,
                triage_task_id=incident.triage_task_id or "",
            )

    def _load_state(self) -> dict[str, WatchdogIncident]:
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Watchdog state unreadable, starting fresh: %s", self._state_path)
            return {}
        if not isinstance(raw, dict):
            return {}
        incidents: dict[str, WatchdogIncident] = {}
        for key, value in cast(_CAST_DICT_STR_OBJ, raw).items():
            incident = _incident_from_raw(key, value)
            if incident is not None:
                incidents[key] = incident
        return incidents

    def _save_state(self, incidents: dict[str, WatchdogIncident]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: asdict(value) for key, value in incidents.items()}
        self._state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _append_event(self, event: str, incident: WatchdogIncident) -> None:
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "timestamp": time.time(),
            "event": event,
            "incident": asdict(incident),
        }
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")


def _coerce_log_state(raw: object) -> tuple[int, int]:
    if isinstance(raw, tuple):
        tuple_values = cast("tuple[object, ...]", raw)
        if len(tuple_values) == 2:
            first = _safe_int(tuple_values[0])
            second = _safe_int(tuple_values[1])
            if first is not None and second is not None:
                return first, second
    if isinstance(raw, list):
        list_values = cast("list[object]", raw)
        if len(list_values) == 2:
            first = _safe_int(list_values[0])
            second = _safe_int(list_values[1])
            if first is not None and second is not None:
                return first, second
    return 0, 0


def _incident_from_raw(key: str, raw: object) -> WatchdogIncident | None:
    if not isinstance(raw, dict):
        return None
    data = cast(_CAST_DICT_STR_OBJ, raw)
    session_id = data.get("session_id")
    task_id = data.get("task_id")
    source = data.get("source")
    severity = data.get("severity")
    summary = data.get("summary")
    detail = data.get("detail")
    triage_task_id = data.get("triage_task_id")

    if not isinstance(session_id, str) or not isinstance(task_id, str):
        return None
    if source not in {"heartbeat", "log_growth", "progress_stall"}:
        return None
    if severity not in {"medium", "high", "critical"}:
        return None
    if not isinstance(summary, str) or not isinstance(detail, str):
        return None
    if triage_task_id is not None and not isinstance(triage_task_id, str):
        return None

    count = _safe_int(data.get("count", 1))
    first_seen_ts = _safe_float(data.get("first_seen_ts", 0.0))
    last_seen_ts = _safe_float(data.get("last_seen_ts", 0.0))
    escalated = data.get("escalated", False)
    if count is None or first_seen_ts is None or last_seen_ts is None:
        return None

    return WatchdogIncident(
        key=key,
        session_id=session_id,
        task_id=task_id,
        source=cast("WatchdogSource", source),
        severity=cast("WatchdogSeverity", severity),
        summary=summary,
        detail=detail,
        count=count,
        first_seen_ts=first_seen_ts,
        last_seen_ts=last_seen_ts,
        triage_task_id=triage_task_id,
        escalated=bool(escalated),
    )


def _priority_for_severity(severity: WatchdogSeverity) -> int:
    return 1 if severity in {"high", "critical"} else 2


def _human_escalation_threshold(severity: WatchdogSeverity) -> int:
    return 2 if severity in {"high", "critical"} else 3


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
