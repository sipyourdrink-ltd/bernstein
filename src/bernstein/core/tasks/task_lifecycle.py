"""Task lifecycle: claim, spawn, complete, retry, decompose.

Methods extracted from the Orchestrator class to reduce orchestrator.py size.
These are free functions that accept the orchestrator instance (or its fields)
as explicit arguments so the Orchestrator methods can delegate to them.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.context import append_decision
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.cross_model_verifier import (
    CrossModelVerifierConfig,
    run_cross_model_verification_sync,
)
from bernstein.core.defaults import TASK
from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.fast_path import (
    TaskLevel,
    classify_task,
    get_l1_model_config,
    try_fast_path_batch,
)
from bernstein.core.janitor import verify_task
from bernstein.core.metrics import get_collector
from bernstein.core.router import RouterError
from bernstein.core.rule_enforcer import RulesConfig, load_rules_config, run_rule_enforcement
from bernstein.core.spawn_analyzer import SpawnAnalyzer, SpawnFailureAnalysis
from bernstein.core.tasks.lifecycle import transition_agent
from bernstein.core.tasks.models import (
    AgentSession,
    Task,
    TaskStatus,
)
from bernstein.core.team_state import TeamStateStore
from bernstein.core.tick_pipeline import (
    CompletionData,
    close_task,
    complete_task,
    fail_task,
)

if TYPE_CHECKING:
    import concurrent.futures
    from pathlib import Path

    from bernstein.core.git_ops import MergeResult
    from bernstein.core.wal import WALWriter

logger = logging.getLogger(__name__)

_XL_ROLES = frozenset({"architect", "security", "manager"})


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
# File ownership helpers
# ---------------------------------------------------------------------------


def infer_affected_paths(task: Task) -> set[str]:
    """Infer file paths a task is likely to edit from its title and description.

    Scans the combined title + description text for explicit path references
    (e.g. ``src/bernstein/core/foo.py``) and bare module names (e.g. ``foo.py``).
    Bare module names are resolved against the ``src/bernstein`` tree; only the
    first match is kept to avoid false positives.

    Args:
        task: Task whose content to scan.

    Returns:
        Set of relative file paths the task is expected to touch.
    """
    from pathlib import Path as _Path

    text = f"{task.title} {task.description}"

    # Match explicit paths like src/bernstein/core/foo.py or tests/unit/test_bar.py
    paths: set[str] = set(re.findall(r"(?:src/bernstein|tests/unit|tests/integration)/\S+\.py", text))

    # Match bare module names like "orchestrator.py" and resolve to real paths
    for match in re.findall(r"\b(\w+\.py)\b", text):
        # Skip if we already have a fully qualified path ending with this name
        if any(p.endswith(match) for p in paths):
            continue
        candidates = list(_Path("src/bernstein").rglob(match))
        if candidates:
            paths.add(str(candidates[0]))

    return paths


def _get_active_agent_files(orch: Any) -> set[str]:
    """Return the set of files currently being edited by active agents.

    Inspects the git diff in each active agent's worktree to discover which
    files have uncommitted changes.  Falls back to ``_file_ownership`` entries
    for agents whose worktree cannot be inspected.

    Args:
        orch: Orchestrator instance.

    Returns:
        Set of file paths (relative to repo root) being edited by active agents.
    """
    active_files: set[str] = set()
    spawner = getattr(orch, "_spawner", None)

    for agent_id, session in orch._agents.items():
        if session.status == "dead":
            continue
        # Try to get real changed files from the worktree git diff
        worktree_path = None
        if spawner is not None:
            _get_wt = getattr(spawner, "get_worktree_path", None)
            worktree_path = _get_wt(agent_id) if _get_wt is not None else None
        if worktree_path is not None:
            changed = _get_changed_files_in_worktree(worktree_path)
            active_files.update(changed)
        # Also include statically declared owned_files from file_ownership
        for fpath, owner in orch._file_ownership.items():
            if owner == agent_id:
                active_files.add(fpath)

    return active_files


def check_file_overlap(
    batch: list[Task],
    file_ownership: dict[str, str],
    agents: dict[str, AgentSession],
) -> bool:
    """Check if any file in the batch is owned by an active agent.

    Checks both explicitly declared ``owned_files`` and paths inferred from the
    task title/description via :func:`infer_affected_paths`.

    Args:
        batch: Tasks to check for file conflicts.
        file_ownership: Mapping of filepath -> agent_id.
        agents: Agent sessions dict.

    Returns:
        True if there is a conflict, False if safe to spawn.
    """
    for task in batch:
        # Check both explicit owned_files and inferred paths
        all_paths = set(task.owned_files) | infer_affected_paths(task)
        for fpath in all_paths:
            if fpath in file_ownership:
                owner = file_ownership[fpath]
                # Only conflict if the owning agent is still alive
                owner_session = agents.get(owner)
                if owner_session and owner_session.status != "dead":
                    logger.debug(
                        "File %s owned by active agent %s, skipping batch",
                        fpath,
                        owner,
                    )
                    return True
    return False


def prepare_speculative_warm_pool(orch: Any, task_graph: Any, tasks: list[Task]) -> None:
    """Pre-create warm-pool capacity for tasks that are one dependency away.

    This keeps AGENT-022 aligned with Bernstein's short-lived-agent invariant:
    only worktrees/adapter capacity are prepared ahead of time. No task is
    claimed and no sleeping agent process is created.

    Args:
        orch: Orchestrator instance.
        task_graph: TaskGraph for the current tick.
        tasks: Current task snapshot across statuses.
    """
    warm_pool = getattr(getattr(orch, "_spawner", None), "_warm_pool", None)
    if warm_pool is None or getattr(orch, "is_shutting_down", lambda: False)():
        return

    candidates = _speculative_warm_pool_candidates(orch, task_graph, tasks)
    if not candidates:
        return

    desired_idle = min(warm_pool.config.max_slots, len({task.role for task in candidates}))
    current_ready = warm_pool.stats().get("ready", 0)
    if desired_idle <= 0 or current_ready >= desired_idle:
        return

    from bernstein.core.warm_pool import PoolSlot

    created = 0
    try:
        for candidate in candidates[: desired_idle - current_ready]:
            warm_pool.add_slot(
                PoolSlot(
                    slot_id=f"spec-{candidate.id}",
                    role=candidate.role,
                    worktree_path="",
                    created_at=0.0,
                )
            )
            created += 1
    except RuntimeError as exc:
        logger.debug("Speculative warm-pool preparation skipped: %s", exc)
        return

    if created > 0:
        logger.info(
            "Speculative warm-pool prep: created %d idle worktree(s) for near-ready roles %s",
            created,
            sorted({task.role for task in candidates}),
        )


def _speculative_warm_pool_candidates(orch: Any, task_graph: Any, tasks: list[Task]) -> list[Task]:
    """Return blocked tasks worth pre-warming for near-future execution."""
    tasks_by_id = {task.id: task for task in tasks}
    active_files = _get_active_agent_files(orch)
    candidates: list[Task] = []

    for task in tasks:
        if task.status != TaskStatus.OPEN:
            continue
        blocking_edges = [
            edge for edge in task_graph.edges_to(task.id) if edge.semantic_type.value in {"blocks", "validates"}
        ]
        if not blocking_edges:
            continue
        unresolved = [
            edge.source
            for edge in blocking_edges
            if tasks_by_id.get(edge.source) is not None and tasks_by_id[edge.source].status != TaskStatus.DONE
        ]
        if len(unresolved) != 1:
            continue
        if set(task.owned_files) & active_files:
            continue
        candidates.append(task)

    candidates.sort(key=lambda task: (task.priority, -task.estimated_minutes, task.id))
    return candidates


def _batch_timeout_seconds(batch: list[Task]) -> int:
    """Return the spawn timeout bucket for a task batch.

    The timeout contract is intentionally coarse-grained so operators can reason
    about behavior without reconstructing adaptive multipliers:
    small=15m, medium=30m, large=60m, xl=120m.
    """
    bucket_seconds = max(TASK.scope_timeout_s.get(task.scope.value, 30 * 60) for task in batch)
    xl_batch = any(task.role in _XL_ROLES for task in batch) or any(
        task.scope.value == "large" and task.complexity.value == "high" for task in batch
    )
    return TASK.xl_timeout_s if xl_batch else bucket_seconds


# ---------------------------------------------------------------------------
# Task retry / fail
# ---------------------------------------------------------------------------


_EFFORT_LADDER = ["low", "medium", "high", "max"]
_MODEL_LADDER = ["haiku", "sonnet", "opus"]


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
    from bernstein.core.tasks.models import Scope as _Scope

    terminal_reason = task.terminal_reason

    match terminal_reason:
        case "error_max_turns":
            new_effort = _bump_effort(current_effort) if current_effort != "max" else current_effort
            return current_model, new_effort
        case "error_max_budget_usd":
            return current_model, "max"
        case "model_error":
            return current_model, current_effort
        case "blocking_limit":
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
    base_delay = task.retry_delay_s if task.retry_delay_s > 0 else 30.0
    backoff_delay = min(base_delay * (2**retry_count), 300.0)

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


# ---------------------------------------------------------------------------
# Auto-decomposition
# ---------------------------------------------------------------------------


def should_auto_decompose(
    task: Task,
    decomposed_task_ids: set[str],
    workdir: Path | None = None,
    force_parallel: bool = False,
) -> bool:
    """Return True if a task should be decomposed into subtasks.

    **Disabled by default.** Requires ``force_parallel=True`` (set when the
    orchestrator's ``auto_decompose`` config is enabled).

    When enabled, decomposition triggers for:
    - LARGE scope tasks
    - Tasks that have been retried 2+ times (title starts with ``[RETRY N]``
      where N >= 2)

    Args:
        task: The task to check.
        decomposed_task_ids: Set of already-decomposed task IDs.
        workdir: Repository root for coupling analysis (unused, kept for API).
        force_parallel: If True, enable decomposition logic.

    Returns:
        True when force_parallel is set AND the task meets scope/retry criteria.
    """
    if not force_parallel:
        return False

    if task.id in decomposed_task_ids:
        return False

    if task.title.startswith("[DECOMPOSE]"):
        return False

    # Extract retry count from title prefix like "[RETRY 2]"
    import re

    from bernstein.core.tasks.models import Scope as _Scope

    retry_match = re.match(r"^\[RETRY\s+(\d+)\]", task.title)
    retry_count = int(retry_match.group(1)) if retry_match else 0

    # Decompose if LARGE scope or 2+ retries
    return task.scope == _Scope.LARGE or retry_count >= 2


def create_conflict_resolution_task(
    conflicting_task: Task,
    conflicting_files: list[str],
    *,
    client: httpx.Client,
    server_url: str,
    session_id: str,
) -> str | None:
    """Create a resolver task when a merge conflict is detected.

    Called by the orchestrator immediately after a failed merge so a
    dedicated ``resolver`` agent can resolve conflicts and commit.

    Args:
        conflicting_task: The original task whose agent branch conflicted.
        conflicting_files: File paths with merge conflicts.
        client: httpx client for task server requests.
        server_url: Task server base URL.
        session_id: Agent session whose branch conflicted (for context).

    Returns:
        The new resolver task ID, or None if creation failed.
    """
    files_list = "\n".join(f"- {f}" for f in conflicting_files)
    description = (
        f"A merge conflict was detected when merging the work of agent session "
        f"`{session_id}` (task: {conflicting_task.id} — {conflicting_task.title!r}).\n\n"
        f"## Conflicting files\n{files_list}\n\n"
        f"## Your job\n"
        f"1. For each conflicting file, read the conflict markers and understand both sides\n"
        f"2. Resolve each conflict — preserve intent from both sides where possible\n"
        f"3. After resolving all conflicts, run tests to verify correctness\n"
        f"4. Stage all resolved files and commit with a message explaining what was kept\n\n"
        f"Original task description:\n{conflicting_task.description}\n"
    )

    resolver_task_body: dict[str, Any] = {
        "title": f"[CONFLICT] {conflicting_task.title[:80]}",
        "description": description,
        "role": "resolver",
        "priority": max(1, conflicting_task.priority - 1),  # Higher priority
        "scope": "small",
        "complexity": "medium",
        "owned_files": conflicting_files,
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=resolver_task_body)
        resp.raise_for_status()
        resolver_id: str = resp.json().get("id", "?")
        logger.info(
            "Conflict resolution task %s created for session %s (%d files: %s)",
            resolver_id,
            session_id,
            len(conflicting_files),
            ", ".join(conflicting_files),
        )
        return resolver_id
    except httpx.HTTPError as exc:
        logger.warning(
            "Failed to create conflict resolution task for session %s: %s",
            session_id,
            exc,
        )
        return None


def auto_decompose_task(
    task: Task,
    *,
    client: httpx.Client,
    server_url: str,
    decomposed_task_ids: set[str],
    workdir: Path | None = None,
) -> None:
    """Queue a large task for decomposition by spawning a planner manager.

    Creates a lightweight manager task (haiku/high) that reads the original
    task and creates 3-5 atomic subtasks. The original large task stays open
    until the subtasks are done.

    Args:
        task: The large task to decompose.
        client: httpx client.
        server_url: Task server base URL.
        decomposed_task_ids: Set of decomposed task IDs (mutated in-place).
    """
    base = server_url

    if workdir is not None:
        try:
            from bernstein import get_templates_dir
            from bernstein.core.manager import ManagerAgent
            from bernstein.core.seed import parse_seed
            from bernstein.core.tasks.task_splitter import TaskSplitter

            # Read internal LLM provider/model from seed config
            _provider = "openrouter_free"
            _model = "nvidia/nemotron-3-super-120b-a12b"
            _seed_path = workdir / "bernstein.yaml"
            if _seed_path.exists():
                try:
                    _seed = parse_seed(_seed_path)
                    _provider = _seed.internal_llm_provider
                    _model = _seed.internal_llm_model
                except Exception:
                    pass

            created_ids = TaskSplitter(client=client, server_url=base).split(
                task,
                ManagerAgent(
                    server_url=server_url,
                    workdir=workdir,
                    templates_dir=get_templates_dir(workdir),
                    model=_model,
                    provider=_provider,
                ),
            )
            decomposed_task_ids.add(task.id)
            logger.info(
                "Auto-decompose: directly created %d subtasks for task %s ('%s')",
                len(created_ids),
                task.id,
                task.title,
            )
            return
        except Exception as exc:
            logger.warning("Auto-decompose direct split failed for %s, falling back to planner task: %s", task.id, exc)

    manager_description = (
        f"A large task needs to be decomposed into 3-5 smaller, atomic subtasks.\n\n"
        f"## Original large task (id={task.id})\n"
        f"**Title:** {task.title}\n"
        f"**Role:** {task.role}\n"
        f"**Description:**\n{task.description}\n\n"
        f"## Your job\n"
        f"1. Read the task description carefully\n"
        f"2. Identify 3-5 specific, atomic subtasks (each completable in one agent session, < 30 min)\n"
        f"3. Each subtask should target specific files and have clear completion criteria\n"
        f"4. Create each subtask via the task server:\n"
        f"```bash\n"
        f"curl -s -X POST {base}/tasks -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"title": "...", "description": "... [subtask of {task.id}]", '
        f'"role": "{task.role}", "priority": {task.priority}, '
        f'"scope": "small", "complexity": "medium"}}\'\n'
        f"```\n"
        f"5. After creating all subtasks, exit.\n\n"
        f"IMPORTANT: Each subtask description MUST include '[subtask of {task.id}]' "
        f"so it can be tracked back to the original task."
    )

    planner_task_body: dict[str, Any] = {
        "title": f"[DECOMPOSE] {task.title[:80]}",
        "description": manager_description,
        "role": "manager",
        "priority": max(1, task.priority - 1),  # Higher priority than original
        "scope": "small",
        "complexity": "medium",
        "model": "haiku",
        "effort": "high",
    }

    try:
        resp = client.post(f"{base}/tasks", json=planner_task_body)
        resp.raise_for_status()
        planner_id = resp.json().get("id", "?")
        decomposed_task_ids.add(task.id)
        logger.info(
            "Auto-decompose: created planner task %s for large task %s ('%s')",
            planner_id,
            task.id,
            task.title,
        )
    except httpx.HTTPError as exc:
        logger.warning("Auto-decompose: failed to create planner task for %s: %s", task.id, exc)


# ---------------------------------------------------------------------------
# Claim and spawn
# ---------------------------------------------------------------------------


def claim_and_spawn_batches(
    orch: Any,  # Orchestrator instance (avoids circular import)
    batches: list[list[Task]],
    alive_count: int,
    assigned_task_ids: set[str],
    done_ids: set[str],
    result: Any,  # TickResult
) -> None:
    """Claim tasks and spawn agents for each ready batch.

    Iterates over role-grouped batches, enforces capacity/overlap/backoff
    guards, claims tasks on the server, spawns an agent, and records metrics.
    Batches that fail to spawn are tracked for backoff and eventually failed.

    Args:
        orch: Orchestrator instance.
        batches: Role-grouped task batches from group_by_role.
        alive_count: Current number of alive agents (used to enforce max_agents cap).
        assigned_task_ids: Task IDs already owned by active agents (mutated in-place).
        done_ids: IDs of already-completed tasks (reserved for future guard use).
        result: TickResult accumulator for spawned/error lists.
    """
    if getattr(orch, "is_shutting_down", lambda: False)():
        logger.debug("Skipping claim/spawn: orchestrator is shutting down")
        return

    # Pre-spawn rate-limit check: avoid wasting worktree/process resources
    # when the provider is known to be throttling requests (CRITICAL-003).
    _adapter = getattr(getattr(orch, "_spawner", None), "_adapter", None)
    if _adapter is not None and _adapter.is_rate_limited():
        logger.warning("Provider rate-limited — skipping all spawns this tick")
        return

    # Convergence guard: block entire spawn wave if system is overloaded.
    _cg = getattr(orch, "_convergence_guard", None)
    if _cg is not None:
        _merge_queue = getattr(orch, "_merge_queue", None)
        _pending_merges = len(_merge_queue) if _merge_queue is not None else 0
        _error_rate = _cg.current_error_rate()
        _spawn_rate = _cg.current_spawn_rate()
        _cg_status = _cg.is_converged(
            pending_merges=_pending_merges,
            active_agents=alive_count,
            error_rate=_error_rate if _error_rate >= 0 else None,
            spawn_rate=_spawn_rate,
        )
        if not _cg_status.ready:
            logger.warning(
                "Convergence guard blocking spawn wave: %s",
                "; ".join(_cg_status.reasons),
            )
            return

    base = orch._config.server_url
    spawn_analyzer = SpawnAnalyzer()
    if not hasattr(orch, "_spawn_failure_history"):
        orch._spawn_failure_history = {}
    raw_spawn_failure_history = getattr(orch, "_spawn_failure_history", {})
    if not isinstance(raw_spawn_failure_history, dict):
        raw_spawn_failure_history = {}
        orch._spawn_failure_history = raw_spawn_failure_history
    spawn_failure_history = cast(
        "dict[frozenset[str], list[SpawnFailureAnalysis]]",
        raw_spawn_failure_history,
    )

    # Compute fair per-role caps: ceil(max_agents * role_tasks / total_tasks).
    # Prevents any single role from consuming all agent slots while other roles starve.
    _all_task_count = sum(len(b) for b in batches)
    _tasks_per_role: dict[str, int] = defaultdict(int)
    # Count open task batches per role — direct cap prevents spawning more agents
    # than there are work items for a role (idle-agent accumulation guard).
    _batches_per_role: dict[str, int] = defaultdict(int)
    for _b in batches:
        if _b:
            _tasks_per_role[_b[0].role] += len(_b)
            _batches_per_role[_b[0].role] += 1

    # Count currently alive agents per role (baseline before this tick's spawns)
    # Exclude idle agents (those sent SHUTDOWN signal) from count since they are
    # exiting and won't accept new work. This ensures spawn prevention doesn't
    # prevent spawning when a role's last agent is idle and waiting to exit.
    _alive_per_role: dict[str, int] = defaultdict(int)
    for _agent in orch._agents.values():
        if _agent.status != "dead" and _agent.id not in orch._idle_shutdown_ts:
            _alive_per_role[_agent.role] += 1

    # Starvation prevention: promote batches for roles with 0 alive agents to the
    # front of the spawn queue. Guarantees a starving role gets at least one agent
    # before over-represented roles receive additional agents. Within each tier
    # (starving / non-starving), stable sort preserves round-robin ordering from
    # group_by_role so no role is permanently delayed.
    _starving_roles: set[str] = {b[0].role for b in batches if b and _alive_per_role[b[0].role] == 0}
    if _starving_roles:
        batches = sorted(batches, key=lambda b: 0 if (b and b[0].role in _starving_roles) else 1)
        logger.debug(
            "Starvation prevention: %d role(s) with 0 agents promoted to front: %s",
            len(_starving_roles),
            sorted(_starving_roles),
        )

    # Track agents spawned this tick per role (avoids stale alive_per_role during loop)
    _spawned_per_role: dict[str, int] = defaultdict(int)

    # Track titles claimed this tick to prevent duplicate agent assignments.
    # Strips [RETRY N] prefixes so retries don't bypass the dedup check.
    def _base_title(title: str) -> str:
        t = title
        while t.startswith("[RETRY"):
            t = t.split("] ", 1)[-1] if "] " in t else t
        return t.strip()

    _claimed_titles: set[str] = set()
    for agent in orch._agents.values():
        if agent.status != "dead":
            for tid in agent.task_ids:
                _claimed_titles.add(tid)

    for batch in batches:
        if getattr(orch, "is_shutting_down", lambda: False)():
            logger.debug("Stopping claim/spawn loop: orchestrator is shutting down")
            break
        if alive_count >= orch._config.max_agents:
            break

        # Skip batches where any task is already assigned to an active agent
        if any(t.id in assigned_task_ids for t in batch):
            continue

        # Enforce per-role cap: no role gets more than ceil(max_agents * role_tasks / total_tasks)
        # agents. This prevents a role with many tasks from occupying all slots while other roles
        # have tasks but zero agents (starvation).
        # Also capped at the number of open task batches for the role: never spawn more agents
        # than there are work items. Prevents idle accumulation when a role's queue shrinks.
        if _all_task_count > 0 and batch:
            _role = batch[0].role
            _role_cap = math.ceil(orch._config.max_agents * _tasks_per_role[_role] / _all_task_count)
            # Cap at open batches count: role can have at most one agent per available task batch
            _effective_role_cap = min(_role_cap, _batches_per_role[_role])
            _current_role_agents = _alive_per_role[_role] + _spawned_per_role[_role]
            if _current_role_agents >= _effective_role_cap:
                logger.debug(
                    "Skipping batch for role %r: at cap (%d/%d agents for %d batches)",
                    _role,
                    _current_role_agents,
                    _effective_role_cap,
                    _batches_per_role[_role],
                )
                continue

        # Dedup: skip if a task with the same base title is already active
        batch_base_titles = {_base_title(t.title) for t in batch}
        if batch_base_titles & _claimed_titles:
            logger.debug(
                "Skipping batch -- duplicate title already active: %s",
                batch_base_titles & _claimed_titles,
            )
            continue

        # Response cache: skip spawning if an identical task was already completed.
        # Check the semantic cache for a verified result — if found, complete the
        # task immediately (zero tokens, instant result).
        _response_cache: Any = getattr(orch, "_response_cache", None)
        if _response_cache is not None and len(batch) == 1:
            _task = batch[0]
            try:
                from bernstein.core.semantic_cache import ResponseCacheManager

                _cache_key = ResponseCacheManager.task_key(_task.role, _task.title, _task.description)
                _cached_entry, _sim = _response_cache.lookup_entry(_cache_key)
                if _cached_entry is not None and _cached_entry.verified:
                    logger.info(
                        "Cache hit for task '%s' (sim=%.2f) — skipping agent spawn",
                        _task.title,
                        _sim,
                    )
                    complete_task(orch._client, orch._config.server_url, _task.id, _cached_entry.response)
                    result.verified.append(_task.id)
                    continue
            except Exception as exc:
                logger.debug("Response cache lookup failed for %s: %s", _task.id, exc)

        # Skip if any owned files overlap with active agents
        _batch_sessions = getattr(orch, "_batch_sessions", {})
        _ownership_sessions = {**orch._agents, **(_batch_sessions if isinstance(_batch_sessions, dict) else {})}
        if check_file_overlap(batch, orch._file_ownership, _ownership_sessions):
            continue

        # Skip if inferred paths overlap with files actively being edited
        # in other agents' worktrees (hot-file detection — CRITICAL-007).
        _active_files = _get_active_agent_files(orch)
        if _active_files:
            _batch_inferred: set[str] = set()
            for _t in batch:
                _batch_inferred |= infer_affected_paths(_t)
            _overlap = _batch_inferred & _active_files
            if _overlap:
                logger.info(
                    "Skipping batch — file overlap with active agent worktree: %s",
                    _overlap,
                )
                continue

        # Check spawn backoff: skip batches that recently failed
        batch_key = frozenset(t.id for t in batch)
        fail_count, last_fail_ts = orch._spawn_failures.get(batch_key, (0, 0.0))
        failure_history = spawn_failure_history.get(batch_key, [])
        # Exponential backoff: base * 2^(failures-1), capped at max
        backoff_s = (
            min(
                orch._SPAWN_BACKOFF_BASE_S * (2 ** max(fail_count - 1, 0)),
                orch._SPAWN_BACKOFF_MAX_S,
            )
            if fail_count > 0
            else 0.0
        )
        if failure_history:
            should_retry, analyzed_delay = spawn_analyzer.should_retry(
                failure_history,
                max_retries=orch._MAX_SPAWN_FAILURES,
            )
            backoff_s = max(backoff_s, analyzed_delay)
            if not should_retry:
                logger.error(
                    "Skipping batch %s permanently after analyzed spawn failures",
                    [t.id for t in batch],
                )
                for task in batch:
                    with contextlib.suppress(Exception):
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            "Spawn failed permanently after classified failures",
                        )
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
                continue
        if fail_count > 0 and (time.time() - last_fail_ts) < backoff_s:
            logger.warning(
                "Skipping batch %s: in backoff after %d consecutive spawn failure(s)",
                [t.id for t in batch],
                fail_count,
            )
            continue

        # Cross-run quarantine: skip tasks that have repeatedly failed across runs.
        # action="skip" -> skip entirely; action="decompose" -> auto-decompose first.
        quarantined_tasks = [t for t in batch if orch._quarantine.is_quarantined(t.title)]
        if quarantined_tasks:
            for task in quarantined_tasks:
                entry = orch._quarantine.get_entry(task.title)
                action = entry.action if entry else "skip"
                logger.warning(
                    "Skipping quarantined task %s (title=%r, fail_count=%d, action=%s)",
                    task.id,
                    task.title,
                    entry.fail_count if entry else 0,
                    action,
                )
                if action == "decompose" and len(batch) == 1 and getattr(orch._config, "auto_decompose", False):
                    auto_decompose_task(
                        task,
                        client=orch._client,
                        server_url=base,
                        decomposed_task_ids=orch._decomposed_task_ids,
                        workdir=orch._workdir,
                    )
            continue

        # Pre-flight: auto-decompose large tasks before claiming.
        # Creates a lightweight manager task that breaks the large task into
        # 3-5 atomic subtasks; the original stays open until subtasks complete.
        # Respects auto_decompose config — disabled by default.
        if (
            getattr(orch._config, "auto_decompose", False)
            and len(batch) == 1
            and should_auto_decompose(
                batch[0],
                orch._decomposed_task_ids,
                workdir=orch._workdir,
                force_parallel=orch._config.force_parallel,
            )
        ):
            auto_decompose_task(
                batch[0],
                client=orch._client,
                server_url=base,
                decomposed_task_ids=orch._decomposed_task_ids,
                workdir=orch._workdir,
            )
            continue

        # Claim tasks BEFORE spawning to prevent duplicate agents.
        # Pass expected_version for CAS (compare-and-swap) to prevent two
        # distributed nodes from claiming the same task simultaneously.
        # Abort on server errors (5xx), CAS conflicts (409), or transport failures.
        claim_failed = False
        _orch_session_id: str | None = getattr(orch, "session_id", None)
        for task in batch:
            try:
                _claim_params: dict[str, Any] = {"expected_version": task.version}
                if _orch_session_id is not None:
                    _claim_params["claimed_by_session"] = _orch_session_id
                resp = orch._client.post(
                    f"{base}/tasks/{task.id}/claim",
                    params=_claim_params,
                )
                if resp.status_code == 409:
                    logger.info(
                        "CAS conflict claiming task %s (version %d) -- another node claimed it",
                        task.id,
                        task.version,
                    )
                    result.errors.append(f"claim:{task.id}: CAS conflict (version {task.version})")
                    claim_failed = True
                    break
                if resp.status_code >= 500:
                    logger.error(
                        "Server error %d claiming task %s -- aborting spawn",
                        resp.status_code,
                        task.id,
                    )
                    result.errors.append(f"claim:{task.id}: server error {resp.status_code}")
                    claim_failed = True
                    break
            except httpx.TransportError as exc:
                logger.error(
                    "Server unreachable claiming task %s: %s -- aborting spawn",
                    task.id,
                    exc,
                )
                result.errors.append(f"claim:{task.id}: {exc}")
                claim_failed = True
                break
        if claim_failed:
            continue

        # WAL: record pre-execution intent (committed=False).
        # The matching committed=True entry is written after successful spawn.
        # On crash recovery, uncommitted entries indicate tasks that were
        # claimed on the server but whose agent was never spawned.
        _wal: WALWriter | None = getattr(orch, "_wal_writer", None)
        if _wal is not None:
            for task in batch:
                try:
                    _wal.write_entry(
                        decision_type="task_claimed",
                        inputs={"task_id": task.id, "role": task.role, "title": task.title},
                        output={"batch_size": len(batch)},
                        actor="task_lifecycle",
                        committed=False,
                    )
                except OSError:
                    logger.debug("WAL write failed for task_claimed %s", task.id)

        # Response cache: if a functionally identical task was already completed,
        # return the cached result without spawning an agent (20-40% savings target).
        # Only applied to single-task batches — multi-task batches have complex
        # inter-task dependencies that make result reuse unsafe.
        if len(batch) == 1:
            _rc = getattr(orch, "_response_cache", None)
            if _rc is not None:
                _rc_task = batch[0]
                _rc_key = _rc.task_key(_rc_task.role, _rc_task.title, _rc_task.description)
                _cached_entry, _rc_sim = _rc.lookup_entry(_rc_key)
                if _cached_entry is not None and _cached_entry.verified:
                    _rc_completed = False
                    try:
                        complete_task(orch._client, base, _rc_task.id, _cached_entry.response)
                        # Move backlog file on cache hit
                        _move_backlog_ticket(orch._workdir, _rc_task)

                        assigned_task_ids.add(_rc_task.id)
                        _claimed_titles.add(_base_title(_rc_task.title))
                        result.spawned.append(f"response-cache:{_rc_task.id}")
                        logger.info(
                            "Verified response cache hit (similarity=%.3f) for task %s (%r) -- skipping spawn",
                            _rc_sim,
                            _rc_task.id,
                            _rc_task.title,
                        )
                        _rc.save()
                        _rc_completed = True
                    except Exception as _rc_exc:
                        logger.warning(
                            "Response cache complete_task failed for %s: %s -- falling through to spawn",
                            _rc_task.id,
                            _rc_exc,
                        )
                    if _rc_completed:
                        continue
                elif _cached_entry is not None:
                    logger.info(
                        "Ignoring unverified response cache hit for task %s (%r)",
                        _rc_task.id,
                        _rc_task.title,
                    )

        # Fast-path: try deterministic execution for trivial (L0) tasks.
        # Runs inline, marks task complete on server, skips spawner entirely.
        if try_fast_path_batch(
            batch,
            orch._workdir,
            orch._client,
            base,
            orch._fast_path_stats,
        ):
            assigned_task_ids.update(t.id for t in batch)
            result.spawned.append(f"fast-path:{batch[0].id}")
            continue

        # L1 downgrade: classify single-task batches and override to cheapest model
        if len(batch) == 1:
            l1_check = classify_task(batch[0])
            if l1_check.level == TaskLevel.L1 and not batch[0].model:
                l1_cfg = get_l1_model_config()
                batch[0].model = l1_cfg.model
                batch[0].effort = l1_cfg.effort
                logger.info(
                    "L1 downgrade for task %s -> %s/%s (%s)",
                    batch[0].id,
                    l1_cfg.model,
                    l1_cfg.effort,
                    l1_check.reason,
                )

        # Provider batch: submit eligible low-risk single-task work to
        # OpenAI/Anthropic batch APIs instead of spawning a local CLI agent.
        if len(batch) == 1:
            _batch_api = getattr(orch, "_batch_api", None)
            if _batch_api is not None:
                _batch_result = _batch_api.try_submit(orch, batch[0])
                if _batch_result.handled:
                    if _batch_result.submitted:
                        assigned_task_ids.add(batch[0].id)
                        _claimed_titles.add(_base_title(batch[0].title))
                        result.spawned.append(_batch_result.session_id or f"provider-batch:{batch[0].id}")
                    elif _batch_result.reason:
                        result.errors.append(f"batch:{batch[0].id}: {_batch_result.reason}")
                    continue

        batch_timeout_s = _batch_timeout_seconds(batch)
        _shadow_bandit_decision: Any | None = None
        _routing_bandit: Any = getattr(orch, "_bandit_router", None)
        _bandit_mode = str(getattr(orch, "_bandit_routing_mode", "static"))
        if len(batch) == 1 and _routing_bandit is not None:
            _bandit_task = batch[0]
            if not _bandit_task.model and not _bandit_task.effort:
                try:
                    _bandit_decision = _routing_bandit.select(_bandit_task)
                    if _bandit_mode == "bandit":
                        _bandit_task.model = _bandit_decision.model
                        _bandit_task.effort = _bandit_decision.effort
                        logger.info(
                            "Bandit routing selected %s/%s for task %s: %s",
                            _bandit_decision.model,
                            _bandit_decision.effort,
                            _bandit_task.id,
                            _bandit_decision.reason,
                        )
                    elif _bandit_mode == "bandit-shadow":
                        _shadow_bandit_decision = _bandit_decision
                        logger.info(
                            "Bandit shadow routing would select %s/%s for task %s: %s",
                            _bandit_decision.model,
                            _bandit_decision.effort,
                            _bandit_task.id,
                            _bandit_decision.reason,
                        )
                except Exception as _bandit_exc:
                    logger.warning(
                        "Bandit routing failed for task %s; using static routing: %s",
                        _bandit_task.id,
                        _bandit_exc,
                    )
        elif len(batch) > 1 and _routing_bandit is not None:
            logger.debug(
                "Bandit routing skipped for multi-task batch %s; static batch escalation keeps attribution clear",
                [task.id for task in batch],
            )

        try:
            # Check if any task in this batch has a preserved worktree for resume
            resume_worktree = next(
                (orch._preserved_worktrees[t.id] for t in batch if t.id in orch._preserved_worktrees),
                None,
            )
            if resume_worktree is not None:
                changed_files = _get_changed_files_in_worktree(resume_worktree)
                session = orch._spawner.spawn_for_resume(
                    batch,
                    worktree_path=resume_worktree,
                    changed_files=changed_files,
                )
                for _t in batch:
                    orch._preserved_worktrees.pop(_t.id, None)
                logger.info(
                    "Resumed %s in preserved worktree %s for tasks: %s",
                    session.id,
                    resume_worktree,
                    [t.id for t in batch],
                )
            else:
                session = orch._spawner.spawn_for_tasks(batch)

            if _shadow_bandit_decision is not None and _routing_bandit is not None:
                _session_config = session.model_config
                _routing_bandit.record_shadow_decision(
                    task=batch[0],
                    decision=_shadow_bandit_decision,
                    executed_model=_session_config.model,
                    executed_effort=_session_config.effort,
                )

            # --- A/B Testing ---
            # When A/B test mode is enabled, deterministically route each task to one
            # of two models using a 50/50 hash split so results can be compared later.
            # Only single-task batches are eligible (multi-task batches are excluded
            # because cost and quality attribution is ambiguous across tasks).
            if getattr(orch._config, "ab_test", False) and len(batch) == 1:
                from bernstein.core.ab_test_results import model_for_task

                ab_task = batch[0]
                primary_model = session.model_config.model
                # Derive the alt model: sonnet ↔ opus; gpt: o3 ↔ gpt-5.4
                if "gpt" in primary_model or "o3" in primary_model:
                    alt_model = "gpt-5.4" if "o3" in primary_model else "o3"
                else:
                    alt_model = "opus" if "sonnet" in primary_model.lower() else "sonnet"

                # 50/50 deterministic split: some tasks go to primary, others to alt
                routed_model = model_for_task(ab_task.id, primary_model, alt_model)
                if routed_model != primary_model:
                    # Re-spawn this task with the alt model (the primary session is
                    # discarded — spawn a new one with the correct model override).
                    try:
                        logger.info(
                            "A/B TEST: routing task %s to model %s (hash split)",
                            ab_task.id,
                            routed_model,
                        )
                        # Record the A/B assignment so reports can track the split
                        _ab_split_tracker = getattr(orch, "_ab_split_tracker", None)
                        if isinstance(_ab_split_tracker, dict):
                            _ab_split_tracker[ab_task.id] = routed_model
                        alt_session = orch._spawner.spawn_for_tasks(batch, model_override=routed_model)
                        alt_session.timeout_s = batch_timeout_s
                        # Replace the primary session with the routed alt session
                        del orch._agents[session.id]
                        session = alt_session
                    except Exception as ab_exc:
                        logger.warning("A/B TEST: alt-model spawn failed, keeping primary: %s", ab_exc)
                else:
                    # This task is assigned to the primary model — record it
                    _ab_split_tracker = getattr(orch, "_ab_split_tracker", None)
                    if isinstance(_ab_split_tracker, dict):
                        _ab_split_tracker[ab_task.id] = primary_model
                    logger.info(
                        "A/B TEST: routing task %s to model %s (hash split)",
                        ab_task.id,
                        primary_model,
                    )

            session.timeout_s = batch_timeout_s
            orch._agents[session.id] = session
            for _t in batch:
                orch._task_to_session[_t.id] = session.id
            _claim_file_ownership(orch, session.id, batch)
            alive_count += 1
            result.spawned.append(session.id)
            assigned_task_ids.update(t.id for t in batch)
            _claimed_titles.update(_base_title(t.title) for t in batch)
            session.heartbeat_ts = time.time()
            orch._spawn_failures.pop(batch_key, None)
            spawn_failure_history.pop(batch_key, None)
            _spawned_per_role[batch[0].role] += 1
            # Track spawn rate in convergence guard
            _convergence = getattr(orch, "_convergence_guard", None)
            if _convergence is not None:
                _convergence.record_spawn()
            # Track active-agent count for rate-limit load spreading
            _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
            if _rl_tracker is not None and session.provider:
                _rl_tracker.increment_active(session.provider)

            logger.info(
                "Spawned %s for %d tasks: %s",
                session.id,
                len(batch),
                [t.id for t in batch],
            )
            # WAL: commit the claim — agent was successfully spawned.
            # This pairs with the committed=False entry written before spawn.
            if _wal is not None:
                for _t in batch:
                    try:
                        _wal.write_entry(
                            decision_type="task_spawn_confirmed",
                            inputs={"task_id": _t.id, "agent_id": session.id},
                            output={"role": session.role},
                            actor="task_lifecycle",
                            committed=True,
                        )
                    except OSError:
                        logger.debug("WAL write failed for task_spawn_confirmed %s", _t.id)
            try:
                rec_engine = RecommendationEngine(orch._workdir)
                rec_engine.build()
                recommendations = rec_engine.for_role(session.role)
                rec_engine.record_hits(session.role, recommendations)
            except Exception as exc:
                logger.debug("Recommendation hit tracking failed: %s", exc)
            try:
                TeamStateStore(orch._workdir / ".sdd").on_spawn(
                    session.id,
                    session.role,
                    model=session.model_config.model,
                    task_ids=[t.id for t in batch],
                    provider=session.provider or "",
                )
            except Exception as _ts_exc:
                logger.debug("Team state on_spawn failed: %s", _ts_exc)

            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.start_agent(
                agent_id=session.id,
                role=session.role,
                model=session.model_config.model,
                provider=session.provider or "default",
                agent_source=session.agent_source,
                tenant_id=batch[0].tenant_id,
            )
            for _task in batch:
                collector.start_task(
                    task_id=_task.id,
                    role=session.role,
                    model=session.model_config.model,
                    provider=session.provider or "default",
                    tenant_id=_task.tenant_id,
                )
            logger.info(
                "Agent '%s' using prompt source: %s",
                session.id,
                session.agent_source,
            )
        except (OSError, RuntimeError, ValueError, RouterError) as exc:
            logger.error("Spawn failed for batch %s: %s", [t.id for t in batch], exc)
            result.errors.append(f"spawn: {exc}")
            analysis = spawn_analyzer.analyze(exc, batch[0])
            batch_history = spawn_failure_history.setdefault(batch_key, [])
            batch_history.append(analysis)
            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.record_error(
                f"agent_spawn_failed:{analysis.error_type}",
                "default",
                role=batch[0].role if batch else None,
                tenant_id=batch[0].tenant_id if batch else "default",
            )
            if not analysis.is_transient:
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed permanently ({analysis.error_type}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning("Could not mark task %s as failed: %s", task.id, fail_exc)
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
                continue
            new_count = fail_count + 1
            orch._spawn_failures[batch_key] = (new_count, time.time())
            should_retry, _ = spawn_analyzer.should_retry(batch_history, max_retries=orch._MAX_SPAWN_FAILURES)
            if new_count >= orch._MAX_SPAWN_FAILURES or not should_retry:
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed {new_count} consecutive times ({analysis.error_type}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning("Could not mark task %s as failed: %s", task.id, fail_exc)
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
            else:
                # Transient failure — release claimed tasks immediately so they
                # don't stay stuck in "claimed" status for the 15-min timeout.
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed (transient, attempt {new_count}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning(
                            "Could not release task %s after transient spawn failure: %s",
                            task.id,
                            fail_exc,
                        )


def _run_verification_gates(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
    janitor_passed: bool,
) -> tuple[bool, Any]:
    """Run quality gates, rule enforcement, and cross-model verification.

    Returns updated (janitor_passed, qg_result) tuple.
    """
    qg_result: Any = None

    # Quality gates: lint/type/test checks run after janitor, before approval.
    qg_config = getattr(orch, "_quality_gate_config", None)
    if janitor_passed and qg_config is not None:
        worktree = orch._spawner.get_worktree_path(session.id)
        gate_run_dir = worktree if worktree is not None else orch._workdir
        qg_result = orch._gate_coalescer.run(task, gate_run_dir, orch._workdir, qg_config)
        if not qg_result.passed:
            janitor_passed = False
            failed = [f"quality_gate:{r.gate}" for r in qg_result.gate_results if r.blocked and not r.passed]
            with contextlib.suppress(ValueError):
                result.verified.remove(task.id)
            result.verification_failures.append((task.id, failed))
            logger.info("Quality gates blocked merge for task %s: %s", task.id, ", ".join(failed))

    # Organizational rule enforcement: .bernstein/rules.yaml checks.
    if janitor_passed:
        rules_config: RulesConfig | None = load_rules_config(orch._workdir)
        if rules_config is not None:
            worktree = orch._spawner.get_worktree_path(session.id)
            run_dir = worktree if worktree is not None else orch._workdir
            re_result = run_rule_enforcement(task, run_dir, orch._workdir, rules_config)
            if not re_result.passed:
                janitor_passed = False
                failed = [f"rule:{v.rule_id}: {v.fix_hint}" for v in re_result.violations if v.blocked]
                with contextlib.suppress(ValueError):
                    result.verified.remove(task.id)
                result.verification_failures.append((task.id, failed))
                logger.info("Rule enforcement blocked merge for task %s: %s", task.id, ", ".join(failed))

    # Cross-model verification: route diff to a different model for review.
    if janitor_passed:
        janitor_passed = _run_cross_model_check(orch, task, session, result)

    return janitor_passed, qg_result


def _run_cross_model_check(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
) -> bool:
    """Run cross-model verification and queue fix task if blocked.

    Returns False if blocked, True otherwise.
    """
    cmv_raw = getattr(orch._config, "cross_model_verify", None)
    cmv_config: CrossModelVerifierConfig = (
        cmv_raw if isinstance(cmv_raw, CrossModelVerifierConfig) else CrossModelVerifierConfig(enabled=False)
    )
    if not cmv_config.enabled:
        return True

    worktree = orch._spawner.get_worktree_path(session.id)
    cmv_path = worktree if worktree is not None else orch._workdir
    verdict = run_cross_model_verification_sync(task, cmv_path, session.model_config.model, cmv_config)

    if verdict.verdict != "request_changes" or not cmv_config.block_on_issues:
        logger.info("Cross-model review approved task %s (reviewer=%s)", task.id, verdict.reviewer_model)
        return True

    issues_str = "; ".join(verdict.issues) if verdict.issues else verdict.feedback
    with contextlib.suppress(ValueError):
        result.verified.remove(task.id)
    result.verification_failures.append((task.id, [f"cross_model_review:{issues_str}"]))
    logger.info(
        "Cross-model review blocked merge for task %s (reviewer=%s): %s",
        task.id,
        verdict.reviewer_model,
        verdict.feedback,
    )
    _create_cmv_fix_task(orch, task, verdict)
    return False


def _create_cmv_fix_task(orch: Any, task: Task, verdict: Any) -> None:
    """Queue a fix task for cross-model review issues."""
    description = (
        f"Cross-model review flagged issues in task {task.id} "
        f"({task.title!r}).\n\n"
        f"**Reviewer:** {verdict.reviewer_model}\n"
        f"**Feedback:** {verdict.feedback}\n\n"
        f"**Issues to fix:**\n"
        + "\n".join(f"- {i}" for i in verdict.issues)
        + f"\n\nOriginal task description:\n{task.description}\n"
    )
    body: dict[str, Any] = {
        "title": f"[REVIEW-FIX] {task.title[:80]}",
        "description": description,
        "role": task.role,
        "priority": max(1, task.priority - 1),
        "scope": "small",
        "complexity": "medium",
        "owned_files": task.owned_files,
    }
    try:
        orch._client.post(f"{orch._config.server_url}/tasks", json=body).raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("cross_model_verifier: failed to create fix task for %s: %s", task.id, exc)


def _evaluate_approval_gate(
    orch: Any,
    task: Task,
    session: AgentSession,
    completion_data: CompletionData | None,
    janitor_passed: bool,
) -> bool:
    """Evaluate the approval gate and return whether to skip merge."""
    if not janitor_passed or orch._approval_gate is None:
        return False

    try:
        override_mode, timeout_s = _resolve_approval_workflow(orch, task)
        approval_result = orch._approval_gate.evaluate(
            task,
            session_id=session.id,
            override_mode=override_mode,
            timeout_s=timeout_s,
        )
        if approval_result.rejected:
            logger.warning("Approval gate: task %s rejected -- skipping merge for agent %s", task.id, session.id)
            return True
        if not approval_result.approved:
            _create_approval_pr(orch, task, session, completion_data)
            return True
    except Exception:
        logger.exception("Approval gate failed for task %s -- defaulting to auto-merge", task.id)
    return False


def _resolve_approval_workflow(orch: Any, task: Task) -> tuple[Any, float | None]:
    """Resolve approval mode and timeout from workflow config."""
    wf = getattr(orch._config, "approval_workflow", None)
    if wf is None or not wf.enabled:
        return None, None

    risk = getattr(task, "risk_level", "low")
    mapping = {
        "low": wf.low_risk,
        "medium": wf.medium_risk,
        "high": wf.high_risk,
        "critical": getattr(wf, "critical_risk", wf.high_risk),
    }
    mode_str = mapping.get(risk, "auto")

    from bernstein.core.approval import ApprovalMode

    override_mode = ApprovalMode(mode_str)
    timeout_s = float(wf.timeout_hours * 3600)

    if override_mode in (ApprovalMode.REVIEW, ApprovalMode.PR):
        orch._notify(
            event="task.approval_needed",
            title=f"Approval required ({risk.upper()} risk): {task.title}",
            body=f"Task {task.id} requires {mode_str} approval. Timeout: {wf.timeout_hours}h.",
            task_id=task.id,
            risk_level=risk,
        )
    return override_mode, timeout_s


def _create_approval_pr(
    orch: Any,
    task: Task,
    session: AgentSession,
    completion_data: CompletionData | None,
) -> None:
    """Create a PR for approval-gate PR mode."""
    worktree_path = orch._spawner.get_worktree_path(session.id)
    if worktree_path is None:
        logger.warning("Approval gate PR mode: no worktree for agent %s -- cannot create PR", session.id)
        return

    collector = get_collector(orch._workdir / ".sdd" / "metrics")
    task_m = collector.task_metrics.get(task.id)
    cost_usd = task_m.cost_usd if task_m else 0.0
    completion = completion_data or {"files_modified": [], "test_results": {}}
    test_summary = completion.get("test_results", {}).get("summary", "")
    pr_url = orch._approval_gate.create_pr(
        task,
        worktree_path=worktree_path,
        session_id=session.id,
        labels=orch._config.pr_labels,
        role=session.role,
        model=session.model_config.model,
        cost_usd=cost_usd,
        test_summary=test_summary,
    )
    if pr_url:
        logger.info("Approval gate: PR created for task %s: %s", task.id, pr_url)


def _reap_and_cleanup_session(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
    janitor_passed: bool,
    skip_merge: bool,
    completion_data: CompletionData | None,
    cache_diff_lines: int,
) -> tuple[bool, int]:
    """Reap agent, handle merge, cleanup worktree.

    Returns (cache_verified, cache_diff_lines).
    """
    merge_result: MergeResult | None = orch._spawner.reap_completed_agent(
        session,
        skip_merge=skip_merge,
        defer_cleanup=True,
    )
    if session.status != "dead":
        transition_agent(session, "dead", actor="task_lifecycle", reason="task completed, process reaped")
    logger.info("Agent %s finished task %s, process reaped", session.id, task.id)

    try:
        TeamStateStore(orch._workdir / ".sdd").on_complete(session.id)
    except Exception as exc:
        logger.debug("Team state on_complete failed: %s", exc)

    _cleanup_batch_session(orch, session)
    cache_verified = janitor_passed and session.exit_code == 0 and cache_diff_lines > 0
    _record_ab_test_outcome(orch, task, session, janitor_passed)
    merge_ok = _handle_merge_result(orch, task, result, merge_result, janitor_passed, skip_merge)

    if janitor_passed and not skip_merge and merge_ok:
        _close_completed_task(orch, task)

    orch._spawner.cleanup_worktree(session.id)
    return cache_verified, cache_diff_lines


def _cleanup_batch_session(orch: Any, session: AgentSession) -> None:
    """Remove session from batch tracking and release ownership."""
    batch_sessions = getattr(orch, "_batch_sessions", None)
    if not isinstance(batch_sessions, dict) or session.id not in batch_sessions:
        return
    cast("dict[str, AgentSession]", batch_sessions).pop(session.id, None)
    release_tasks = getattr(orch, "_release_task_to_session", None)
    if callable(release_tasks):
        release_tasks(session.task_ids)
    release_files = getattr(orch, "_release_file_ownership", None)
    if callable(release_files):
        release_files(session.id)


def _record_ab_test_outcome(
    orch: Any,
    task: Task,
    session: AgentSession,
    janitor_passed: bool,
) -> None:
    """Persist A/B test quality/cost result for this task."""
    if not getattr(orch._config, "ab_test", False):
        return
    tracker = getattr(orch, "_ab_split_tracker", None)
    if not isinstance(tracker, dict) or task.id not in tracker:
        return
    model_map = cast("dict[str, str]", tracker)
    try:
        from bernstein.core.ab_test_results import record_ab_outcome

        record_ab_outcome(
            orch._workdir,
            task_id=task.id,
            task_title=task.title,
            model=model_map[task.id],
            session_id=session.id,
            tokens_used=session.tokens_used,
            files_changed=session.files_changed,
            status="completed" if janitor_passed else "failed",
            duration_s=time.time() - session.spawn_ts,
        )
    except Exception as exc:
        logger.debug("A/B test outcome recording failed: %s", exc)


def _handle_merge_result(
    orch: Any,
    task: Task,
    result: Any,
    merge_result: MergeResult | None,
    janitor_passed: bool,
    skip_merge: bool,
) -> bool:
    """Handle merge conflicts and return whether merge succeeded."""
    if merge_result is None or merge_result.success:
        return True
    if not merge_result.conflicting_files or skip_merge:
        return False
    create_conflict_resolution_task(
        task,
        merge_result.conflicting_files,
        client=orch._client,
        server_url=orch._config.server_url,
        session_id=None,
    )
    orch._post_bulletin(
        "alert",
        f"merge conflict in {len(merge_result.conflicting_files)} files — resolver task created (task {task.id})",
    )
    return False


def _close_completed_task(orch: Any, task: Task) -> None:
    """Move backlog ticket, close task on server, close linked GitHub issue."""
    _move_backlog_ticket(orch._workdir, task)
    try:
        close_task(orch._client, orch._config.server_url, task.id)
    except Exception as exc:
        logger.warning("Failed to close task %s: %s", task.id, exc)

    issue_number = task.metadata.get("issue_number") if task.metadata else None
    if not issue_number:
        return
    try:
        from bernstein.core.github import GitHubClient

        gh = GitHubClient()
        gh.close_issue(int(issue_number), comment=f"Closed by Bernstein task {task.id}")
        logger.info("Closed GitHub issue #%s for task %s", issue_number, task.id)
    except Exception as exc:
        logger.warning("Failed to close GitHub issue #%s: %s", issue_number, exc)


def _record_bandit_outcome(
    orch: Any,
    task: Task,
    session: AgentSession,
    janitor_passed: bool,
) -> None:
    """Feed quality-cost reward to the bandit policy."""
    bandit: Any = getattr(orch, "_bandit_router", None)
    if bandit is None:
        return
    bm = get_collector(orch._workdir / ".sdd" / "metrics").task_metrics.get(task.id)
    bandit.record_outcome(
        task=task,
        model=session.model_config.model if session.model_config else "sonnet",
        effort=getattr(session, "effort", "") or "",
        cost_usd=bm.cost_usd if bm is not None else 0.0,
        quality_score=1.0 if janitor_passed else 0.0,
        budget_ceiling=max(float(getattr(orch._config, "budget_usd", 0.0) or 0.0), 1.0),
    )
    bandit.save()


def _record_completion_metrics(
    orch: Any,
    task: Task,
    session: AgentSession | None,
    janitor_passed: bool,
    qg_result: Any,
    completion_data: CompletionData | None,
    agent_just_reaped: bool,
) -> tuple[Any, float]:
    """Record task completion in metrics, cost tracker, convergence guard.

    Returns (task_metrics, cost_usd) for use by callers.
    """
    collector = get_collector(orch._workdir / ".sdd" / "metrics")
    task_m = collector.task_metrics.get(task.id)
    cost_usd = task_m.cost_usd if task_m else 0.0

    agent_id = session.id if session else "unknown"
    model = session.model_config.model if session else "unknown"
    tokens_in = task_m.tokens_prompt if task_m else 0
    tokens_out = task_m.tokens_completion if task_m else 0
    orch._cost_tracker.record_cumulative(
        agent_id=agent_id,
        task_id=task.id,
        model=model,
        total_input_tokens=tokens_in,
        total_output_tokens=tokens_out,
        total_cost_usd=cost_usd if cost_usd > 0 else None,
        tenant_id=task.tenant_id,
    )
    try:
        orch._cost_tracker.save(orch._workdir / ".sdd")
    except OSError as exc:
        logger.warning("Failed to persist cost tracker: %s", exc)

    collector.complete_task(task.id, success=janitor_passed, janitor_passed=janitor_passed, cost_usd=cost_usd)

    convergence = getattr(orch, "_convergence_guard", None)
    if convergence is not None:
        convergence.record_success() if janitor_passed else convergence.record_failure()

    try:
        budget = CompletionBudget(orch._workdir)
        budget.record_attempt(
            task,
            is_fix=("fix:" in task.title.lower()) or ("judge retry" in task.title.lower()),
            cost_usd=cost_usd,
        )
    except Exception as exc:
        logger.debug("Completion budget update failed for task %s: %s", task.id, exc)

    if session is not None:
        collector.complete_agent_task(session.id, success=janitor_passed)
        collector.end_agent(session.id)
        _record_effectiveness_score(orch, task, session, qg_result, completion_data)
        if orch._evolution is not None and agent_just_reaped:
            _record_agent_lifetime(orch, session, collector)

    return task_m, cost_usd


def _record_effectiveness_score(
    orch: Any,
    task: Task,
    session: AgentSession,
    qg_result: Any,
    completion_data: CompletionData | None,
) -> None:
    """Score agent effectiveness and persist the result."""
    try:
        scorer = EffectivenessScorer(orch._workdir)
        score = scorer.score(
            session,
            task,
            qg_result,
            completion_data.get("log_summary") if completion_data is not None else None,
        )
        scorer.record(score)
        logger.info("Agent effectiveness: %s grade=%s total=%d", session.id, score.grade, score.total)
    except Exception as exc:
        logger.debug("Effectiveness scoring failed for %s: %s", task.id, exc)


def _record_agent_lifetime(orch: Any, session: AgentSession, collector: Any) -> None:
    """Record agent lifetime to evolution collector (once per agent)."""
    try:
        agent_m = collector.agent_metrics.get(session.id)
        lifetime = round((time.time() - session.spawn_ts) if session.spawn_ts > 0 else 0.0, 2)
        tasks_done = agent_m.tasks_completed if agent_m else 0
        orch._evolution.record_agent_lifetime(
            agent_id=session.id,
            role=session.role,
            lifetime_seconds=lifetime,
            tasks_completed=tasks_done,
            model=session.model_config.model,
        )
    except Exception as exc:
        logger.warning("Evolution record_agent_lifetime failed: %s", exc)


def _post_completion_bulletin(
    orch: Any,
    task: Task,
    janitor_passed: bool,
    cache_verified: bool,
    cache_diff_lines: int,
) -> None:
    """Post bulletin and cache result for completed/failed tasks."""
    if janitor_passed:
        orch._post_bulletin("status", f"task completed: {task.title} ({task.id})")
        orch._notify(
            "task.completed",
            f"Task completed: {task.title}",
            task.result_summary or "",
            task_id=task.id,
            role=task.role,
        )
        _enqueue_paired_test_task(orch, task)
        _cache_task_result(orch, task, cache_verified, cache_diff_lines)
    else:
        orch._post_bulletin("alert", f"task failed janitor: {task.title} ({task.id})")
        orch._notify(
            "task.failed",
            f"Task failed: {task.title}",
            task.result_summary or "Janitor verification did not pass.",
            task_id=task.id,
            role=task.role,
        )


def _cache_task_result(orch: Any, task: Task, verified: bool, diff_lines: int) -> None:
    """Store result in response cache for future identical tasks."""
    if not task.result_summary:
        return
    rc = getattr(orch, "_response_cache", None)
    if rc is None:
        return
    try:
        rc.store(
            rc.task_key(task.role, task.title, task.description),
            task.result_summary,
            verified=verified,
            git_diff_lines=diff_lines,
            source_task_id=task.id,
        )
        rc.save()
    except Exception as exc:
        logger.warning("Response cache store failed for task %s: %s", task.id, exc)


def _record_evolution_completion(
    orch: Any,
    task: Task,
    session: AgentSession | None,
    task_m: Any,
    cost_usd: float,
    janitor_passed: bool,
) -> None:
    """Record task completion in evolution tracker and set agent affinity."""
    if orch._evolution is not None:
        model = session.model_config.model if session else None
        provider = session.provider if session else None
        if task_m and task_m.end_time:
            duration = task_m.end_time - task_m.start_time
        elif session and session.spawn_ts > 0:
            duration = time.time() - session.spawn_ts
        else:
            duration = 0.0
        try:
            orch._evolution.record_task_completion(
                task=task,
                duration_seconds=round(duration, 2),
                cost_usd=cost_usd,
                janitor_passed=janitor_passed,
                model=model,
                provider=provider,
            )
        except Exception as exc:
            logger.warning("Evolution record_task_completion failed: %s", exc)

    if not (janitor_passed and task.assigned_agent):
        return
    affinity: dict[str, str] | None = getattr(orch, "_agent_affinity", None)
    if affinity is None:
        return
    latest: dict[str, Task] = getattr(orch, "_latest_tasks_by_id", {})
    for downstream in latest.values():
        if task.id in downstream.depends_on and downstream.status.value == "open":
            affinity[downstream.id] = task.assigned_agent
            logger.debug(
                "agent_affinity: task %s -> agent %s (downstream of %s)",
                downstream.id,
                task.assigned_agent,
                task.id,
            )


def process_completed_tasks(
    orch: Any,  # Orchestrator instance
    done_tasks: list[Task],
    result: Any,  # TickResult
) -> None:
    """Run janitor verification and record evolution metrics for done tasks.

    Skips tasks already processed in a prior tick. For each new done task,
    submits verify_task() calls in parallel via orch._executor, then
    processes post-verification steps (sync backlog, append decision,
    record evolution) after all verifications complete.

    Args:
        orch: Orchestrator instance.
        done_tasks: Tasks with status "done" fetched from the server.
        result: TickResult accumulator for verified/verification_failures lists.
    """
    # Filter to only new tasks and mark them all processed upfront.
    new_tasks: list[Task] = []
    for task in done_tasks:
        if task.id in orch._processed_done_tasks:
            continue
        orch._processed_done_tasks[task.id] = None
        new_tasks.append(task)

    if not new_tasks:
        return

    # Fan-out: submit all verify_task() calls in parallel.
    verify_futures: dict[str, concurrent.futures.Future[tuple[bool, list[str]]]] = {}
    for task in new_tasks:
        if task.completion_signals:
            verify_futures[task.id] = orch._executor.submit(verify_task, task, orch._workdir)

    # Fan-in: collect results then run sequential post-verification steps.
    for task in new_tasks:
        _process_single_completed_task(orch, task, verify_futures, result)


def _resolve_janitor_result(
    task: Task,
    verify_futures: dict[str, Any],
    result: Any,
) -> bool:
    """Resolve janitor verification for a single task."""
    if task.id not in verify_futures:
        result.verified.append(task.id)
        return True

    try:
        passed, failed_signals = verify_futures[task.id].result()
    except Exception:
        logger.warning("verify_task raised for %s — treating as failed", task.id)
        passed = False
        failed_signals = ["verify_task exception"]

    if passed:
        result.verified.append(task.id)
    else:
        result.verification_failures.append((task.id, failed_signals))
    return passed


def _process_single_completed_task(
    orch: Any,
    task: Task,
    verify_futures: dict[str, Any],
    result: Any,
) -> None:
    """Process a single completed task through verification and post-merge pipeline."""
    cache_verified = False
    cache_diff_lines = 0
    qg_result: Any = None

    janitor_passed = _resolve_janitor_result(task, verify_futures, result)

    # WAL: record task completion/failure decision
    _wal_c: WALWriter | None = getattr(orch, "_wal_writer", None)
    if _wal_c is not None:
        wal_dtype = "task_completed" if janitor_passed else "task_failed"
        try:
            _wal_c.write_entry(
                decision_type=wal_dtype,
                inputs={"task_id": task.id, "title": task.title, "role": task.role},
                output={"janitor_passed": janitor_passed},
                actor="task_lifecycle",
            )
        except OSError:
            logger.debug("WAL write failed for %s %s", wal_dtype, task.id)

    session = orch._find_session_for_task(task.id)
    agent_just_reaped = session is not None and session.status != "dead"
    completion_data = collect_completion_data(orch._workdir, session) if session is not None else None

    if session is not None:
        worktree = orch._spawner.get_worktree_path(session.id)
        if worktree is not None:
            cache_diff_lines = _get_git_diff_line_count_in_worktree(worktree)

        janitor_passed, qg_result = _run_verification_gates(orch, task, session, result, janitor_passed)
        orch._record_provider_health(session, success=janitor_passed)
        _record_bandit_outcome(orch, task, session, janitor_passed)

        skip_merge = _evaluate_approval_gate(orch, task, session, completion_data, janitor_passed)
        cache_verified, cache_diff_lines = _reap_and_cleanup_session(
            orch,
            task,
            session,
            result,
            janitor_passed,
            skip_merge,
            completion_data,
            cache_diff_lines,
        )

    task_m, cost_usd = _record_completion_metrics(
        orch,
        task,
        session,
        janitor_passed,
        qg_result,
        completion_data,
        agent_just_reaped,
    )

    _post_completion_bulletin(orch, task, janitor_passed, cache_verified, cache_diff_lines)
    orch._sync_backlog_file(task)

    if task.result_summary:
        try:
            append_decision(orch._workdir, task.id, task.result_summary or task.title, task.result_summary)
        except Exception as exc:
            logger.warning("append_decision failed for task %s: %s", task.id, exc)

    _record_evolution_completion(orch, task, session, task_m, cost_usd, janitor_passed)


# ---------------------------------------------------------------------------
# Dedicated test-agent slot
# ---------------------------------------------------------------------------


def _enqueue_paired_test_task(orch: Any, completed_task: Task) -> None:
    """Create a paired QA task for completed implementation work.

    Guarded by ``OrchestratorConfig.test_agent`` and idempotent via a marker
    embedded in both title and description.
    """
    config = getattr(orch, "_config", None)
    test_agent_cfg = getattr(config, "test_agent", None)
    if test_agent_cfg is None:
        return
    if not bool(getattr(test_agent_cfg, "always_spawn", False)):
        return
    if str(getattr(test_agent_cfg, "trigger", "")) != "on_task_complete":
        return
    if completed_task.role.lower() in {"qa", "test", "tester"}:
        return

    marker = f"[TEST:{completed_task.id}]"
    if marker in completed_task.title or marker in completed_task.description:
        return

    try:
        existing_resp = orch._client.get(f"{orch._config.server_url}/tasks")
        existing_resp.raise_for_status()
        existing_raw = cast("list[dict[str, Any]]", existing_resp.json())
    except Exception as exc:
        logger.warning("test_agent slot: failed to list tasks for idempotency check: %s", exc)
        return

    for raw in existing_raw:
        title = str(raw.get("title", ""))
        description = str(raw.get("description", ""))
        if marker in title or marker in description:
            return

    payload: dict[str, Any] = {
        "title": f"{marker} Add tests for {completed_task.title[:72]}",
        "description": (
            f"{marker}\n"
            f"Implementation task `{completed_task.id}` completed.\n\n"
            "Write or update tests that validate the implemented behavior, "
            "cover edge cases, and prevent regressions."
        ),
        "role": "qa",
        "priority": completed_task.priority,
        "scope": "small",
        "complexity": "medium",
        "depends_on": [completed_task.id],
        "owned_files": completed_task.owned_files,
        "model": str(getattr(test_agent_cfg, "model", "sonnet")),
        "effort": "high",
    }
    try:
        orch._client.post(f"{orch._config.server_url}/tasks", json=payload).raise_for_status()
        logger.info("test_agent slot: queued paired QA task for %s", completed_task.id)
    except httpx.HTTPError as exc:
        logger.warning("test_agent slot: failed to queue paired QA task for %s: %s", completed_task.id, exc)


# ---------------------------------------------------------------------------
# Private helpers shared with claim_and_spawn_batches
# ---------------------------------------------------------------------------


def _get_changed_files_in_worktree(worktree_path: Path) -> list[str]:
    """Return the list of files changed in a worktree relative to HEAD.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        List of changed file paths, or empty list on any error.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f.strip()]
    except Exception as exc:
        logger.debug("_get_changed_files_in_worktree failed for %s: %s", worktree_path, exc)
    return []


