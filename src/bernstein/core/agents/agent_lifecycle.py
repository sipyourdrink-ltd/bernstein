"""Agent lifecycle: tracking, heartbeat, crash detection, reaping.

Methods extracted from the Orchestrator class that deal with agent state
management: refreshing statuses, handling orphaned tasks, reaping timed-out
agents, and emitting metrics for dead agents.

Includes ``_save_partial_work()`` which commits and merges uncommitted agent
work before worktree destruction — preventing data loss on timeout kills.
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core import heartbeat as heartbeat_protocol
from bernstein.core.janitor import verify_task
from bernstein.core.lifecycle import transition_agent
from bernstein.core.metrics import get_collector
from bernstein.core.models import AbortReason, AgentSession, Task, TaskStatus, TransitionReason
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
    from bernstein.core.abort_chain import AbortChain, AbortPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abort chain helpers — three-level hierarchy
# ---------------------------------------------------------------------------
# The abort chain enforces a strict containment hierarchy:
#
#   TOOL  < SIBLING  < SESSION
#
# * TOOL   — a single tool invocation is aborted; the agent session continues.
#            Written as a TOOL_ABORT signal file in the session's signals dir.
# * SIBLING — sibling agents (same parent) receive SHUTDOWN; the parent and
#             this session are unaffected unless policy escalates further.
# * SESSION — the full agent session is torn down and SHUTDOWN cascades to
#             all descendants via ``propagate_abort``.
#
# Escalation between levels is opt-in via ``AbortPolicy``.  By default each
# level contains its failure and does not propagate upward.
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


def _abort_siblings(
    orch: Any,
    session_id: str,
    *,
    reason: str = "sibling_failure",
    policy: AbortPolicy | None = None,
) -> list[str]:
    """Send SHUTDOWN to sibling agents of *session_id* (SIBLING scope).

    Looks for ``_abort_chain`` on the orchestrator.  When present, calls
    :meth:`~abort_chain.AbortChain.abort_siblings`.  The parent session is
    *not* stopped unless *policy.sibling_to_session* is ``True``.

    Args:
        orch: Orchestrator instance.
        session_id: The session whose siblings should receive SHUTDOWN.
        reason: Human-readable reason for the sibling abort.
        policy: Optional escalation policy.  When ``None`` the sibling abort
            is contained (no cascade to the parent session).

    Returns:
        List of session IDs that received a SHUTDOWN signal.  Empty list when
        the chain is not configured or the session has no siblings.
    """
    chain: AbortChain | None = getattr(orch, "_abort_chain", None)
    if chain is None:
        return []
    return chain.abort_siblings(
        session_id,
        triggering_session_id=session_id,
        reason=reason,
        policy=policy,
    )


def classify_agent_abort_reason(session: AgentSession) -> tuple[AbortReason, str]:
    """Classify an abnormal agent stop into a canonical abort reason.

    Args:
        session: Agent session with the latest exit metadata populated.

    Returns:
        Tuple of canonical abort reason and a short detail string.
    """
    exit_code = session.exit_code
    if exit_code is None:
        return AbortReason.UNKNOWN, "agent stopped without exit code"
    if exit_code == 124:
        return AbortReason.TIMEOUT, "process exited with timeout status 124"
    if exit_code == 137:
        return AbortReason.OOM, "process exited with status 137"
    if exit_code == 126:
        return AbortReason.PERMISSION_DENIED, "process exited with permission denied status 126"
    if exit_code > 0:
        return AbortReason.UNKNOWN, f"process exited with status {exit_code}"

    signal_num = abs(exit_code)
    if signal_num == getattr(signal, "SIGINT", 2):
        return AbortReason.USER_INTERRUPT, "process interrupted by SIGINT"
    if signal_num == getattr(signal, "SIGTERM", 15):
        return AbortReason.SHUTDOWN_SIGNAL, "process terminated by SIGTERM"
    if signal_num == getattr(signal, "SIGKILL", 9):
        return AbortReason.OOM, "process killed by SIGKILL"
    return AbortReason.UNKNOWN, f"process terminated by signal {signal_num}"


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
    except Exception:
        pass

    # Try to merge the branch before cleanup
    with contextlib.suppress(Exception):
        spawner.reap_completed_agent(session, skip_merge=False)

    if committed:
        logger.info("Saved partial work for agent %s", session.id)
    return committed


# ---------------------------------------------------------------------------
# Agent state refresh
# ---------------------------------------------------------------------------


def _handle_dead_agent(orch: Any, session: AgentSession, tasks_snapshot: dict[str, list[Task]]) -> None:
    """Process a single agent that has been detected as dead."""
    abort_reason, abort_detail = classify_agent_abort_reason(session)
    transition_reason = TransitionReason.ABORTED
    if session.finish_reason == "max_output_tokens":
        transition_reason = TransitionReason.MAX_OUTPUT_TOKENS

    transition_agent(
        session,
        "dead",
        actor="agent_lifecycle",
        reason="process not alive",
        transition_reason=transition_reason,
        abort_reason=abort_reason,
        abort_detail=abort_detail,
        finish_reason=session.finish_reason or "agent_exit",
    )
    _propagate_abort_to_children(orch, session.id)
    if session.role:
        adapter_name = getattr(session, "adapter", "unknown")
        orch._agent_failure_timestamps[adapter_name] = time.time()

    _release_file_ownership(orch, session.id)
    _release_task_to_session(orch, session.task_ids)
    _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
    if _rl_tracker is not None and session.provider:
        _rl_tracker.decrement_active(session.provider)
    for task_id in session.task_ids:
        orch._crash_counts[task_id] = orch._crash_counts.get(task_id, 0) + 1
        _maybe_preserve_worktree(orch, session, task_id)
        handle_orphaned_task(orch, task_id, session, tasks_snapshot)
    _save_partial_work(orch._spawner, session)
    _preserved = getattr(orch, "_preserved_worktrees", {})
    _session_preserved = any(
        orch._spawner.get_worktree_path(session.id) == _preserved.get(tid) for tid in session.task_ids
    )
    if not _session_preserved:
        orch._spawner.cleanup_worktree(session.id)
    with contextlib.suppress(OSError):
        orch._signal_mgr.clear_signals(session.id)


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
        if orch._spawner.check_alive(session):
            continue
        _handle_dead_agent(orch, session, tasks_snapshot)

    # Purge dead agents to prevent unbounded dict growth (memory leak fix)
    purge_dead_agents(orch)

    # Purge expired spawn backoff entries
    now = time.time()

    # Memory monitoring: check for leaks in active processes
    if hasattr(orch, "_memory_guard"):
        active_sessions = [s for s in orch._agents.values() if s.status != "dead"]
        leaking_ids = orch._memory_guard.monitor_agents(active_sessions)
        if leaking_ids:
            logger.warning("Memory leak detected in sessions: %s", leaking_ids)
            # Optional: kill leaking agents if configured
            if getattr(orch._config, "kill_on_memory_leak", False):
                for sid in leaking_ids:
                    session = orch._agents.get(sid)
                    if session:
                        orch._spawner.kill(session)
                        _propagate_abort_to_children(orch, sid)
                        transition_agent(session, "dead", actor="memory_guard", reason="memory leak")

    expired = [k for k, (_, ts) in orch._spawn_failures.items() if now - ts > orch._SPAWN_BACKOFF_MAX_S]
    for k in expired:
        del orch._spawn_failures[k]

    # Cap _processed_done_tasks to prevent unbounded growth (FIFO eviction)
    if len(orch._processed_done_tasks) > orch._MAX_PROCESSED_DONE:
        excess = len(orch._processed_done_tasks) - orch._MAX_PROCESSED_DONE // 2
        # popitem(last=False) removes the oldest entry first
        for _ in range(excess):
            orch._processed_done_tasks.popitem(last=False)


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


# ---------------------------------------------------------------------------
# Reactive 413 compaction handler
# ---------------------------------------------------------------------------

#: Meta-message injected into tasks retried after context-overflow compaction.
_COMPACT_RETRY_META = (
    "CONTEXT COMPACTION: Previous attempt hit a context-window limit (HTTP 413). "
    "The prompt has been compacted.  Focus on the task goal — do NOT try to "
    "reconstruct the removed context."
)

#: Maximum number of times a task may be retried via context compaction.
#: After this many compaction-retries the task is failed permanently.
_COMPACT_MAX_RETRIES: int = 1


def _try_compact_and_retry(
    *,
    orch: Any,
    task: Task,
    task_id: str,
    session: AgentSession,
    tasks_snapshot: dict[str, list[Task]],
    fallback_model: str | None,
) -> bool:
    """Run the compaction pipeline on the task's prompt and retry once.

    When an agent crashes with a 413 / context-overflow error, this function:

    1. Reads the agent's log to reconstruct what the prompt looked like.
    2. Runs :class:`~bernstein.core.compaction_pipeline.CompactionPipeline`
       on the task description (the only mutable part of the prompt).
    3. Creates a retry task with a ``meta_message`` instructing the agent
       to work with reduced context.
    4. Returns ``True`` if the retry was queued, ``False`` if compaction
       failed or the retry limit was reached.

    Bounded to ``_COMPACT_MAX_RETRIES`` retries to prevent infinite loops.

    Args:
        orch: Orchestrator instance.
        task: The failed task.
        task_id: Task ID.
        session: Dead agent session.
        tasks_snapshot: Pre-fetched tasks for dedup checks.
        fallback_model: Optional cascade fallback model.

    Returns:
        True if a compacted retry was successfully queued.
    """
    from bernstein.core.compaction_pipeline import CompactionPipeline

    # Guard: check if we've already compacted this task too many times.
    # We detect previous compaction retries via the meta_messages list.
    prior_compact_retries = sum(1 for m in task.meta_messages if "CONTEXT COMPACTION" in m)
    if prior_compact_retries >= _COMPACT_MAX_RETRIES:
        logger.warning(
            "Task %s already had %d compaction retries — failing permanently",
            task_id,
            prior_compact_retries,
        )
        retry_or_fail_task(
            task_id,
            f"Context overflow: compaction retries exhausted ({prior_compact_retries}/{_COMPACT_MAX_RETRIES})",
            client=orch._client,
            server_url=orch._config.server_url,
            max_task_retries=0,  # force permanent fail
            retried_task_ids=orch._retried_task_ids,
            tasks_snapshot=tasks_snapshot,
        )
        return False

    # Run the compaction pipeline on the task description.
    pipeline = CompactionPipeline(plugin_manager=getattr(orch, "_plugin_manager", None))
    description_text = task.description
    tokens_before = max(1, len(description_text) // 4)

    # Persist pre-compaction usage in the budget manager (if available) so that
    # the effective remaining budget shown to the retry agent is accurate.
    _budget_mgr: Any = getattr(orch, "_budget_manager", None)
    effective_remaining: int | None = None
    if _budget_mgr is not None:
        try:
            _task_budget = _budget_mgr.get_budget(task_id, complexity=task.scope.value)
            _task_budget.record_pre_compaction(tokens_before)
            effective_remaining = _task_budget.effective_remaining()
        except Exception as _be:
            logger.debug("Budget pre-compaction snapshot failed for %s: %s", task_id, _be)

    try:
        result = pipeline.execute(
            session_id=session.id,
            context_text=description_text,
            tokens_before=tokens_before,
            reason="provider_413",
        )
    except Exception as exc:
        logger.error("Compaction pipeline failed for task %s: %s", task_id, exc)
        retry_or_fail_task(
            task_id,
            f"Context overflow compaction failed: {exc}",
            client=orch._client,
            server_url=orch._config.server_url,
            max_task_retries=orch._config.max_task_retries,
            retried_task_ids=orch._retried_task_ids,
            tasks_snapshot=tasks_snapshot,
        )
        return False

    # Reconcile post-compaction budget now that we know how many tokens were saved.
    if _budget_mgr is not None:
        try:
            _task_budget = _budget_mgr.get_budget(task_id, complexity=task.scope.value)
            _task_budget.reconcile_post_compaction()
            effective_remaining = _task_budget.effective_remaining()
        except Exception as _be:
            logger.debug("Budget post-compaction reconcile failed for %s: %s", task_id, _be)

    logger.info(
        "Compacted task %s description: %d → %d tokens (saved %d, correlation=%s)",
        task_id,
        result.tokens_before,
        result.tokens_after,
        result.tokens_saved,
        result.correlation_id,
    )

    # Retry the task with compacted description and a nudge meta-message.
    retry_or_fail_task(
        task_id,
        f"Context overflow (413): compacted and retrying ({result.correlation_id})",
        client=orch._client,
        server_url=orch._config.server_url,
        max_task_retries=orch._config.max_task_retries,
        retried_task_ids=orch._retried_task_ids,
        tasks_snapshot=tasks_snapshot,
    )

    # Patch the newly created retry task with compacted description and meta-message.
    # The retry task is the latest open task with the same title prefix.
    # We inject the compaction meta-message via the task server PATCH endpoint.
    _patch_retry_with_compaction(
        client=orch._client,
        server_url=orch._config.server_url,
        original_task=task,
        compacted_description=result.compacted_text,
        fallback_model=fallback_model,
        effective_remaining=effective_remaining,
    )

    # WAL entry for audit trail
    _wal: Any = getattr(orch, "_wal_writer", None)
    if _wal is not None:
        try:
            _wal.write_entry(
                decision_type="context_overflow_compacted",
                inputs={
                    "task_id": task_id,
                    "agent_id": session.id,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                },
                output={
                    "correlation_id": result.correlation_id,
                    "tokens_saved": result.tokens_saved,
                    "compacted": True,
                },
                actor="agent_lifecycle",
            )
        except OSError:
            logger.debug("WAL write failed for context_overflow_compacted %s", task_id)

    return True


def _patch_retry_with_compaction(
    *,
    client: httpx.Client,
    server_url: str,
    original_task: Task,
    compacted_description: str,
    fallback_model: str | None,
    effective_remaining: int | None = None,
) -> None:
    """Patch the retry task created by ``retry_or_fail_task`` with compacted context.

    Finds the most recent open task whose title starts with ``[RETRY`` and
    matches the original task's title, then patches its description and
    meta_messages to include the compacted context and the compaction nudge.

    When *effective_remaining* is provided it is injected as an additional
    operational nudge so the retry agent knows the true remaining token budget
    (``budget_tokens - pre_compact_used``), preventing it from treating the
    full budget as available when significant context was already consumed.

    Args:
        client: httpx client for task-server calls.
        server_url: Task server base URL.
        original_task: The original (failed) task.
        compacted_description: The compacted description text.
        fallback_model: Optional model to set on the retry task.
        effective_remaining: Effective remaining token budget after accounting
            for pre-compaction spend.  ``None`` means unknown / skip injection.
    """
    try:
        resp = client.get(f"{server_url}/tasks", params={"status": "open"})
        resp.raise_for_status()
        open_tasks = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("Failed to list open tasks for compaction patch: %s", exc)
        return

    # audit-017: look the retry task up by metadata.original_task_id and an
    # incremented retry_count.  Falls back to a title-prefix match for
    # legacy tasks whose retry clones still carry the old ``[RETRY N]``
    # prefix (no new ones are created by the orchestrator).
    retry_task_id: str | None = None
    lineage_id = original_task.metadata.get("original_task_id", original_task.id)
    for t in open_tasks:
        meta = t.get("metadata") or {}
        same_lineage = meta.get("original_task_id") == lineage_id
        bumped = int(t.get("retry_count") or 0) > original_task.retry_count
        if same_lineage and bumped:
            retry_task_id = t.get("id")
    if retry_task_id is None:
        base_title = (
            original_task.title.removeprefix("[RETRY 1] ").removeprefix("[RETRY 2] ").removeprefix("[RETRY 3] ")
        )
        for t in open_tasks:
            title = t.get("title", "")
            if title == base_title or (title.startswith("[RETRY") and base_title in title):
                retry_task_id = t.get("id")

    if retry_task_id is None:
        logger.debug("No retry task found to patch with compaction for %s", original_task.id)
        return

    # Build patch payload — include compaction nudge and optional budget hint.
    new_meta = [*original_task.meta_messages, _COMPACT_RETRY_META]
    if effective_remaining is not None:
        if effective_remaining >= 1_000_000:
            budget_hint = f"~{effective_remaining // 1_000_000}M"
        elif effective_remaining >= 1_000:
            budget_hint = f"~{effective_remaining // 1_000}K"
        else:
            budget_hint = str(effective_remaining)
        new_meta.append(
            f"BUDGET EFFECTIVE REMAINING: {budget_hint} tokens remaining after "
            "accounting for context consumed before compaction.  Plan work to fit."
        )
    patch_body: dict[str, Any] = {
        "description": compacted_description,
        "meta_messages": new_meta,
    }
    if fallback_model:
        patch_body["model"] = fallback_model

    try:
        client.patch(f"{server_url}/tasks/{retry_task_id}", json=patch_body).raise_for_status()
        logger.info(
            "Patched retry task %s with compacted description (%d chars) and %s meta-message",
            retry_task_id,
            len(compacted_description),
            "compaction",
        )
    except httpx.HTTPError as exc:
        logger.warning("Failed to patch retry task %s with compaction: %s", retry_task_id, exc)


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


def _resolve_agent_log_path(workdir: Path, session: AgentSession) -> Path:
    """Find the agent's log file, checking session attribute then standard locations."""
    _session_lp = getattr(session, "log_path", "")
    if _session_lp and Path(_session_lp).exists():
        return Path(_session_lp)
    log_path = workdir / ".sdd" / "runtime" / f"{session.id}.log"
    if not log_path.exists():
        _wt_log = workdir / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"
        if _wt_log.exists():
            return _wt_log
    return log_path


def _handle_failure_detection(
    orch: Any,
    task: Task,
    task_id: str,
    session: AgentSession,
    base: str,
    start_ts: float,
    tasks_snapshot: dict[str, list[Task]],
) -> bool:
    """Detect rate-limit/context-overflow failures and handle them. Returns True if handled."""
    _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
    if _rl_tracker is None or not session.provider:
        return False

    _log_path = _resolve_agent_log_path(orch._workdir, session)
    _failure_type = _rl_tracker.detect_failure_type(_log_path)
    if _failure_type is None:
        return False

    _rl_tracker.throttle_provider(session.provider, getattr(orch, "_router", None))
    logger.warning(
        "Failure detected (%s) in log for session %s (provider=%r, task=%s)",
        _failure_type,
        session.id,
        session.provider,
        task_id,
    )

    _fallback_model = _run_cascade_fallback(orch, task, task_id, session, _rl_tracker, _failure_type)

    if _failure_type == "rate_limit":
        _handle_rate_limit_orphan(orch, task, task_id, session, base, start_ts, _fallback_model)
        return True

    if _failure_type == "context_overflow":
        _compacted = _try_compact_and_retry(
            orch=orch,
            task=task,
            task_id=task_id,
            session=session,
            tasks_snapshot=tasks_snapshot,
            fallback_model=_fallback_model,
        )
        error_type = "context_overflow_compacted" if _compacted else "context_overflow_compact_failed"
        emit_orphan_metrics(orch._workdir, task_id, session, start_ts, success=False, error_type=error_type)
        orch._record_provider_health(session, success=False)
        return True

    return False


def _run_cascade_fallback(
    orch: Any,
    task: Task,
    task_id: str,
    session: AgentSession,
    _rl_tracker: Any,
    _failure_type: str,
) -> str | None:
    """Run cascade fallback logic and return the fallback model (or None)."""
    from bernstein.core.cascade import CascadeDecision, CascadeFallbackManager

    _cascade = getattr(orch, "_cascade_manager", None)
    if _cascade is None:
        _cascade = CascadeFallbackManager(rate_limit_tracker=_rl_tracker)
        orch._cascade_manager = _cascade  # type: ignore[attr-defined]

    _throttled = frozenset(p for p in _rl_tracker.throttle_summary() if _rl_tracker.is_throttled(p))
    _current_entry = getattr(task, "model", None) or session.provider or None
    _decision = _cascade.find_fallback(
        task.complexity,
        _throttled,
        current_entry=_current_entry,
        trigger=_failure_type,
    )

    if isinstance(_decision, CascadeDecision):
        logger.info(
            "Cascade fallback: task %s reassigned from %s → %s (%s)",
            task_id,
            session.provider,
            _decision.fallback_provider,
            _decision.reason,
        )
        _cascade.save_metrics(orch._workdir / ".sdd" / "metrics")
        return _decision.fallback_model

    logger.warning(
        "Cascade exhausted for task %s: %s — task will wait for throttle recovery",
        task_id,
        _decision.reason,
    )
    return None


def _handle_rate_limit_orphan(
    orch: Any,
    task: Task,
    task_id: str,
    session: AgentSession,
    base: str,
    start_ts: float,
    _fallback_model: str | None,
) -> None:
    """Handle a rate-limited orphaned task: requeue or fail."""
    error_type: str | None
    if _requeue_rate_limited_task(client=orch._client, server_url=base, task=task, fallback_model=_fallback_model):
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
                    inputs={"task_id": task_id, "agent_id": session.id, "orphaned": True, "trigger": "rate_limit"},
                    output={"model": task.model or "", "provider": session.provider or ""},
                    actor="agent_lifecycle",
                )
            except OSError:
                logger.debug("WAL write failed for orphaned task_requeued %s", task_id)
    else:
        error_type = "rate_limit_requeue_failed"

    emit_orphan_metrics(orch._workdir, task_id, session, start_ts, success=False, error_type=error_type)
    orch._record_provider_health(session, success=False)
    if orch._evolution is not None:
        try:
            orch._evolution.record_task_completion(
                task=task,
                duration_seconds=round(time.time() - start_ts, 2),
                cost_usd=0.0,
                janitor_passed=False,
                model=session.model_config.model,
                provider=session.provider,
            )
        except Exception as exc:
            logger.warning("Evolution record_task_completion for orphan %s failed: %s", task_id, exc)


