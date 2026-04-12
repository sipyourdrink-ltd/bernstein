"""Task retry, escalation, and failure handling.

Extracted from task_lifecycle.py — contains retry logic, model/effort
escalation ladders, and failure context extraction.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.defaults import TASK
from bernstein.core.metrics import get_collector
from bernstein.core.tick_pipeline import (
    fail_task,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_EFFORT_LADDER = ["low", "medium", "high", "max"]
_MODEL_LADDER = ["haiku", "sonnet", "opus"]

_XL_ROLES = frozenset({"architect", "security", "manager"})


def _bump_effort(current_effort: str) -> str:
    """Return the next effort level, capped at 'max'."""
    idx = _EFFORT_LADDER.index(current_effort) if current_effort in _EFFORT_LADDER else 2
    return _EFFORT_LADDER[min(idx + 1, len(_EFFORT_LADDER) - 1)]


def _escalate_model(current_model: str) -> str:
    """Return the next model in the escalation ladder, capped at 'opus'."""
    model_lower = current_model.lower()
    model_idx = 1  # default to sonnet position
    for i, name in enumerate(_MODEL_LADDER):
        if name in model_lower:
            model_idx = i
            break
    return _MODEL_LADDER[min(model_idx + 1, len(_MODEL_LADDER) - 1)]


def _choose_retry_escalation(
    task: Task,
    next_retry: int,
    current_model: str,
    current_effort: str,
) -> tuple[str, str]:
    """Decide model and effort for the retry based on terminal reason and context.

    Returns (new_model, new_effort).
    """
    from bernstein.core.models import Scope as _Scope

    terminal_reason = task.terminal_reason

    if terminal_reason == "error_max_turns":
        new_effort = _bump_effort(current_effort) if current_effort != "max" else current_effort
        return current_model, new_effort

    if terminal_reason == "error_max_budget_usd":
        return current_model, "max"

    if terminal_reason == "model_error":
        return current_model, current_effort

    if terminal_reason == "blocking_limit":
        return "opus", "max"

    if task.scope == _Scope.LARGE or task.role in ("architect", "security"):
        return "opus", "max"

    if task.deadline is not None and time.time() > task.deadline:
        return "opus", "max"

    if next_retry == 1:
        return current_model, _bump_effort(current_effort)

    # Second+ retry: escalate model, reset effort to high
    return _escalate_model(current_model), "high"


def _extract_failure_context(
    task: Task,
    workdir: Path | None,
    session_id: str | None,
) -> str:
    """Extract failure context from the agent log for retry descriptions."""
    if workdir is None or not session_id:
        return ""

    aggregator = AgentLogAggregator(workdir)
    failure_context = aggregator.failure_context_for_retry(session_id)
    summary = aggregator.parse_log(session_id)
    if summary.dominant_failure_category:
        try:
            get_collector(workdir / ".sdd" / "metrics").record_error(
                summary.dominant_failure_category,
                "retry",
                role=task.role,
            )
        except Exception as exc:
            logger.debug("Failed to record retry failure category metric: %s", exc)

    return failure_context


def maybe_retry_task(
    task: Task,
    *,
    retried_task_ids: set[str],
    max_task_retries: int,
    client: httpx.Client,
    server_url: str,
    quarantine: Any,
    workdir: Path | None = None,
    session_id: str | None = None,
) -> bool:
    """Queue a retry for a failed task with model/effort escalation.

    First retry bumps effort one level (low->medium->high->max), keeps model.
    Second retry escalates model (haiku->sonnet->opus) and resets effort to high.

    Args:
        task: The failed task to potentially retry.
        retried_task_ids: Set of task IDs already retried (mutated in-place).
        max_task_retries: Maximum retries allowed.
        client: httpx client.
        server_url: Task server base URL.
        quarantine: QuarantineStore instance.
        workdir: Optional repo root used to inspect the failed agent log.
        session_id: Optional failed session ID for failure-context extraction.

    Returns:
        True if a retry task was created, False otherwise.
    """
    if task.id in retried_task_ids:
        return False

    retry_count = task.retry_count

    if retry_count >= task.max_retries:
        base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
        quarantine.record_failure(base_title, "Max retries exhausted")
        logger.warning(
            "Task %r exhausted %d retries -- recorded cross-run failure in quarantine",
            base_title,
            max_task_retries,
        )
        return False

    next_retry = retry_count + 1
    base_delay = task.retry_delay_s if task.retry_delay_s > 0 else TASK.retry_base_delay_s
    backoff_delay = min(base_delay * (2**retry_count), TASK.retry_max_backoff_s)

    current_model = task.model or "sonnet"
    current_effort = task.effort or "high"

    new_model, new_effort = _choose_retry_escalation(task, next_retry, current_model, current_effort)

    base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
    new_title = f"[RETRY {next_retry}] {base_title}"

    failure_context = _extract_failure_context(task, workdir, session_id)

    new_description = f"[RETRY {next_retry}] {task.description}"
    if failure_context:
        new_description = (
            f"[RETRY {next_retry}] {task.description}\n\n"
            "## Previous attempt failed\n"
            f"{failure_context}\n\n"
            "Avoid the same mistakes. If you hit the same error, try a different approach."
        )

    progressive_minutes = task.estimated_minutes * (retry_count + 2)

    # When the previous attempt hit the per-task budget cap, double the
    # budget for the retry so the agent has enough runway to finish.
    prev_multiplier = float(task.metadata.get("budget_multiplier", 1.0))
    budget_multiplier = prev_multiplier * 2.0 if task.terminal_reason == "error_max_budget_usd" else prev_multiplier

    retry_metadata = dict(task.metadata)
    retry_metadata["budget_multiplier"] = budget_multiplier

    payload: dict[str, Any] = {
        "title": new_title,
        "description": new_description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": progressive_minutes,
        "model": new_model,
        "effort": new_effort,
        "deadline": task.deadline,
        "retry_count": next_retry,
        "max_retries": task.max_retries,
        "created_at": time.time() + backoff_delay,
        "retry_delay_s": base_delay,
        "terminal_reason": None,
        "metadata": retry_metadata,
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=payload)
        resp.raise_for_status()
        new_task_id = resp.json().get("id", "?")
        retried_task_ids.add(task.id)
        logger.info(
            "Retry %d queued for failed task %s -> %s (model=%s effort=%s budget_mult=%.1fx)",
            next_retry,
            task.id,
            new_task_id,
            new_model,
            new_effort,
            budget_multiplier,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to queue retry for task %s: %s", task.id, exc)
        return False


def retry_or_fail_task(
    task_id: str,
    reason: str,
    *,
    client: httpx.Client,
    server_url: str,
    max_task_retries: int,
    retried_task_ids: set[str],
    tasks_snapshot: dict[str, list[Task]] | None = None,
) -> None:
    """Re-queue a task for retry, or fail it permanently if max retries reached.

    Reads the current retry count from a ``[retry:N]`` marker in the task
    description.  If the count is below ``max_task_retries`` a new open task
    is created (clone of the original with the marker bumped) and the old
    task is failed silently.  Once the limit is hit the task is failed with
    a "Max retries exceeded" reason.

    Args:
        task_id: ID of the task to retry or fail.
        reason: Human-readable reason for the failure / retry.
        client: httpx client.
        server_url: Task server base URL.
        max_task_retries: Maximum number of retries allowed.
        retried_task_ids: Set of already-retried task IDs (mutated in-place).
        tasks_snapshot: Optional pre-fetched tasks snapshot to avoid an
            extra HTTP round-trip when the task is already in cache.
    """
    from bernstein.core.models import Task

    base = server_url

    # Dynamic retry limit based on failure type (T176)
    reason_lower = reason.lower()
    transient_markers = (
        "rate limit",
        "timeout",
        "503",
        "transient",
        "connection error",
        "connection refused",
        "502",
        "504",
        "429",
        "too many requests",
        "service unavailable",
        "overloaded",
        "temporary failure",
        "network error",
        "internal server error",
    )
    # Only treat agent-process-level fatal errors as truly fatal (0 retries).
    # Python exception type names (TypeError, ValueError, etc.) appear routinely
    # in tool output and log snippets — they do NOT indicate an unrecoverable
    # agent failure, so they should not suppress retries.
    fatal_markers = (
        "syntaxerror",
        "syntax error",
        "fatal",
    )
    if any(k in reason_lower for k in transient_markers):
        max_retries = TASK.transient_max_retries
    elif any(k in reason_lower for k in fatal_markers):
        max_retries = TASK.fatal_max_retries
    else:
        max_retries = max_task_retries

    # Try the pre-fetched snapshot first to avoid an extra GET
    task: Task | None = None
    if tasks_snapshot is not None:
        for bucket in tasks_snapshot.values():
            for t in bucket:
                if t.id == task_id:
                    task = t
                    break
            if task is not None:
                break
        if task is not None:
            logger.debug("retry_or_fail_task %s: resolved from tick snapshot", task_id)

    if task is None:
        try:
            resp = client.get(f"{base}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
        except httpx.HTTPError as exc:
            logger.error("retry_or_fail_task: could not fetch task %s: %s", task_id, exc)
            return

    # Dedup: prevent retry fan-out (same task retried multiple times)
    if task_id in retried_task_ids:
        logger.debug("Skipping duplicate retry for task %s", task_id)
        return
    retried_task_ids.add(task_id)

    # Extract current retry count from description marker
    marker_re = re.compile(r"^\[retry:(\d+)\]\s*")
    m = marker_re.match(task.description)
    retry_count = int(m.group(1)) if m else 0
    base_description = marker_re.sub("", task.description)

    if retry_count < max_retries:
        failure_note = (
            f"\n\n## Previous attempt failed\nReason: {reason}\n"
            "Avoid the same mistake. If you hit the same error, try a different approach."
        )
        new_description = f"[retry:{retry_count + 1}] {base_description}{failure_note}"
        # Escalate model on retry: large/architect/security always opus/max;
        # other roles: sonnet->opus on 2nd retry, effort->high on 1st retry.
        from bernstein.core.models import Scope as _Scope

        _high_stakes_roles = ("architect", "security")
        if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
            retry_model = "opus"
            retry_effort = "max"
        elif retry_count >= 1:
            retry_model = "opus"
            retry_effort = "high"
        else:
            retry_model = task.model or "sonnet"
            retry_effort = task.effort or "high"

        # Max output tokens escalation (T415)
        new_max_output_tokens = task.max_output_tokens
        if "max_output_tokens" in reason.lower() or "truncated" in reason.lower():
            # Canonical escalation: double the previous limit (default 4k -> 8k -> 16k...)
            current_limit = task.max_output_tokens or 4096
            new_max_output_tokens = min(current_limit * 2, 1_000_000)
            logger.info(
                "Escalating max_output_tokens for task %s: %d -> %d",
                task_id,
                current_limit,
                new_max_output_tokens,
            )

        # Meta messages / Nudges (T423)
        new_meta_messages = list(task.meta_messages)
        new_meta_messages.append(f"Retry {retry_count + 1}: Previous attempt failed with reason: {reason}")

        # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
        # so retry 1 doubles the time, retry 2 triples it, giving agents more runway.
        progressive_minutes = task.estimated_minutes * (retry_count + 2)

        # Budget escalation: when the agent hit the per-task budget cap,
        # double the budget_multiplier so the retry gets more runway.
        prev_multiplier = float(task.metadata.get("budget_multiplier", 1.0))
        if "max_budget" in reason.lower() or "budget" in reason.lower():
            budget_multiplier = prev_multiplier * 2.0
        else:
            budget_multiplier = prev_multiplier
        retry_metadata = dict(task.metadata)
        retry_metadata["budget_multiplier"] = budget_multiplier

        task_body: dict[str, Any] = {
            "title": f"[RETRY {retry_count + 1}] {task.title}",
            "description": new_description,
            "role": task.role,
            "priority": task.priority,
            "scope": task.scope.value,
            "complexity": task.complexity.value,
            "estimated_minutes": progressive_minutes,
            "depends_on": task.depends_on,
            "owned_files": task.owned_files,
            "task_type": task.task_type.value,
            "model": retry_model,
            "effort": retry_effort,
            "max_output_tokens": new_max_output_tokens,
            "meta_messages": new_meta_messages,
            "metadata": retry_metadata,
        }
        # Preserve completion signals on retry
        if task.completion_signals:
            task_body["completion_signals"] = [{"type": s.type, "value": s.value} for s in task.completion_signals]
        try:
            client.post(f"{base}/tasks", json=task_body).raise_for_status()
            logger.info(
                "Retrying task %s (attempt %d/%d): %s",
                task_id,
                retry_count + 1,
                max_retries,
                reason,
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to re-create task %s for retry: %s", task_id, exc)
            # Fall through to permanent fail
            fail_task(client, base, task_id, f"Max retries exceeded: {reason}")
            return
        # Fail the old task silently (it has been replaced)
        with contextlib.suppress(httpx.HTTPError):
            fail_task(client, base, task_id, f"Retried: {reason}")
    else:
        fail_task(client, base, task_id, f"Max retries exceeded: {reason}")
