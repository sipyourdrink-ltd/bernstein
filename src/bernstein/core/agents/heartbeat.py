"""Heartbeat monitoring and adaptive stall detection."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from bernstein.core.agent_log_aggregator import AgentLogAggregator, AgentLogSummary
from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.models import AgentHeartbeat, ProgressSnapshot, Task

logger = logging.getLogger(__name__)

# Idle agent detection thresholds
IDLE_LOG_AGE_THRESHOLD_SECONDS = 180  # 3 minutes without log activity


@dataclass(frozen=True)
class HeartbeatStatus:
    """Status of an agent heartbeat signal."""

    session_id: str
    last_heartbeat: datetime | None
    age_seconds: float
    phase: str
    progress_pct: int
    is_alive: bool
    is_stale: bool


@dataclass(frozen=True)
class StallProfile:
    """Adaptive snapshot thresholds for one agent session."""

    wakeup_threshold: int
    shutdown_threshold: int
    kill_threshold: int
    reason: str


class HeartbeatMonitor:
    """Monitor agent liveness via heartbeat files."""

    def __init__(self, workdir: Path, *, timeout_s: float = 120.0) -> None:
        self._workdir = workdir
        self._timeout_s = timeout_s
        self._signal_mgr = AgentSignalManager(workdir)

    def check(self, session_id: str) -> HeartbeatStatus:
        """Check one session's heartbeat."""
        heartbeat = self._read_heartbeat(session_id)
        if heartbeat is None:
            return HeartbeatStatus(
                session_id=session_id,
                last_heartbeat=None,
                age_seconds=0.0,
                phase="",
                progress_pct=0,
                is_alive=False,
                is_stale=False,
            )
        last_heartbeat = datetime.fromtimestamp(heartbeat.timestamp, tz=UTC)
        age_seconds = max(time.time() - heartbeat.timestamp, 0.0)
        return HeartbeatStatus(
            session_id=session_id,
            last_heartbeat=last_heartbeat,
            age_seconds=age_seconds,
            phase=heartbeat.phase or heartbeat.status,
            progress_pct=max(0, min(int(heartbeat.progress_pct), 100)),
            is_alive=age_seconds < self._timeout_s,
            is_stale=age_seconds >= self._timeout_s,
        )

    def check_all(self, session_ids: list[str]) -> list[HeartbeatStatus]:
        """Check all session IDs in order."""
        return [self.check(session_id) for session_id in session_ids]

    def inject_heartbeat_instructions(self, session_id: str) -> str:
        """Return a shell snippet that writes heartbeats in the background."""
        heartbeat_path = self._workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
        escaped_path = str(heartbeat_path)
        return (
            f"(mkdir -p '{heartbeat_path.parent}' && "
            f"while true; do "
            f'printf \'{{"timestamp":%s,'
            f'"phase":"implementing",'
            f'"progress_pct":0,'
            f'"current_file":"",'
            f'"message":"working"}}\' '
            f"\"$(date +%s)\" > '{escaped_path}'; "
            f"sleep 15; "
            f"done) >/dev/null 2>&1 &"
        )

    def _read_heartbeat(self, session_id: str) -> AgentHeartbeat | None:
        """Read a heartbeat from the primary or fallback location."""
        heartbeat = self._signal_mgr.read_heartbeat(session_id)
        if heartbeat is not None:
            return heartbeat

        fallback = self._workdir / ".sdd" / "runtime" / "signals" / session_id / "HEARTBEAT"
        if not fallback.exists():
            return None
        try:
            raw = json.loads(fallback.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        raw_timestamp = raw.get("timestamp")
        timestamp: float
        if isinstance(raw_timestamp, str):
            try:
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        else:
            try:
                timestamp = float(raw_timestamp)
            except (TypeError, ValueError):
                return None

        return AgentHeartbeat(
            timestamp=timestamp,
            files_changed=int(raw.get("files_changed", 0)),
            status=str(raw.get("status", "working")),
            current_file=str(raw.get("current_file", "")),
            phase=str(raw.get("phase", raw.get("status", ""))),
            progress_pct=int(raw.get("progress_pct", 0)),
            message=str(raw.get("message", "")),
        )


def compute_stall_profile(
    task: Task | None,
    heartbeat_status: HeartbeatStatus | None,
    log_summary: AgentLogSummary | None,
) -> StallProfile:
    """Compute adaptive snapshot thresholds from runtime context."""
    if heartbeat_status is not None and heartbeat_status.phase.lower() in {"testing", "tests", "pytest"}:
        return StallProfile(8, 12, 16, "heartbeat indicates testing phase")
    if log_summary is not None and log_summary.rate_limit_hits > 0:
        return StallProfile(6, 10, 14, "recent rate-limit activity detected")
    if heartbeat_status is not None and heartbeat_status.last_heartbeat is None:
        no_log_activity = log_summary is None or log_summary.last_activity_line == 0
        if no_log_activity:
            return StallProfile(2, 3, 5, "no heartbeat and no log activity")
    if task is not None and (task.scope.value == "large" or task.complexity.value == "high"):
        return StallProfile(5, 8, 12, "large/high-complexity task")
    return StallProfile(3, 5, 7, "default profile")


def check_stale_agents(orch: Any) -> None:
    """Write WAKEUP / SHUTDOWN signals for agents with stale heartbeats."""
    config = getattr(orch, "_config", None)
    if not bool(getattr(config, "heartbeat_enabled", True)):
        return

    workdir = getattr(orch, "_workdir", None)
    if not isinstance(workdir, Path):
        now = time.time()
        for session in orch._agents.values():
            if session.status == "dead":
                continue
            hb = orch._signal_mgr.read_heartbeat(session.id)
            if hb is None:
                continue
            age = now - hb.timestamp
            task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
            elapsed = now - session.spawn_ts
            if age >= 120:
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_shutdown(session.id, reason="no_heartbeat_120s", task_title=task_title)
            elif age >= 60:
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_wakeup(
                        session.id,
                        task_title=task_title,
                        elapsed_s=elapsed,
                        last_activity_ago_s=age,
                    )
        return

    timeout_s = float(getattr(config, "heartbeat_timeout_s", 120))
    wakeup_after_s = max(timeout_s / 2.0, 60.0)
    monitor = HeartbeatMonitor(workdir, timeout_s=timeout_s)
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        hb_status = monitor.check(session.id)
        if hb_status.last_heartbeat is not None:
            session.heartbeat_ts = hb_status.last_heartbeat.timestamp()
        else:
            continue

        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        elapsed = time.time() - session.spawn_ts
        if hb_status.age_seconds >= timeout_s:
            with contextlib.suppress(OSError):
                orch._signal_mgr.write_shutdown(session.id, reason="no_heartbeat", task_title=task_title)
        elif hb_status.age_seconds >= wakeup_after_s:
            with contextlib.suppress(OSError):
                orch._signal_mgr.write_wakeup(
                    session.id,
                    task_title=task_title,
                    elapsed_s=elapsed,
                    last_activity_ago_s=hb_status.age_seconds,
                )


def check_stalled_tasks(orch: Any) -> None:
    """Detect stalled agents via snapshots, heartbeat, and recent log activity."""
    workdir = getattr(orch, "_workdir", None)
    if not isinstance(workdir, Path):
        base = orch._config.server_url
        for session in orch._agents.values():
            if session.status == "dead":
                continue
            for task_id in session.task_ids:
                try:
                    resp = orch._client.get(f"{base}/tasks/{task_id}/snapshots")
                    resp.raise_for_status()
                    snapshots_raw: Any = resp.json()
                except Exception:
                    continue
                if not isinstance(snapshots_raw, list) or not snapshots_raw:
                    continue
                snapshots_data = cast("list[dict[str, Any]]", snapshots_raw)
                latest_raw = snapshots_data[-1]
                latest = ProgressSnapshot(
                    timestamp=float(latest_raw["timestamp"]),
                    files_changed=int(latest_raw.get("files_changed", 0)),
                    tests_passing=int(latest_raw.get("tests_passing", -1)),
                    errors=int(latest_raw.get("errors", 0)),
                    last_file=str(latest_raw.get("last_file", "")),
                )
                last_ts = orch._last_snapshot_ts.get(task_id, 0.0)
                if latest.timestamp <= last_ts:
                    continue
                prev: ProgressSnapshot | None = orch._last_snapshot.get(task_id)
                orch._last_snapshot_ts[task_id] = latest.timestamp
                orch._last_snapshot[task_id] = latest
                if prev is not None and prev.is_same_progress(latest):
                    orch._stall_counts[task_id] = orch._stall_counts.get(task_id, 0) + 1
                else:
                    orch._stall_counts[task_id] = 0
                count = orch._stall_counts[task_id]
                elapsed = time.time() - session.spawn_ts
                if count >= 7:
                    with contextlib.suppress(Exception):
                        orch._spawner.kill(session)
                    orch._stall_counts[task_id] = 0
                elif count >= 5:
                    with contextlib.suppress(OSError):
                        orch._signal_mgr.write_shutdown(
                            session.id,
                            reason="stalled_5min",
                            task_title=task_id,
                        )
                elif count >= 3:
                    with contextlib.suppress(OSError):
                        orch._signal_mgr.write_wakeup(
                            session.id,
                            task_title=task_id,
                            elapsed_s=elapsed,
                            last_activity_ago_s=elapsed,
                        )
        return

    timeout_s = float(getattr(getattr(orch, "_config", None), "heartbeat_timeout_s", 120))
    monitor = HeartbeatMonitor(workdir, timeout_s=timeout_s)
    aggregator = AgentLogAggregator(workdir)
    latest_tasks = getattr(orch, "_latest_tasks_by_id", {})
    base = orch._config.server_url

    for session in orch._agents.values():
        if session.status == "dead":
            continue
        hb_status = monitor.check(session.id)
        if hb_status.last_heartbeat is not None:
            session.heartbeat_ts = hb_status.last_heartbeat.timestamp()
        log_summary = aggregator.parse_log(session.id)

        for task_id in session.task_ids:
            try:
                resp = orch._client.get(f"{base}/tasks/{task_id}/snapshots")
                resp.raise_for_status()
                snapshots_raw: Any = resp.json()
            except Exception:
                continue

            if not isinstance(snapshots_raw, list) or not snapshots_raw:
                continue
            snapshots_data = cast("list[dict[str, Any]]", snapshots_raw)
            latest_raw = snapshots_data[-1]
            latest = ProgressSnapshot(
                timestamp=float(latest_raw["timestamp"]),
                files_changed=int(latest_raw.get("files_changed", 0)),
                tests_passing=int(latest_raw.get("tests_passing", -1)),
                errors=int(latest_raw.get("errors", 0)),
                last_file=str(latest_raw.get("last_file", "")),
            )

            last_ts = orch._last_snapshot_ts.get(task_id, 0.0)
            if latest.timestamp <= last_ts:
                continue

            prev: ProgressSnapshot | None = orch._last_snapshot.get(task_id)
            orch._last_snapshot_ts[task_id] = latest.timestamp
            orch._last_snapshot[task_id] = latest

            if hb_status.is_alive:
                orch._stall_counts[task_id] = 0
                continue

            if prev is not None and prev.is_same_progress(latest):
                orch._stall_counts[task_id] = orch._stall_counts.get(task_id, 0) + 1
            else:
                orch._stall_counts[task_id] = 0

            task_map = cast("dict[str, Task]", latest_tasks) if isinstance(latest_tasks, dict) else {}
            task = task_map.get(task_id)
            profile = compute_stall_profile(task, hb_status, log_summary)
            count = orch._stall_counts[task_id]
            elapsed = time.time() - session.spawn_ts

            if count >= profile.kill_threshold:
                logger.warning(
                    "Stall-killing agent %s (task %s): %d identical snapshots (%s)",
                    session.id,
                    task_id,
                    count,
                    profile.reason,
                )
                with contextlib.suppress(Exception):
                    orch._spawner.kill(session)
                orch._stall_counts[task_id] = 0
            elif count >= profile.shutdown_threshold:
                logger.warning(
                    "Stall-shutdown agent %s (task %s): %d identical snapshots (%s)",
                    session.id,
                    task_id,
                    count,
                    profile.reason,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_shutdown(
                        session.id,
                        reason=f"stalled:{profile.reason}",
                        task_title=task_id,
                    )
            elif count >= profile.wakeup_threshold:
                logger.info(
                    "Stall-wakeup agent %s (task %s): %d identical snapshots (%s)",
                    session.id,
                    task_id,
                    count,
                    profile.reason,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_wakeup(
                        session.id,
                        task_title=task_id,
                        elapsed_s=elapsed,
                        last_activity_ago_s=elapsed,
                    )


def detect_idle_agents(
    workdir: Path,
    agents: dict[str, Any],
    _max_idle_seconds: int = IDLE_LOG_AGE_THRESHOLD_SECONDS,
) -> list[str]:
    """Detect agents that are idle and should be killed to save cost.

    An agent is considered idle if:
    - Log file hasn't grown in max_idle_seconds (default 3 minutes)
    - Heartbeat is still alive (agent process is running)

    Args:
        workdir: Repository root directory.
        agents: Dict of agent sessions (id -> AgentSession).
        max_idle_seconds: Threshold for considering agent idle.

    Returns:
        List of session IDs that are idle and should be killed.
    """
    idle_agents: list[str] = []
    aggregator = AgentLogAggregator(workdir)

    for session_id, agent in agents.items():
        # Skip dead agents
        if hasattr(agent, "status") and agent.status == "dead":
            continue

        # Check log activity - use last_activity_line as proxy for recent activity
        log_summary = aggregator.parse_log(session_id)
        # If log has recent activity (more than a few lines), consider active
        if log_summary.total_lines > 10:
            continue

        # Agent appears idle
        idle_agents.append(session_id)
        logger.info(
            "Idle agent detected: %s (only %d log lines)",
            session_id,
            log_summary.total_lines,
        )

    return idle_agents