def _handle_orphan_no_signals(
    orch: Any,
    task: Task,
    task_id: str,
    session: AgentSession,
    base: str,
    start_ts: float,
) -> tuple[bool, str | None]:
    """Handle orphaned task without completion signals by checking work indicators."""
    completion_data = collect_completion_data(orch._workdir, session)
    files_changed = len(completion_data.get("files_modified", []))
    has_commits = False
    worktree_path = orch._spawner.get_worktree_path(session.id)
    if worktree_path is not None:
        has_commits = _has_git_commits_on_branch(worktree_path)
    clean_exit = session.exit_code == 0

    if files_changed > 0:
        summary = f"Auto-completed: agent {session.id} modified {files_changed} files (no signals to verify)"
        log_msg = (
            f"Orphaned task {task_id} auto-completed "
            f"({files_changed} files modified, no signals) after agent {session.id} died"
        )
        return _try_auto_complete(orch, task_id, base, summary, log_msg)
    if has_commits:
        summary = f"Auto-completed: agent {session.id} made git commits on branch (no signals to verify)"
        log_msg = (
            f"Orphaned task {task_id} auto-completed (git commits detected, no signals) after agent {session.id} died"
        )
        return _try_auto_complete(orch, task_id, base, summary, log_msg)
    if clean_exit:
        summary = (
            f"Auto-completed (no changes needed): agent {session.id} "
            f"exited cleanly with empty diff (exit code 0, no signals to verify)"
        )
        log_msg = (
            f"Orphaned task {task_id} auto-completed (no changes needed, clean exit) after agent {session.id} died"
        )
        return _try_auto_complete(orch, task_id, base, summary, log_msg)

    # Agent died without output
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
            "Reason: process exited (PID %s, %ds runtime). Check log: .sdd/runtime/%s.log",
            task.title,
            session.pid or "unknown",
            runtime,
            session.id,
        )
    except httpx.HTTPError as exc:
        logger.error("Failed to retry/fail orphaned task %s: %s", task_id, exc)
    return False, "no_signals"


