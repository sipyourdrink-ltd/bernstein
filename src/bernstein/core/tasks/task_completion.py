"""Task completion, retry, and post-completion processing.

Extracted from task_lifecycle.py to reduce module size.  Covers retry
escalation and log parsing for completed agent work.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.metrics import get_collector
from bernstein.core.tasks.models import (
    AgentSession,
    Task,
)
from bernstein.core.tick_pipeline import (
    CompletionData,
    fail_task,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Completion data extraction
# ---------------------------------------------------------------------------


def collect_completion_data(workdir: Path, session: AgentSession) -> CompletionData:
    """Read agent log file and extract structured completion data.

    Parses the agent's runtime log into a backward-compatible completion payload.

    Args:
        workdir: Project working directory.
        session: Agent session whose log to parse.

    Returns:
        Dict with files_modified, test_results, and optional log_summary keys.
    """
    aggregator = AgentLogAggregator(workdir)
    summary = aggregator.parse_log(session.id)
    data: CompletionData = {
        "files_modified": list(summary.files_modified),
        "test_results": {},
    }
    if aggregator.log_exists(session.id) and summary.total_lines > 0:
        data["log_summary"] = summary
    if summary.test_summary:
        data["test_results"] = {"summary": summary.test_summary}
    return data


# ---------------------------------------------------------------------------
# Task retry / fail
# ---------------------------------------------------------------------------


_EFFORT_LADDER = ["low", "medium", "high", "max"]
_MODEL_LADDER = ["haiku", "sonnet", "opus"]
_HIGH_STAKES_ROLES = ("architect", "security")


def _escalate_model_effort(task: Task, next_retry: int) -> tuple[str, str]:
    """Determine escalated model and effort for a retry attempt."""
    from bernstein.core.tasks.models import Scope as _Scope

    if task.scope == _Scope.LARGE or task.role in _HIGH_STAKES_ROLES:
        return "opus", "max"

    current_model = task.model or "sonnet"
    current_effort = task.effort or "high"

    if next_retry == 1:
        idx = _EFFORT_LADDER.index(current_effort) if current_effort in _EFFORT_LADDER else 2
        return current_model, _EFFORT_LADDER[min(idx + 1, len(_EFFORT_LADDER) - 1)]

    model_lower = current_model.lower()
    model_idx = 1
    for i, name in enumerate(_MODEL_LADDER):
        if name in model_lower:
            model_idx = i
            break
    return _MODEL_LADDER[min(model_idx + 1, len(_MODEL_LADDER) - 1)], "high"


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

    # Determine current retry count from title prefix [RETRY N]
    retry_count = 0
    m = re.match(r"^\[RETRY (\d+)\] ", task.title)
    if m:
        retry_count = int(m.group(1))

    if retry_count >= max_task_retries:
        base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
        quarantine.record_failure(base_title, "Max retries exhausted")
        logger.warning(
            "Task %r exhausted %d retries -- recorded cross-run failure in quarantine",
            base_title,
            max_task_retries,
        )
        return False

    next_retry = retry_count + 1
    new_model, new_effort = _escalate_model_effort(task, next_retry)

    base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
    new_title = f"[RETRY {next_retry}] {base_title}"
    failure_context = ""
    if workdir is not None and session_id:
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

    new_description = f"[RETRY {next_retry}] {task.description}"
    if failure_context:
        new_description = (
            f"[RETRY {next_retry}] {task.description}\n\n"
            "## Previous attempt failed\n"
            f"{failure_context}\n\n"
            "Avoid the same mistakes. If you hit the same error, try a different approach."
        )

    # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
    progressive_minutes = task.estimated_minutes * (retry_count + 2)

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
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=payload)
        resp.raise_for_status()
        new_task_id = resp.json().get("id", "?")
        retried_task_ids.add(task.id)
        logger.info(
            "Retry %d queued for failed task %s -> %s (model=%s effort=%s)",
            next_retry,
            task.id,
            new_task_id,
            new_model,
            new_effort,
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
    fatal_markers = (
        "syntaxerror",
        "syntax error",
        "fatal",
        "typeerror",
        "valueerror",
        "nameerror",
        "attributeerror",
    )
    if any(k in reason_lower for k in transient_markers):
        max_retries = 3
    elif any(k in reason_lower for k in fatal_markers):
        max_retries = 0
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
        from bernstein.core.tasks.models import Scope as _Scope

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
        # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
        # so retry 1 doubles the time, retry 2 triples it, giving agents more runway.
        progressive_minutes = task.estimated_minutes * (retry_count + 2)

        # Budget escalation: when the agent hit the per-task budget cap,
        # double the budget_multiplier so the retry gets more runway.
        prev_multiplier = float(task.metadata.get("budget_multiplier", 1.0))
        is_budget_fail = "max_budget" in reason.lower() or "budget" in reason.lower()
        budget_multiplier = prev_multiplier * 2.0 if is_budget_fail else prev_multiplier
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
