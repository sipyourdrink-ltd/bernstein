"""State refresh, abort chains, and error recovery.

Extracted from ``agent_lifecycle`` — refreshing agent states, classifying
abort reasons, propagating abort chains, and context compaction retry logic.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import time
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.agent_reaping import (
    _maybe_preserve_worktree,
    _propagate_abort_to_children,
    _release_file_ownership,
    _release_task_to_session,
    _save_partial_work,
    handle_orphaned_task,
    purge_dead_agents,
)
from bernstein.core.lifecycle import transition_agent
from bernstein.core.models import AbortReason, AgentSession, Task, TransitionReason
from bernstein.core.task_lifecycle import retry_or_fail_task

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
            # Record failure timestamp for cooldown
            if session.role:
                # Use session.id or adapter name if available.
                # Assuming session has adapter attribute based on previous work.
                adapter_name = getattr(session, "adapter", "unknown")
                orch._agent_failure_timestamps[adapter_name] = time.time()

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
            # Save uncommitted work before destroying the worktree
            _save_partial_work(orch._spawner, session)
            # Clean up worktree unless preserved for crash-resume
            _preserved = getattr(orch, "_preserved_worktrees", {})
            _session_preserved = any(
                orch._spawner.get_worktree_path(session.id) == _preserved.get(tid) for tid in session.task_ids
            )
            if not _session_preserved:
                orch._spawner.cleanup_worktree(session.id)
            # Clean up signal/heartbeat files for naturally-dead agents
            with contextlib.suppress(OSError):
                orch._signal_mgr.clear_signals(session.id)

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

    # Find the retry task: title starts with [RETRY and contains original title
    base_title = original_task.title.removeprefix("[RETRY 1] ").removeprefix("[RETRY 2] ").removeprefix("[RETRY 3] ")
    retry_task_id: str | None = None
    for t in open_tasks:
        title = t.get("title", "")
        if title.startswith("[RETRY") and base_title in title:
            retry_task_id = t.get("id")
            # Take the most recently found (last in list)

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