def _try_auto_complete(
    orch: Any,
    task_id: str,
    base: str,
    summary: str,
    log_msg: str,
) -> tuple[bool, str | None]:
    """Try to auto-complete a task. Returns (success, error_type)."""
    try:
        complete_task(orch._client, base, task_id, summary)
        logger.info(log_msg)
        return True, None
    except httpx.HTTPError as exc:
        logger.error(_ORPHAN_COMPLETE_ERROR, task_id, exc)
        return False, "complete_failed"


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
    # patterns before deciding how to retry.
    if _handle_failure_detection(orch, task, task_id, session, base, start_ts, tasks_snapshot):
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
        success, error_type = _handle_orphan_no_signals(orch, task, task_id, session, base, start_ts)

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
# Loop and deadlock detection
# ---------------------------------------------------------------------------


def _poll_file_mtimes(orch: Any, detector: Any, lock_mgr: Any) -> None:
    """Poll modification times of locked files and record edits."""
    file_mtime_cache: dict[str, float] = getattr(orch, "_loop_mtime_cache", {})
    if not hasattr(orch, "_loop_mtime_cache"):
        orch._loop_mtime_cache = file_mtime_cache  # type: ignore[attr-defined]

    for lock in lock_mgr.all_locks():
        candidate = orch._workdir / lock.file_path
        if not candidate.exists():
            candidate = Path(lock.file_path)
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        last = file_mtime_cache.get(lock.file_path, 0.0)
        if mtime > last:
            detector.record_edit(lock.agent_id, lock.file_path, mtime)
            file_mtime_cache[lock.file_path] = mtime