def _get_git_diff_line_count_in_worktree(worktree_path: Path) -> int:
    """Return the total tracked diff line count in a worktree.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        Count of added plus deleted lines from ``git diff --numstat HEAD``.
        Returns 0 on any error or when there are no tracked changes.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0:
            return 0
        total = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            if parts[0].isdigit():
                total += int(parts[0])
            if parts[1].isdigit():
                total += int(parts[1])
        return total
    except Exception as exc:
        logger.debug("_get_git_diff_line_count_in_worktree failed for %s: %s", worktree_path, exc)
        return 0


def _claim_file_ownership(orch: Any, agent_id: str, tasks: list[Task]) -> None:
    """Register file ownership for files in the given tasks.

    Uses :class:`~bernstein.core.file_locks.FileLockManager` when available,
    falling back to the legacy ``_file_ownership`` dict for compatibility.

    Also claims ownership for paths inferred from the task title/description
    (CRITICAL-007) so that subsequent ``check_file_overlap`` calls detect
    conflicts even when tasks lack explicit ``owned_files``.

    Args:
        orch: Orchestrator instance.
        agent_id: The agent claiming ownership.
        tasks: Tasks whose owned_files to claim.
    """
    lock_manager = getattr(orch, "_lock_manager", None)
    for task in tasks:
        explicit_files = task.owned_files
        inferred_files = infer_affected_paths(task)
        all_files = list(set(explicit_files) | inferred_files)
        if not all_files:
            continue
        if lock_manager is not None:
            lock_manager.acquire(
                all_files,
                agent_id=agent_id,
                task_id=task.id,
                task_title=task.title,
            )
        # Keep legacy dict in sync so existing code that reads _file_ownership still works
        for fpath in all_files:
            orch._file_ownership[fpath] = agent_id


# ---------------------------------------------------------------------------
# Backlog ticket lifecycle: move completed tickets to closed/
# ---------------------------------------------------------------------------


def _move_backlog_ticket(workdir: Any, task: Any) -> None:
    """Move a completed task's backlog .md file from open/ to closed/.

    Uses the ``<!-- source: filename.md -->`` tag embedded by sync.py for
    **exact** filename matching.  Falls back to exact normalised-title match
    (never substring).  This prevents accidental closure of unrelated tickets.

    Args:
        workdir: Project root (Path-like).
        task: Completed Task object.
    """
    from pathlib import Path

    _log = logging.getLogger(__name__)
    open_dir = Path(workdir) / ".sdd" / "backlog" / "open"
    closed_dir = Path(workdir) / ".sdd" / "backlog" / "closed"
    if not open_dir.exists():
        return
    closed_dir.mkdir(parents=True, exist_ok=True)

    # --- Strategy 1: exact filename from <!-- source: ... --> tag ---
    source_match = re.search(r"<!--\s*source:\s*(\S+\.md)\s*-->", getattr(task, "description", "") or "")
    if source_match:
        source_file = open_dir / source_match.group(1)
        if source_file.exists():
            try:
                source_file.rename(closed_dir / source_file.name)
                _log.info(
                    "Moved ticket %s to closed/ (exact source match, task: %s)", source_file.name, task.title[:50]
                )
            except OSError:
                pass
            return

    # --- Strategy 2: exact normalised-title match (no substring!) ---
    title_slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")
    for md_file in [*open_dir.glob("*.yaml"), *open_dir.glob("*.md")]:
        # Parse the ticket heading and normalise it
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("# "):
                heading = re.sub(r"^[0-9a-fA-F]+\s*[—:\-]\s*", "", line[2:].strip())
                heading_slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                if heading_slug == title_slug:
                    try:
                        md_file.rename(closed_dir / md_file.name)
                        _log.info("Moved ticket %s to closed/ (title match, task: %s)", md_file.name, task.title[:50])
                    except OSError:
                        pass
                    return
                break  # only check first heading


# ---------------------------------------------------------------------------
# Priority decay for old unclaimed tasks
# ---------------------------------------------------------------------------


def deprioritize_old_unclaimed_tasks(
    orch: Any,
    threshold_hours: int | None = None,
    min_priority: int | None = None,
) -> int:
    """Deprioritize tasks that have been open for too long without being claimed.

    Called during janitor tick. Tasks open for > threshold_hours without being
    claimed have their priority decreased by 1 (min priority floor).

    Args:
        orch: Orchestrator instance.
        threshold_hours: Hours before deprioritization.
        min_priority: Minimum priority value.

    Returns:
        Count of tasks deprioritized.
    """
    from bernstein.core.tasks.models import TaskStatus

    if threshold_hours is None:
        threshold_hours = int(TASK.priority_decay_threshold_hours)
    if min_priority is None:
        min_priority = TASK.min_priority

    now = time.time()
    threshold_seconds = threshold_hours * 3600
    deprioritized_count = 0

    for task in orch._store.list_tasks():
        if task.status != TaskStatus.OPEN:
            continue

        # Check if task has been open too long
        age_seconds = now - task.created_at
        if age_seconds < threshold_seconds:
            continue

        # Check if task was ever claimed (has agent history)
        # If it was claimed and returned to open, don't deprioritize
        # For simplicity, we deprioritize all old open tasks

        old_priority = task.priority
        new_priority = min(min_priority, old_priority + 1)

        if new_priority > old_priority:
            # Update task priority (optimistic locking)
            try:
                orch._store.update_task_priority(task.id, new_priority, task.version)
                deprioritized_count += 1
                logger.info(
                    "Task %s deprioritized after %.0f h unclaimed (%d → %d)",
                    task.id,
                    age_seconds / 3600,
                    old_priority,
                    new_priority,
                )
            except Exception as exc:
                logger.debug("Failed to deprioritize task %s: %s", task.id, exc)

    return deprioritized_count


# ---------------------------------------------------------------------------
# Permission denied hooks for retry hints (T570)
# ---------------------------------------------------------------------------


def handle_permission_denied_error(error_message: str, task_id: str, role: str, retry_count: int) -> dict[str, Any]:
    """Handle permission denied errors with retry hints."""
    from bernstein.core.worker import get_permission_hint

    hint = get_permission_hint(error_message)

    if hint:
        logger.warning(f"Permission denied for task {task_id} ({role}): {error_message}\nHint: {hint}")

        # Determine if we should retry
        should_retry = retry_count < 2  # Max 2 retries for permission issues

        return {
            "permission_denied": True,
            "error_message": error_message,
            "hint": hint,
            "should_retry": should_retry,
            "retry_count": retry_count,
            "max_retries": 2,
        }
    else:
        logger.warning(f"Permission denied for task {task_id} ({role}): {error_message}")

        return {
            "permission_denied": True,
            "error_message": error_message,
            "hint": None,
            "should_retry": False,
            "retry_count": retry_count,
            "max_retries": 2,
        }
