"""Agent lifecycle: tracking, heartbeat, crash detection, reaping.

Methods extracted from the Orchestrator class that deal with agent state
management: refreshing statuses, handling orphaned tasks, reaping timed-out
agents, and emitting metrics for dead agents.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.janitor import verify_task
from bernstein.core.lifecycle import transition_agent
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    ProgressSnapshot,
    Task,
    TaskStatus,
)
from bernstein.core.task_lifecycle import (
    collect_completion_data,
    retry_or_fail_task,
)
from bernstein.core.tick_pipeline import (
    block_task,
    complete_task,
)
from bernstein.evolution.types import MetricsRecord

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent state refresh
# ---------------------------------------------------------------------------


def refresh_agent_states(orch: Any, tasks_snapshot: dict[str, list[Task]]) -> None:
    """Update alive/dead status for all tracked agents.

    When an agent process dies, handles orphaned tasks via the agent
    completion protocol: checks task status on the server, runs janitor
    verification if completion signals exist, and completes or fails
    accordingly. Also releases file ownership and emits metrics.

    Args:
        orch: Orchestrator instance.
        tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
    """
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue
        if not orch._spawner.check_alive(session):
            transition_agent(session, "dead", actor="agent_lifecycle", reason="process not alive")
            # Release file ownership for this agent
            _release_file_ownership(orch, session.id)
            _release_task_to_session(orch, session.task_ids)
            # Decrement active-agent count for this provider
            _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
            if _rl_tracker is not None and session.provider:
                _rl_tracker.decrement_active(session.provider)
            # Handle orphaned tasks
            for task_id in session.task_ids:
                # Increment crash count and preserve worktree when using resume strategy
                orch._crash_counts[task_id] = orch._crash_counts.get(task_id, 0) + 1
                _maybe_preserve_worktree(orch, session, task_id)
                handle_orphaned_task(orch, task_id, session, tasks_snapshot)

    # Purge dead agents to prevent unbounded dict growth (memory leak fix)
    purge_dead_agents(orch)

    # Purge expired spawn backoff entries
    now = time.time()
    expired = [k for k, (_, ts) in orch._spawn_failures.items() if now - ts > orch._SPAWN_BACKOFF_MAX_S]
    for k in expired:
        del orch._spawn_failures[k]

    # Cap _processed_done_tasks to prevent unbounded set growth
    if len(orch._processed_done_tasks) > orch._MAX_PROCESSED_DONE:
        # Keep only the most recent half
        excess = len(orch._processed_done_tasks) - orch._MAX_PROCESSED_DONE // 2
        for _ in range(excess):
            orch._processed_done_tasks.pop()


def purge_dead_agents(orch: Any) -> None:
    """Remove oldest dead agent sessions to bound memory usage.

    Args:
        orch: Orchestrator instance.
    """
    dead = [(sid, s) for sid, s in orch._agents.items() if s.status == "dead"]
    if len(dead) <= orch._MAX_DEAD_AGENTS_KEPT:
        return
    # Sort by heartbeat_ts (oldest first), remove excess
    dead.sort(key=lambda x: x[1].heartbeat_ts)
    to_remove = len(dead) - orch._MAX_DEAD_AGENTS_KEPT
    for sid, _ in dead[:to_remove]:
        del orch._agents[sid]
        # Clean up reverse index entries pointing to this agent
        stale_tasks = [tid for tid, aid in orch._task_to_session.items() if aid == sid]
        for tid in stale_tasks:
            del orch._task_to_session[tid]


# ---------------------------------------------------------------------------
# Crash recovery / worktree preservation
# ---------------------------------------------------------------------------


def _maybe_preserve_worktree(orch: Any, session: AgentSession, task_id: str) -> None:
    """Preserve the crashed agent's worktree for resume if policy permits.

    Stores the worktree path in ``_preserved_worktrees`` so the next spawn
    for this task can call ``spawn_for_resume`` instead of creating a fresh
    worktree.  Only applies when ``recovery == "resume"`` and the crash
    count is still within ``max_crash_retries``.

    Args:
        orch: Orchestrator instance.
        session: The crashed agent's session.
        task_id: ID of the task that was being worked on.
    """
    if orch._config.recovery != "resume":
        return
    crash_count = orch._crash_counts.get(task_id, 0)
    if crash_count > orch._config.max_crash_retries:
        return
    worktree_path = orch._spawner._worktree_paths.get(session.id)  # type: ignore[attr-defined]
    if worktree_path is None:
        return
    orch._preserved_worktrees[task_id] = worktree_path
    logger.info(
        "Crash recovery: preserving worktree %s for task %s (crash #%d)",
        worktree_path,
        task_id,
        crash_count,
    )


# ---------------------------------------------------------------------------
# Orphaned task handling
# ---------------------------------------------------------------------------


def handle_orphaned_task(
    orch: Any,
    task_id: str,
    session: AgentSession,
    tasks_snapshot: dict[str, list[Task]],
) -> None:
    """Handle a task left behind by a dead agent process.

    Checks task status using the pre-fetched snapshot (no extra HTTP call).
    Falls back to a live fetch only if the task is not found in the snapshot.
    Runs janitor verification if the task has completion signals, and marks
    it complete or failed. Emits a MetricsRecord afterward.

    Args:
        orch: Orchestrator instance.
        task_id: ID of the orphaned task.
        session: The dead agent's session.
        tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
    """
    base = orch._config.server_url
    start_ts = session.heartbeat_ts if session.heartbeat_ts > 0 else time.time()
    success = False
    error_type: str | None = None

    # Try to find the task in the pre-fetched snapshot first (avoids HTTP call)
    all_cached: list[Task] = []
    for bucket in tasks_snapshot.values():
        all_cached.extend(bucket)
    task_by_id = {t.id: t for t in all_cached}

    if task_id in task_by_id:
        task = task_by_id[task_id]
        logger.debug("handle_orphaned_task %s: resolved from tick snapshot", task_id)
    else:
        # Not in snapshot -- fall back to a live fetch
        try:
            resp = orch._client.get(f"{base}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
            logger.debug("handle_orphaned_task %s: fetched live (not in snapshot)", task_id)
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch orphaned task %s: %s", task_id, exc)
            error_type = "fetch_failed"
            emit_orphan_metrics(
                orch._workdir,
                task_id,
                session,
                start_ts,
                success=False,
                error_type=error_type,
            )
            return

    status = task.status
    if status not in (TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
        logger.info(
            "Orphaned task %s already resolved (status=%s), skipping",
            task_id,
            status.value,
        )
        return

    # Rate-limit 429 detection: scan the agent's log before deciding how to retry.
    # If a 429 pattern is found, throttle the provider so subsequent spawns avoid it.
    _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
    if _rl_tracker is not None and session.provider:
        _log_path = orch._workdir / ".sdd" / "runtime" / f"{session.id}.log"
        if _rl_tracker.scan_log_for_429(_log_path):
            _rl_tracker.throttle_provider(session.provider, getattr(orch, "_router", None))
            logger.warning(
                "Rate-limit detected in log for session %s (provider=%r, task=%s)",
                session.id,
                session.provider,
                task_id,
            )

    # Escalate strategy: block task when crash limit exceeded
    if orch._config.recovery == "escalate" and orch._crash_counts.get(task_id, 0) >= orch._config.max_crash_retries:
        reason = (
            f"Agent {session.id} died; escalating after "
            f"{orch._crash_counts[task_id]} crash(es) -- requires human intervention"
        )
        try:
            block_task(orch._client, base, task_id, reason)
            logger.warning(
                "Escalated task %s to BLOCKED after %d crash(es)",
                task_id,
                orch._crash_counts[task_id],
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to block escalated task %s: %s", task_id, exc)
        emit_orphan_metrics(orch._workdir, task_id, session, start_ts, success=False, error_type="escalated")
        return

    # Collect structured completion data from agent log
    completion_data = collect_completion_data(orch._workdir, session)

    if task.completion_signals:
        passed, failed_signals = verify_task(task, orch._workdir)
        if passed:
            try:
                result_payload: dict[str, Any] = {
                    "result_summary": f"Auto-completed after agent {session.id} died; janitor passed",
                    **completion_data,
                }
                orch._client.post(
                    f"{base}/tasks/{task_id}/complete",
                    json=result_payload,
                )
                success = True
                logger.info(
                    "Orphaned task %s auto-completed (janitor passed) after agent %s died",
                    task_id,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to complete orphaned task %s: %s", task_id, exc)
                error_type = "complete_failed"
        else:
            try:
                retry_or_fail_task(
                    task_id,
                    f"Agent {session.id} died; janitor failed: {failed_signals}",
                    client=orch._client,
                    server_url=base,
                    max_task_retries=orch._config.max_task_retries,
                    retried_task_ids=orch._retried_task_ids,
                )
                logger.info(
                    "Orphaned task %s retry/failed (janitor failed: %s) after agent %s died",
                    task_id,
                    failed_signals,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to retry/fail orphaned task %s: %s", task_id, exc)
            error_type = "janitor_failed"
    else:
        # No completion signals -- check if agent produced output (files modified)
        completion_data = collect_completion_data(orch._workdir, session)
        files_changed = len(completion_data.get("files_modified", []))
        if files_changed > 0:
            # Agent did work but task had no signals -- auto-complete
            try:
                complete_task(
                    orch._client,
                    base,
                    task_id,
                    f"Auto-completed: agent {session.id} modified {files_changed} files (no signals to verify)",
                )
                success = True
                logger.info(
                    "Orphaned task %s auto-completed (%d files modified, no signals) after agent %s died",
                    task_id,
                    files_changed,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to complete orphaned task %s: %s", task_id, exc)
                error_type = "complete_failed"
        else:
            runtime = int(time.time() - start_ts)
            try:
                retry_or_fail_task(
                    task_id,
                    f"Agent {session.id} died; no completion signals and no files modified",
                    client=orch._client,
                    server_url=base,
                    max_task_retries=orch._config.max_task_retries,
                    retried_task_ids=orch._retried_task_ids,
                )
                logger.warning(
                    "Task '%s' failed — agent died without output. "
                    "Reason: process exited (PID %s, %ds runtime). "
                    "Check log: .sdd/runtime/%s.log",
                    task.title,
                    session.pid or "unknown",
                    runtime,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to retry/fail orphaned task %s: %s", task_id, exc)
            error_type = "no_signals"

    # WAL: record the orphaned-task outcome for audit trail
    _wal = getattr(orch, "_wal_writer", None)
    if _wal is not None:
        _wal_dtype = "task_completed" if success else "task_failed"
        try:
            _wal.write_entry(
                decision_type=_wal_dtype,
                inputs={"task_id": task_id, "agent_id": session.id, "orphaned": True},
                output={"success": success, "error_type": error_type or ""},
                actor="agent_lifecycle",
            )
        except OSError:
            logger.debug("WAL write failed for orphaned %s %s", _wal_dtype, task_id)

    emit_orphan_metrics(
        orch._workdir,
        task_id,
        session,
        start_ts,
        success=success,
        error_type=error_type,
    )
    orch._record_provider_health(session, success=success)

    # Feed orphaned task outcome to the evolution coordinator so that
    # failed/timed-out agent runs are visible to trend analysis.
    if orch._evolution is not None:
        _now = time.time()
        _duration = _now - start_ts
        try:
            orch._evolution.record_task_completion(
                task=task,
                duration_seconds=round(_duration, 2),
                cost_usd=0.0,
                janitor_passed=success,
                model=session.model_config.model,
                provider=session.provider,
            )
        except Exception as exc:
            logger.warning(
                "Evolution record_task_completion for orphan %s failed: %s",
                task_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Metrics emission
# ---------------------------------------------------------------------------


def emit_orphan_metrics(
    workdir: Path,
    task_id: str,
    session: AgentSession,
    start_ts: float,
    *,
    success: bool,
    error_type: str | None,
) -> None:
    """Write a 14-field MetricsRecord to .sdd/metrics/YYYY-MM-DD.jsonl.

    Args:
        workdir: Project working directory.
        task_id: The task ID.
        session: The agent session that died.
        start_ts: Approximate start timestamp of the agent run.
        success: Whether the orphaned task was auto-completed.
        error_type: Error category, or None on success.
    """
    now = time.time()
    record = MetricsRecord(
        timestamp=datetime.now(UTC).isoformat(),
        task_id=task_id,
        agent_id=session.id,
        role=session.role,
        model_used=session.model_config.model,
        duration_seconds=round(now - start_ts, 2),
        token_count=0,
        cost_usd=0.0,
        success=success,
        error_type=error_type,
        files_modified=0,
        test_pass_rate=1.0 if success else 0.0,
        retry_count=0,
        step_count=0,
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = metrics_dir / f"{today}.jsonl"
    with metrics_path.open("a") as f:
        f.write(json.dumps(record.to_dict()) + "\n")


# ---------------------------------------------------------------------------
# Stale agent detection
# ---------------------------------------------------------------------------


def check_stale_agents(orch: Any) -> None:
    """Write WAKEUP / SHUTDOWN signals for agents that stopped heartbeating.

    Thresholds:
    - 60s without a heartbeat  -> WAKEUP
    - 120s without a heartbeat -> SHUTDOWN
    - 180s without a heartbeat -> handled by wall-clock kill in reap_dead_agents

    Only fires when an agent has at least one heartbeat on record (agents
    that never wrote a heartbeat are assumed to not support the protocol).

    Args:
        orch: Orchestrator instance.
    """
    now = time.time()
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        hb = orch._signal_mgr.read_heartbeat(session.id)
        if hb is None:
            continue  # Agent never wrote a heartbeat -- skip
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


# ---------------------------------------------------------------------------
# Stall detection via progress snapshots
# ---------------------------------------------------------------------------


def check_stalled_tasks(orch: Any) -> None:
    """Detect agents making no progress via consecutive identical snapshots.

    Fetches the latest progress snapshot for each active agent's tasks.
    Compares against the last seen snapshot tracked in the orchestrator.
    Escalates through WAKEUP → SHUTDOWN → kill based on stall count.

    Thresholds (each snapshot = ~60s):
    - 3 identical consecutive snapshots → WAKEUP signal
    - 5 identical consecutive snapshots → SHUTDOWN signal
    - 7 identical consecutive snapshots → kill process

    Only fires when a task has at least one snapshot on record.

    Args:
        orch: Orchestrator instance.
    """
    base = orch._config.server_url
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        for task_id in session.task_ids:
            try:
                resp = orch._client.get(f"{base}/tasks/{task_id}/snapshots")
                resp.raise_for_status()
                snapshots_data: list[dict[str, Any]] = resp.json()
            except Exception:
                continue  # Server unavailable or task not found — skip

            if not snapshots_data:
                continue  # No snapshots yet

            # Parse the latest snapshot
            latest_raw = snapshots_data[-1]
            latest = ProgressSnapshot(
                timestamp=float(latest_raw["timestamp"]),
                files_changed=int(latest_raw.get("files_changed", 0)),
                tests_passing=int(latest_raw.get("tests_passing", -1)),
                errors=int(latest_raw.get("errors", 0)),
                last_file=str(latest_raw.get("last_file", "")),
            )

            # Skip if we have already processed this snapshot (same timestamp)
            last_ts = orch._last_snapshot_ts.get(task_id, 0.0)
            if latest.timestamp <= last_ts:
                continue

            # Compare with previous snapshot to track stall count
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
                logger.warning(
                    "Stall-killing agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(Exception):
                    orch._spawner.kill(session)
                # Reset to prevent repeated kill calls before process exits
                orch._stall_counts[task_id] = 0
            elif count >= 5:
                logger.warning(
                    "Stall-shutdown agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_shutdown(
                        session.id,
                        reason="stalled_5min",
                        task_title=task_id,
                    )
            elif count >= 3:
                logger.info(
                    "Stall-wakeup agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_wakeup(
                        session.id,
                        task_title=task_id,
                        elapsed_s=elapsed,
                        last_activity_ago_s=elapsed,
                    )


# ---------------------------------------------------------------------------
# Reap dead / timed-out agents
# ---------------------------------------------------------------------------


def reap_dead_agents(
    orch: Any,
    result: Any,  # TickResult
    tasks_snapshot: dict[str, list[Task]],
) -> None:
    """Kill agents that exceeded heartbeat or wall-clock timeout.

    Also fails any tasks owned by reaped agents.

    Args:
        orch: Orchestrator instance.
        result: TickResult to record reaped agent IDs into.
        tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
    """
    now = time.time()
    collector = get_collector()
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        # Wall-clock timeout: use per-session timeout if set, else global config
        timeout_s = session.timeout_s if session.timeout_s is not None else orch._config.max_agent_runtime_s
        runtime = now - session.spawn_ts
        if runtime > timeout_s:
            logger.warning(
                "Reaping agent %s (exceeded timeout %.0fs, runtime %.0fs)",
                session.id,
                timeout_s,
                runtime,
            )
            orch._spawner.kill(session)
            result.reaped.append(session.id)
            _release_file_ownership(orch, session.id)
            _release_task_to_session(orch, session.task_ids)
            # Record agent end metrics (mirrors the heartbeat-timeout branch)
            collector.end_agent(session.id)
            # Record agent lifetime in evolution collector (wall-clock reap)
            if orch._evolution is not None:
                with contextlib.suppress(Exception):
                    orch._evolution.record_agent_lifetime(
                        agent_id=session.id,
                        role=session.role,
                        lifetime_seconds=round(runtime, 2),
                        tasks_completed=0,
                        model=session.model_config.model,
                    )
            with contextlib.suppress(OSError):
                orch._signal_mgr.clear_signals(session.id)
            for task_id in session.task_ids:
                handle_orphaned_task(orch, task_id, session, tasks_snapshot)
            continue

        # Heartbeat timeout
        age = now - session.heartbeat_ts
        if session.heartbeat_ts > 0 and age > orch._config.heartbeat_timeout_s:
            logger.warning(
                "Reaping stale agent %s (last heartbeat %.0fs ago)",
                session.id,
                age,
            )
            orch._spawner.kill(session)
            result.reaped.append(session.id)
            # Release file ownership
            _release_file_ownership(orch, session.id)
            _release_task_to_session(orch, session.task_ids)
            # Record agent end metrics
            collector.end_agent(session.id)
            # Record agent lifetime in evolution collector (heartbeat reap)
            if orch._evolution is not None:
                with contextlib.suppress(Exception):
                    orch._evolution.record_agent_lifetime(
                        agent_id=session.id,
                        role=session.role,
                        lifetime_seconds=round(now - session.spawn_ts, 2),
                        tasks_completed=0,
                        model=session.model_config.model,
                    )
            # Record provider health failure for reaped agent
            orch._record_provider_health(session, success=False)
            with contextlib.suppress(OSError):
                orch._signal_mgr.clear_signals(session.id)
            # Retry or fail their tasks
            for task_id in session.task_ids:
                # WAL: record heartbeat-reaped task failure
                _wal_r = getattr(orch, "_wal_writer", None)
                if _wal_r is not None:
                    try:
                        _wal_r.write_entry(
                            decision_type="task_failed",
                            inputs={"task_id": task_id, "agent_id": session.id},
                            output={"reason": "heartbeat_timeout"},
                            actor="agent_lifecycle",
                        )
                    except OSError:
                        logger.debug("WAL write failed for heartbeat-reaped task %s", task_id)
                try:
                    retry_or_fail_task(
                        task_id,
                        f"Agent {session.id} reaped (heartbeat timeout)",
                        client=orch._client,
                        server_url=orch._config.server_url,
                        max_task_retries=orch._config.max_task_retries,
                        retried_task_ids=orch._retried_task_ids,
                        tasks_snapshot=tasks_snapshot,
                    )
                except httpx.HTTPError as exc:
                    logger.error("Failed to retry/fail task %s: %s", task_id, exc)


# ---------------------------------------------------------------------------
# Idle agent detection and recycling
# ---------------------------------------------------------------------------

#: Seconds to wait after sending SHUTDOWN before force-killing an idle agent.
_IDLE_GRACE_S: float = 30.0

#: Default no-heartbeat idle threshold (seconds).
_IDLE_HEARTBEAT_THRESHOLD_S: float = 90.0

#: Aggressive idle threshold used when evolve mode is active.
_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S: float = 60.0


def recycle_idle_agents(
    orch: Any,
    tasks_snapshot: dict[str, list[Task]],
) -> None:
    """Detect and recycle agents that are idle but consuming a slot.

    An agent is considered idle when:
    - All of its tasks are already resolved (done/failed) on the server
      while the process is still alive, OR
    - The process has not written a heartbeat for ``_IDLE_HEARTBEAT_THRESHOLD_S``
      seconds (60 s in evolve mode for faster agent turnover), OR
    - The agent's role has zero active tasks (open + claimed + in_progress),
      meaning the role is fully drained and the agent should exit so its
      slot can be used by under-served roles (rebalancing).

    Recycling protocol:
    1. Send SHUTDOWN signal — agent has 30 s to save WIP and exit cleanly.
    2. If still alive after 30 s → SIGKILL.
    3. Clear signal files and release the slot.

    Args:
        orch: Orchestrator instance.
        tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
    """
    now = time.time()

    # Build resolved task ID set from snapshot (done / failed / blocked)
    resolved_ids: set[str] = set()
    for status in ("done", "failed", "blocked"):
        for t in tasks_snapshot.get(status, []):
            resolved_ids.add(t.id)

    # Count open tasks per role — used in Case 3 to detect empty role queues.
    open_per_role: dict[str, int] = {}
    for t in tasks_snapshot.get("open", []):
        open_per_role[t.role] = open_per_role.get(t.role, 0) + 1

    # Count active tasks per role (open + claimed + in_progress) — used in
    # Case 4 to detect fully drained roles for rebalancing (#333d-03).
    active_per_role: dict[str, int] = {}
    for status in ("open", "claimed", "in_progress"):
        for t in tasks_snapshot.get(status, []):
            active_per_role[t.role] = active_per_role.get(t.role, 0) + 1

    # Heartbeat-idle threshold — tighter in evolve mode for fast turnover
    hb_idle_s = _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S if orch._config.evolve_mode else _IDLE_HEARTBEAT_THRESHOLD_S

    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue
        if not orch._spawner.check_alive(session):
            continue  # Already dead — refresh_agent_states handles it next tick

        idle_reason: str | None = None

        # Case 1: all tasks already resolved on server
        if session.task_ids and all(tid in resolved_ids for tid in session.task_ids):
            idle_reason = "task_already_resolved"

        # Case 2: no heartbeat update for idle threshold
        elif orch._signal_mgr.read_heartbeat(session.id) is not None:
            hb = orch._signal_mgr.read_heartbeat(session.id)
            if hb is not None and (now - hb.timestamp) >= hb_idle_s:
                idle_reason = f"no_heartbeat_{int(hb_idle_s)}s"

        # Case 3: agent has no assigned tasks and the role queue is empty.
        # Handles edge cases where an agent slipped through without tasks, or
        # had its task list cleared, while the role has no pending work.
        elif not session.task_ids and open_per_role.get(session.role, 0) == 0:
            idle_reason = "role_queue_empty_no_tasks"

        # Case 4: role fully drained — zero active tasks (open + claimed +
        # in_progress) remain for this role.  Catches agents whose task_ids
        # are orphaned (e.g. task deleted from server) that Cases 1-3 miss,
        # and enables rebalancing: agents exit so their slots can be used by
        # under-served roles.  (#333d-03)
        elif active_per_role.get(session.role, 0) == 0:
            idle_reason = "role_drained_rebalance"

        if idle_reason is None:
            continue

        _recycle_or_kill(orch, session, now, idle_reason)


def _recycle_or_kill(orch: Any, session: AgentSession, now: float, reason: str) -> None:
    """Send SHUTDOWN or SIGKILL to an idle agent.

    On first call: writes the SHUTDOWN signal file and records the timestamp.
    On subsequent calls once grace period elapsed: force-kills the process and
    clears the tracking entry.

    Args:
        orch: Orchestrator instance.
        session: The idle agent session.
        now: Current Unix timestamp.
        reason: Human-readable reason for recycling (used in log and signal).
    """
    shutdown_sent_ts: float = orch._idle_shutdown_ts.get(session.id, 0.0)

    if shutdown_sent_ts == 0.0:
        # First detection — send SHUTDOWN and record timestamp
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with contextlib.suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=reason, task_title=task_title)
        orch._idle_shutdown_ts[session.id] = now
        logger.info(
            "Idle agent %s detected (%s) — SHUTDOWN signal sent, waiting %ds",
            session.id,
            reason,
            int(_IDLE_GRACE_S),
        )
    elif now - shutdown_sent_ts >= _IDLE_GRACE_S:
        # Grace period elapsed — force-kill
        logger.warning(
            "Recycled idle agent %s (%s — no exit after %ds SHUTDOWN grace)",
            session.id,
            reason,
            int(_IDLE_GRACE_S),
        )
        with contextlib.suppress(Exception):
            orch._spawner.kill(session)
        orch._idle_shutdown_ts.pop(session.id, None)
        with contextlib.suppress(OSError):
            orch._signal_mgr.clear_signals(session.id)
        get_collector().end_agent(session.id)


# ---------------------------------------------------------------------------
# Kill signal processing
# ---------------------------------------------------------------------------


def check_kill_signals(orch: Any, result: Any) -> None:
    """Process ``.kill`` signal files from the runtime directory.

    For each ``<session_id>.kill`` file found, terminates the matching
    agent (if alive) and removes the signal file.

    Args:
        orch: Orchestrator instance.
        result: Current tick result to record reaped agents.
    """
    runtime_dir = orch._workdir / ".sdd" / "runtime"
    if not runtime_dir.is_dir():
        return
    for kill_file in runtime_dir.glob("*.kill"):
        session_id = kill_file.stem
        # Remove the signal file first (idempotent)
        with contextlib.suppress(OSError):
            kill_file.unlink()
        session = orch._agents.get(session_id)
        if session is None or session.status == "dead":
            continue
        logger.info("Kill signal received for %s, terminating", session_id)
        orch._spawner.kill(session)
        result.reaped.append(session_id)


def send_shutdown_signals(orch: Any, reason: str) -> None:
    """Write SHUTDOWN signal files to all currently active agents.

    Called when ``bernstein stop`` is issued or the budget is hit so
    agents can save WIP before the orchestrator exits.

    Args:
        orch: Orchestrator instance.
        reason: Human-readable reason for the shutdown.
    """
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with contextlib.suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=reason, task_title=task_title)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _release_file_ownership(orch: Any, agent_id: str) -> None:
    """Release all files owned by the given agent.

    Uses :class:`~bernstein.core.file_locks.FileLockManager` when available,
    falling back to the legacy ``_file_ownership`` dict for compatibility.

    Args:
        orch: Orchestrator instance.
        agent_id: The agent whose files to release.
    """
    lock_manager = getattr(orch, "_lock_manager", None)
    if lock_manager is not None:
        lock_manager.release(agent_id)
    # Always clean the legacy dict so tests and code that write to it directly stay consistent
    to_remove = [fp for fp, owner in orch._file_ownership.items() if owner == agent_id]
    for fp in to_remove:
        del orch._file_ownership[fp]


def _release_task_to_session(orch: Any, task_ids: list[str]) -> None:
    """Remove reverse-index entries for the given task IDs.

    Args:
        orch: Orchestrator instance.
        task_ids: The task IDs whose mappings to remove.
    """
    for tid in task_ids:
        orch._task_to_session.pop(tid, None)