def _recover_loops(orch: Any, detector: Any, lock_mgr: Any) -> None:
    """Kill agents caught in edit loops and release their locks."""
    for loop in detector.detect_loops():
        session = orch._agents.get(loop.agent_id)
        if session is None or session.status == "dead":
            continue
        logger.warning(
            "Loop detected: agent %s edited '%s' %d times in %.0fs — killing agent",
            loop.agent_id,
            loop.file_path,
            loop.edit_count,
            loop.window_seconds,
        )
        with contextlib.suppress(Exception):
            orch._spawner.kill(session)
        _propagate_abort_to_children(orch, loop.agent_id)
        detector.clear_wait(loop.agent_id)
        if lock_mgr is not None:
            lock_mgr.release(loop.agent_id)


def check_loops_and_deadlocks(orch: Any) -> None:
    """Detect and recover from agent edit loops and file-lock deadlocks.

    **Loop detection** — polls modification times of files currently locked by
    active agents.  When a file's mtime advances since the last poll, the edit
    is recorded.  If the same agent edits the same file more than
    :data:`~bernstein.core.loop_detector.LOOP_EDIT_THRESHOLD` times within the
    detection window, the agent is killed so the task can be retried.

    **Deadlock detection** — builds a wait-for graph from the
    :class:`~bernstein.core.file_locks.FileLockManager` and any pending
    lock-wait entries recorded via
    :meth:`~bernstein.core.loop_detector.LoopDetector.record_lock_wait`.
    When a cycle is found, the lock held by the *oldest* agent in the cycle is
    released to break the deadlock.

    This function is a no-op when the orchestrator has no ``_loop_detector``
    attribute (e.g. in tests that do not set it up).

    Args:
        orch: Orchestrator instance.
    """
    from bernstein.core.loop_detector import LoopDetector  # noqa: TC001

    detector: LoopDetector | None = getattr(orch, "_loop_detector", None)
    if detector is None:
        return

    lock_mgr = getattr(orch, "_lock_manager", None)

    if lock_mgr is not None:
        _poll_file_mtimes(orch, detector, lock_mgr)

    _recover_loops(orch, detector, lock_mgr)

    if lock_mgr is None:
        return

    for deadlock in detector.detect_deadlocks(lock_mgr):
        logger.warning(
            "%s — releasing locks for victim agent %s",
            deadlock.description,
            deadlock.victim_agent_id,
        )
        lock_mgr.release(deadlock.victim_agent_id)
        detector.clear_wait(deadlock.victim_agent_id)


