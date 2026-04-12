"""Agent death handling and orphaned task recovery.

Extracted from ``agent_lifecycle`` — reaping dead/timed-out agents,
handling orphaned tasks, emitting metrics, and crash recovery.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.janitor import verify_task
from bernstein.core.metrics import get_collector
from bernstein.core.models import AgentSession, Task, TaskStatus
from bernstein.core.task_lifecycle import (
    collect_completion_data,
    retry_or_fail_task,
)
from bernstein.core.tick_pipeline import (
    block_task,
    complete_task,
)
from bernstein.evolution.types import MetricsRecord

_ORPHAN_COMPLETE_ERROR = "Failed to complete orphaned task %s: %s"

if TYPE_CHECKING:
    from bernstein.core.abort_chain import AbortChain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Partial work preservation
# ---------------------------------------------------------------------------


def _save_partial_work(spawner: Any, session: Any) -> bool:
    """Commit and merge uncommitted agent work before worktree destruction.

    Called before ``cleanup_worktree()`` to prevent data loss on timeout
    kills and agent crashes.  Stages all changes, creates a ``[WIP]``
    commit, then attempts to merge the branch back to main via
    ``reap_completed_agent()``.

    All errors are suppressed so the cleanup path is never interrupted.

    Returns:
        True if a WIP commit was created, False otherwise.
    """
    worktree_path = spawner.get_worktree_path(session.id)
    if worktree_path is None or not Path(worktree_path).is_dir():
        return False

    wt = str(worktree_path)
    committed = False
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=wt,
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            ["git", "commit", "-m", f"[WIP] {session.id} partial work"],
            cwd=wt,
            capture_output=True,
            timeout=10,
        )
        committed = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, Exception):
        pass

    # Try to merge the branch before cleanup
    with contextlib.suppress(Exception):
        spawner.reap_completed_agent(session, skip_merge=False)

    if committed:
        logger.info("Saved partial work for agent %s", session.id)
    return committed


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
# Abort chain helpers
# ---------------------------------------------------------------------------


def _propagate_abort_to_children(orch: Any, session_id: str) -> None:
    """Cascade SESSION-scope abort signals to all children of the given session.

    Looks for ``_abort_chain`` on the orchestrator.  When present,
    calls :meth:`~abort_chain.AbortChain.propagate_abort` (SESSION scope)
    followed by :meth:`~abort_chain.AbortChain.cleanup` for the session.

    This is the most destructive level of the abort hierarchy.  For
    finer-grained containment use :func:`_abort_siblings` (SIBLING scope) or
    leave tool-level aborts to the worker process (TOOL scope).

    Args:
        orch: Orchestrator instance.
        session_id: Session ID whose children should receive abort signals.
    """
    chain: AbortChain | None = getattr(orch, "_abort_chain", None)
    if chain is None:
        return
    try:
        chain.propagate_abort(session_id)
    finally:
        chain.cleanup(session_id)


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
    from bernstein.core.agent_state_refresh import _try_compact_and_retry

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
            # 404 = task from a previous session — not a real error, just stale
            if "404" in str(exc):
                logger.info("Orphaned task %s from previous session (404), skipping", task_id)
            else:
                logger.error("Failed to fetch orphaned task %s: %s", task_id, exc)
            emit_orphan_metrics(
                orch._workdir,
                task_id,
                session,
                start_ts,
                success=False,
                error_type="stale_session" if "404" in str(exc) else "fetch_failed",
            )
            return

    status = task.status
    if status not in (TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
        logger.info(
            "Orphaned task %s already resolved (status=%s), skipping",
            task_id,
            status.value,
        )
        # Record as SUCCESS — agent completed work before dying.
        # Previously this was not recorded at all, causing the SLO tracker
        # to count it as a failure (the death event was recorded elsewhere
        # without checking task status), creating a death spiral.
        emit_orphan_metrics(
            orch._workdir,
            task_id,
            session,
            start_ts,
            success=True,
            error_type="already_resolved",
        )
        return

    # Failure detection: scan the agent's log for rate-limit, timeout, or API error
    # patterns before deciding how to retry.  If a failure is detected, throttle
    # the provider and attempt cascade fallback to another installed agent.
    _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
    if _rl_tracker is not None and session.provider:
        # Use session's log_path if available, else check standard locations
        _session_lp = getattr(session, "log_path", "")
        if _session_lp and Path(_session_lp).exists():
            _log_path = Path(_session_lp)
        else:
            _log_path = orch._workdir / ".sdd" / "runtime" / f"{session.id}.log"
            if not _log_path.exists():
                _wt_log = orch._workdir / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"
                if _wt_log.exists():
                    _log_path = _wt_log

        # Detect failure type: rate_limit, timeout, api_error, or None
        _failure_type = _rl_tracker.detect_failure_type(_log_path)
        if _failure_type is not None:
            _rl_tracker.throttle_provider(session.provider, getattr(orch, "_router", None))
            logger.warning(
                "Failure detected (%s) in log for session %s (provider=%r, task=%s)",
                _failure_type,
                session.id,
                session.provider,
                task_id,
            )

            # Cascade fallback: find an alternative agent for this task.
            from bernstein.core.cascade import CascadeDecision, CascadeFallbackManager

            _cascade = getattr(orch, "_cascade_manager", None)
            if _cascade is None:
                _cascade = CascadeFallbackManager(rate_limit_tracker=_rl_tracker)
                orch._cascade_manager = _cascade  # type: ignore[attr-defined]

            # Collect all currently throttled providers
            _throttled = frozenset(p for p in _rl_tracker.throttle_summary() if _rl_tracker.is_throttled(p))

            # Determine current cascade entry from the session's model/provider
            _current_entry = getattr(task, "model", None) or session.provider or None
            _decision = _cascade.find_fallback(
                task.complexity,
                _throttled,
                current_entry=_current_entry,
                trigger=_failure_type,
            )

            _fallback_model: str | None = None
            if isinstance(_decision, CascadeDecision):
                logger.info(
                    "Cascade fallback: task %s reassigned from %s → %s (%s)",
                    task_id,
                    session.provider,
                    _decision.fallback_provider,
                    _decision.reason,
                )
                _fallback_model = _decision.fallback_model

                # Persist cascade metrics
                _metrics_dir = orch._workdir / ".sdd" / "metrics"
                _cascade.save_metrics(_metrics_dir)
            else:
                logger.warning(
                    "Cascade exhausted for task %s: %s — task will wait for throttle recovery",
                    task_id,
                    _decision.reason,
                )

            if _failure_type == "rate_limit":
                success = False
                if _requeue_rate_limited_task(
                    client=orch._client,
                    server_url=base,
                    task=task,
                    fallback_model=_fallback_model,
                ):
                    if _fallback_model:
                        task.model = _fallback_model
                    error_type = "rate_limit_requeued"
                    logger.info(
                        "Requeued rate-limited orphaned task %s via force-claim (provider=%s, model=%s)",
                        task_id,
                        session.provider,
                        task.model or "",
                    )
                    _wal = getattr(orch, "_wal_writer", None)
                    if _wal is not None:
                        try:
                            _wal.write_entry(
                                decision_type="task_requeued",
                                inputs={
                                    "task_id": task_id,
                                    "agent_id": session.id,
                                    "orphaned": True,
                                    "trigger": _failure_type,
                                },
                                output={"model": task.model or "", "provider": session.provider or ""},
                                actor="agent_lifecycle",
                            )
                        except OSError:
                            logger.debug("WAL write failed for orphaned task_requeued %s", task_id)
                else:
                    error_type = "rate_limit_requeue_failed"

                emit_orphan_metrics(
                    orch._workdir,
                    task_id,
                    session,
                    start_ts,
                    success=False,
                    error_type=error_type,
                )
                orch._record_provider_health(session, success=False)
                if orch._evolution is not None:
                    _now = time.time()
                    _duration = _now - start_ts
                    try:
                        orch._evolution.record_task_completion(
                            task=task,
                            duration_seconds=round(_duration, 2),
                            cost_usd=0.0,
                            janitor_passed=False,
                            model=session.model_config.model,
                            provider=session.provider,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Evolution record_task_completion for orphan %s failed: %s",
                            task_id,
                            exc,
                        )
                return

            if _failure_type == "context_overflow":
                # Reactive 413 handler: compact context and retry once.
                _compacted = _try_compact_and_retry(
                    orch=orch,
                    task=task,
                    task_id=task_id,
                    session=session,
                    tasks_snapshot=tasks_snapshot,
                    fallback_model=_fallback_model,
                )
                error_type = "context_overflow_compacted" if _compacted else "context_overflow_compact_failed"
                emit_orphan_metrics(
                    orch._workdir,
                    task_id,
                    session,
                    start_ts,
                    success=False,
                    error_type=error_type,
                )
                orch._record_provider_health(session, success=False)
                return

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
                logger.error(_ORPHAN_COMPLETE_ERROR, task_id, exc)
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
        # No completion signals -- check if agent produced output.
        # We check three indicators of success (in order):
        #   1. Files modified in the working tree (original check)
        #   2. Git commits on the agent's worktree branch
        #   3. Agent exited with code 0 (clean exit = success)
        completion_data = collect_completion_data(orch._workdir, session)
        files_changed = len(completion_data.get("files_modified", []))

        # Check for git commits on the agent's worktree branch.
        # The worktree still exists at this point (cleanup happens after
        # handle_orphaned_task returns to refresh_agent_states).
        has_commits = False
        worktree_path = orch._spawner.get_worktree_path(session.id)
        if worktree_path is not None:
            has_commits = _has_git_commits_on_branch(worktree_path)

        # Agent exited with code 0 = clean exit, treat as success
        clean_exit = session.exit_code == 0

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
                logger.error(_ORPHAN_COMPLETE_ERROR, task_id, exc)
                error_type = "complete_failed"
        elif has_commits:
            # Agent made git commits on its branch — work was done even
            # though no uncommitted file modifications remain.
            try:
                complete_task(
                    orch._client,
                    base,
                    task_id,
                    f"Auto-completed: agent {session.id} made git commits on branch (no signals to verify)",
                )
                success = True
                logger.info(
                    "Orphaned task %s auto-completed (git commits detected, no signals) after agent %s died",
                    task_id,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error(_ORPHAN_COMPLETE_ERROR, task_id, exc)
                error_type = "complete_failed"
        elif clean_exit:
            # Agent exited with code 0 but produced no diff — treat as
            # "no changes needed" rather than failure.  This covers tasks
            # like documentation review, validation, or investigation where
            # the correct outcome is confirming no changes are required.
            try:
                complete_task(
                    orch._client,
                    base,
                    task_id,
                    f"Auto-completed (no changes needed): agent {session.id} "
                    f"exited cleanly with empty diff (exit code 0, no signals to verify)",
                )
                success = True
                logger.info(
                    "Orphaned task %s auto-completed (no changes needed, clean exit) after agent %s died",
                    task_id,
                    session.id,
                )
            except httpx.HTTPError as exc:
                logger.error(_ORPHAN_COMPLETE_ERROR, task_id, exc)
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
    from bernstein.core.agent_recycling import _IDLE_HEARTBEAT_THRESHOLD_S, _is_process_alive

    now = time.time()
    collector = get_collector()
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        # Wall-clock timeout: use per-session timeout if set, else global config.
        # Heartbeat-aware: if the agent heartbeated within the last 120s, extend
        # the deadline by 10 minutes (up to a hard cap of 90 minutes).  This
        # prevents killing agents that are actively making progress.
        timeout_s = session.timeout_s if session.timeout_s is not None else orch._config.max_agent_runtime_s
        runtime = now - session.spawn_ts
        _time_since_heartbeat = now - session.heartbeat_ts if session.heartbeat_ts > 0 else runtime
        _hard_cap_s = 5400  # 90 minutes absolute maximum
        if runtime > timeout_s and _time_since_heartbeat < 120 and timeout_s < _hard_cap_s:
            # Agent is still actively working — extend timeout by 10 minutes
            session.timeout_s = min(timeout_s + 600, _hard_cap_s)
            logger.info(
                "Agent %s exceeded %.0fs timeout but heartbeated %.0fs ago — extending to %.0fs",
                session.id,
                timeout_s,
                _time_since_heartbeat,
                session.timeout_s,
            )
            continue
        if runtime > timeout_s:
            logger.warning(
                "Reaping agent %s (exceeded timeout %.0fs, runtime %.0fs, last heartbeat %.0fs ago)",
                session.id,
                timeout_s,
                runtime,
                _time_since_heartbeat,
            )
            orch._spawner.kill(session)
            _propagate_abort_to_children(orch, session.id)
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
            # Save uncommitted work before destroying the worktree
            _save_partial_work(orch._spawner, session)
            # Clean up worktree unless preserved for crash-resume
            _preserved = getattr(orch, "_preserved_worktrees", {})
            _session_preserved = any(
                orch._spawner.get_worktree_path(session.id) == _preserved.get(tid) for tid in session.task_ids
            )
            if not _session_preserved:
                orch._spawner.cleanup_worktree(session.id)
            continue

        # Heartbeat proxy: refresh heartbeat_ts using multiple signals.
        # The PID from Popen may refer to a wrapper process that exits while
        # the actual CLI agent (claude/qwen) continues as a child.  We check
        # four signals — if ANY signal indicates activity, the agent is alive:
        #   1. Process liveness (PID alive → definitely working)
        #   2. Heartbeat file mtime
        #   3. Log file mtime
        #   4. Worktree directory mtime (agent writing/reading files)
        _hb_freshness_s = _IDLE_HEARTBEAT_THRESHOLD_S * 0.8
        _heartbeat_refreshed = False

        if _is_process_alive(session.pid):
            session.heartbeat_ts = now
            _heartbeat_refreshed = True

        if not _heartbeat_refreshed:
            _hb_path = orch._workdir / ".sdd" / "runtime" / "heartbeats" / f"{session.id}.json"
            try:
                if _hb_path.exists() and (now - _hb_path.stat().st_mtime) < _hb_freshness_s:
                    session.heartbeat_ts = now
                    _heartbeat_refreshed = True
            except OSError:
                pass

        if not _heartbeat_refreshed:
            _log_path = orch._workdir / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"
            try:
                if _log_path.exists() and (now - _log_path.stat().st_mtime) < _hb_freshness_s:
                    session.heartbeat_ts = now
                    _heartbeat_refreshed = True
            except OSError:
                pass

        # Fallback: check worktree .git dir mtime — git operations (checkout,
        # commit) update this, proving the agent is actively working.
        if not _heartbeat_refreshed:
            _wt_git = orch._workdir / ".sdd" / "worktrees" / session.id / ".git"
            try:
                if _wt_git.exists() and (now - _wt_git.stat().st_mtime) < _hb_freshness_s:
                    session.heartbeat_ts = now
                    _heartbeat_refreshed = True
            except OSError:
                pass

        # Heartbeat timeout
        age = now - session.heartbeat_ts
        if session.heartbeat_ts > 0 and age > orch._config.heartbeat_timeout_s:
            logger.warning(
                "Reaping stale agent %s (last heartbeat %.0fs ago)",
                session.id,
                age,
            )
            orch._spawner.kill(session)
            _propagate_abort_to_children(orch, session.id)
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


def _has_git_commits_on_branch(worktree_path: Path) -> bool:
    """Return True if the worktree branch has commits beyond main."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return len(result.stdout.strip()) > 0
    except Exception:
        return False


def _requeue_rate_limited_task(
    *,
    client: httpx.Client,
    server_url: str,
    task: Task,
    fallback_model: str | None,
) -> bool:
    """Persist a fallback model if needed, then force-claim the task.

    Args:
        client: HTTP client for task-server calls.
        server_url: Base server URL.
        task: Task to requeue.
        fallback_model: Optional model override selected by cascade logic.

    Returns:
        ``True`` when the task was successfully force-claimed, otherwise
        ``False``.
    """
    if fallback_model and fallback_model != task.model:
        try:
            client.patch(
                f"{server_url}/tasks/{task.id}",
                json={"model": fallback_model},
            ).raise_for_status()
            task.model = fallback_model
        except httpx.HTTPError as exc:
            logger.warning(
                "Failed to persist fallback model %s for task %s before requeue: %s",
                fallback_model,
                task.id,
                exc,
            )

    try:
        client.post(f"{server_url}/tasks/{task.id}/force-claim").raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Failed to force-claim rate-limited task %s: %s", task.id, exc)
        return False
    return True


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