# ---------------------------------------------------------------------------
# Stale agent detection
# ---------------------------------------------------------------------------


def check_stale_agents(orch: Any) -> None:
    """Delegate stale-heartbeat checks to the shared heartbeat module."""
    heartbeat_protocol.check_stale_agents(orch)


# ---------------------------------------------------------------------------
# Stall detection via progress snapshots
# ---------------------------------------------------------------------------


def check_stalled_tasks(orch: Any) -> None:
    """Delegate stall checks to the shared heartbeat module."""
    heartbeat_protocol.check_stalled_tasks(orch)


def _has_git_commits_on_branch(worktree_path: Path) -> bool:
    """Return True if the worktree branch has commits beyond main."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return len(result.stdout.strip()) > 0
    except Exception:
        return False


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    from bernstein.core.platform_compat import process_alive

    return process_alive(pid)


# ---------------------------------------------------------------------------
# Reap dead / timed-out agents
# ---------------------------------------------------------------------------


def _refresh_heartbeat_from_signals(orch: Any, session: AgentSession, now: float) -> None:
    """Refresh heartbeat_ts using multiple signals (PID, heartbeat file, log, worktree)."""
    _hb_freshness_s = _IDLE_HEARTBEAT_THRESHOLD_S * 0.8

    if _is_process_alive(session.pid):
        session.heartbeat_ts = now
        return

    paths_to_check = [
        orch._workdir / ".sdd" / "runtime" / "heartbeats" / f"{session.id}.json",
        orch._workdir / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log",
        orch._workdir / ".sdd" / "worktrees" / session.id / ".git",
    ]
    for path in paths_to_check:
        try:
            if path.exists() and (now - path.stat().st_mtime) < _hb_freshness_s:
                session.heartbeat_ts = now
                return
        except OSError:
            pass


def _reap_wall_clock_timeout(
    orch: Any,
    session: AgentSession,
    result: Any,
    tasks_snapshot: dict[str, list[Task]],
    runtime: float,
) -> None:
    """Reap an agent that exceeded its wall-clock timeout."""
    collector = get_collector()
    orch._spawner.kill(session)
    _propagate_abort_to_children(orch, session.id)
    result.reaped.append(session.id)
    _release_file_ownership(orch, session.id)
    _release_task_to_session(orch, session.task_ids)
    collector.end_agent(session.id)
    if orch._evolution is not None:
        with contextlib.suppress(Exception):
            orch._evolution.record_agent_lifetime(
                agent_id=session.id,
                role=session.role,
                lifetime_seconds=round(runtime, 2),
                tasks_completed=0,
                _model=session.model_config.model,
            )
    with contextlib.suppress(OSError):
        orch._signal_mgr.clear_signals(session.id)
    for task_id in session.task_ids:
        handle_orphaned_task(orch, task_id, session, tasks_snapshot)
    _save_partial_work(orch._spawner, session)
    _preserved = getattr(orch, "_preserved_worktrees", {})
    _session_preserved = any(
        orch._spawner.get_worktree_path(session.id) == _preserved.get(tid) for tid in session.task_ids
    )
    if not _session_preserved:
        orch._spawner.cleanup_worktree(session.id)


def _reap_heartbeat_timeout(
    orch: Any,
    session: AgentSession,
    result: Any,
    tasks_snapshot: dict[str, list[Task]],
    now: float,
    age: float,
) -> None:
    """Reap an agent whose heartbeat went stale."""
    collector = get_collector()
    logger.warning("Reaping stale agent %s (last heartbeat %.0fs ago)", session.id, age)
    orch._spawner.kill(session)
    _propagate_abort_to_children(orch, session.id)
    result.reaped.append(session.id)
    _release_file_ownership(orch, session.id)
    _release_task_to_session(orch, session.task_ids)
    collector.end_agent(session.id)
    if orch._evolution is not None:
        with contextlib.suppress(Exception):
            orch._evolution.record_agent_lifetime(
                agent_id=session.id,
                role=session.role,
                lifetime_seconds=round(now - session.spawn_ts, 2),
                tasks_completed=0,
                _model=session.model_config.model,
            )
    orch._record_provider_health(session, success=False)
    with contextlib.suppress(OSError):
        orch._signal_mgr.clear_signals(session.id)
    for task_id in session.task_ids:
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
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        timeout_s = session.timeout_s if session.timeout_s is not None else orch._config.max_agent_runtime_s
        runtime = now - session.spawn_ts
        _time_since_heartbeat = now - session.heartbeat_ts if session.heartbeat_ts > 0 else runtime
        _hard_cap_s = 5400  # 90 minutes absolute maximum
        if runtime > timeout_s and _time_since_heartbeat < 120 and timeout_s < _hard_cap_s:
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
            _reap_wall_clock_timeout(orch, session, result, tasks_snapshot, runtime)
            continue

        _refresh_heartbeat_from_signals(orch, session, now)

        age = now - session.heartbeat_ts
        if session.heartbeat_ts > 0 and age > orch._config.heartbeat_timeout_s:
            _reap_heartbeat_timeout(orch, session, result, tasks_snapshot, now, age)


# ---------------------------------------------------------------------------
# Idle agent detection and recycling
# ---------------------------------------------------------------------------
#
# The canonical implementation lives in
# :mod:`bernstein.core.agents.agent_recycling`.  Its symbols are re-exported
# from the bottom of this module (see the ``from agent_recycling import ...``
# block at EOF) so existing importers of ``bernstein.core.agent_lifecycle``
# continue to work while the constants and algorithm have a single source
# of truth.  This closes audit-010 — previously ``_detect_idle_reason`` and
# its four ``_IDLE_*`` thresholds existed in parallel copies that could
# (and did) silently diverge when one was tuned and the other was not.
#
# The import lives at EOF because ``agent_recycling`` pulls in
# ``agent_reaping``, which imports several helpers defined later in *this*
# module; deferring the import until after those definitions avoids the
# circular-import failure.


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
        _propagate_abort_to_children(orch, session_id)
        result.reaped.append(session_id)


def send_shutdown_signals(orch: Any, reason: str, stagger_delay_s: float = 0.0) -> None:
    """Write SHUTDOWN signal files to all currently active agents.

    Called when ``bernstein stop`` is issued or the budget is hit so
    agents can save WIP before the orchestrator exits.

    When *stagger_delay_s* > 0, signals are sent one at a time with a
    ``time.sleep(stagger_delay_s)`` gap between each agent.  This prevents
    a thundering-herd of simultaneous merge attempts during drain mode.

    Args:
        orch: Orchestrator instance.
        reason: Human-readable reason for the shutdown.
        stagger_delay_s: Seconds to wait between consecutive SHUTDOWN signals.
            Default 0 means all signals are sent without delay (original
            behaviour, preserving backward compatibility).
    """
    active = [s for s in orch._agents.values() if s.status != "dead"]
    for idx, session in enumerate(active):
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with contextlib.suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=reason, task_title=task_title)
        if stagger_delay_s > 0 and idx < len(active) - 1:
            time.sleep(stagger_delay_s)


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


# ---------------------------------------------------------------------------
# Re-exports: canonical idle-detection / recycling implementation.
#
# Deferred to end-of-module to avoid a circular import via
# ``agent_recycling -> agent_reaping -> agent_lifecycle``.  See audit-010.
# ---------------------------------------------------------------------------

from bernstein.core.agents.agent_recycling import (  # noqa: E402, F401 — re-exported for back-compat
    _IDLE_GRACE_S,
    _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S,
    _IDLE_HEARTBEAT_THRESHOLD_S,
    _IDLE_LIVENESS_EXTENSION_S,
    _detect_idle_reason,
    _reap_completed_agent,
    _recycle_or_kill,
    recycle_idle_agents,
)
