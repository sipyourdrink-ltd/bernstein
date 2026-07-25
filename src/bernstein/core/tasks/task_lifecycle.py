"""Task lifecycle: claim, spawn, complete, retry, decompose.

Methods extracted from the Orchestrator class to reduce orchestrator.py size.
These are free functions that accept the orchestrator instance (or its fields)
as explicit arguments so the Orchestrator methods can delegate to them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import time
from collections import defaultdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.agents.spawn_errors import ModelNotConfiguredError
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.context import append_decision
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.cross_model_verifier import (
    CrossModelVerifierConfig,
    run_cross_model_verification_sync,
)
from bernstein.core.defaults import TASK
from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.evidence.completion_gate import seal_evidence_on_completion
from bernstein.core.fast_path import (
    TaskLevel,
    classify_task,
    get_l1_model_config,
    try_fast_path_batch,
)
from bernstein.core.hook_events import HookEvent
from bernstein.core.janitor import run_janitor
from bernstein.core.metrics import get_collector
from bernstein.core.replay.review_board import (
    record_task_diff_captured,
    record_task_merged,
    store_task_diff,
)
from bernstein.core.router import RouterError
from bernstein.core.rule_enforcer import RulesConfig, load_rules_config, run_rule_enforcement
from bernstein.core.spawn_analyzer import SpawnAnalyzer, SpawnFailureAnalysis
from bernstein.core.tasks.artifact_completion import is_artifact_mode, verify_task_completion
from bernstein.core.tasks.auto_spawn_guard import AutoSpawnGuard, meta_task_kind
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

# Bug 1 (2026-07-02, fix/claim-conflict-churn): bounds for the claim-conflict
# recovery loop in ``_claim_task_with_conflict_retry`` / ``claim_and_spawn_batches``.
# See that function's docstring for the full root-cause writeup.
_CLAIM_CONFLICT_MAX_ATTEMPTS = 5  # hard cap on re-fetch+retry attempts within one episode
_CLAIM_CONFLICT_BACKOFF_BASE_S = 5.0  # first cross-tick backoff after an exhausted episode
_CLAIM_CONFLICT_BACKOFF_MAX_S = 300.0  # cap so a permanently-stuck task backs off at most 5 min
_CLAIM_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.DONE,
        TaskStatus.CLOSED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.ABANDONED,
        TaskStatus.REFUSED,
    }
)

# Bug 2 (2026-07-02, fix/claim-conflict-churn): hard retry ceiling applied to
# EVERY task lineage in ``retry_or_fail_task`` (meta-tasks additionally go
# through AutoSpawnGuard's ancestry/dedupe/cap checks (see below) and
# normally never reach this ceiling at all). Evidence
# (work/bernstein/proofs/d2/claim-loop-evidence/d2-minimax-final-snap.tar,
# tasks.jsonl) showed "Add test for hello subcommand" and "Commit changes on
# feature branch" each respawn 3x (retry_count 0, 1, 2) inside one 12-minute
# run with zero forward progress -- the prior effective limit
# (min(task.max_retries=3, dynamic_limit=3) = 3) allowed up to 4 total
# attempts per lineage before permanent failure. Capping the retry ceiling to
# 2 (3 total attempts) cuts one full churn cycle off every structurally-dead
# task lineage, regardless of what ``task.max_retries`` or the
# reason-derived ``dynamic_limit`` would otherwise allow.
_MAX_REGULAR_TASK_RETRIES = 2


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

    This keeps aligned with Bernstein's short-lived-agent invariant:
    only worktrees/adapter capacity are prepared ahead of time. No task is
    claimed and no sleeping agent process is created.

    Args:
        orch: Orchestrator instance.
        task_graph: TaskGraph for the current tick.
        tasks: Current task snapshot across statuses.
    """
    warm_pool = getattr(getattr(orch, "_spawner", None), "_warm_pool", None)
    if warm_pool is None or getattr(orch, "is_shutting_down", bool)():
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


def _stamp_checkpoint_retry_metadata_safe(
    *,
    task: Task,
    retry_metadata: dict[str, Any],
    workdir: Path | None,
    reason: str,
) -> dict[str, Any]:
    """Stamp the checkpointed-retry decision onto retry metadata, best-effort.

    Issue #2359: every retry carries a deterministic warm/fork/cold decision
    derived from the failed attempt's journal-anchored checkpoint. The stamp
    must never break the retry itself: any failure inside the decision path
    degrades to a plain ``retry_mode="cold"`` stamp (the historical
    behavior), and callers without a workdir (legacy tests, ad-hoc scripts)
    get the same cold stamp without touching the decision machinery.

    A task pinned to fresh-context retries (issue #1109,
    ``agent_restart_between_retries``) is forced cold so the two contracts
    never fight: the fresh-restart audit trail stays authoritative.
    """
    if workdir is None:
        retry_metadata.setdefault("retry_mode", "cold")
        return retry_metadata
    try:
        from bernstein.core.tasks import checkpoint_retry

        requested = str(retry_metadata.get("retry_policy", "warm"))
        return checkpoint_retry.stamp_checkpoint_retry_metadata(
            metadata=retry_metadata,
            task_id=task.id,
            workdir=workdir,
            requested_mode=requested,
            gate_name=str(task.terminal_reason or "task_failure"),
            gate_output=reason,
            force_cold=bool(getattr(task, "agent_restart_between_retries", False)),
        )
    except Exception as exc:
        logger.debug(
            "checkpoint-retry stamp skipped for task %s (%s); retry proceeds cold",
            task.id,
            type(exc).__name__,
        )
        retry_metadata.setdefault("retry_mode", "cold")
        return retry_metadata


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

    # ``task.retry_count`` is the single source of truth. Title
    # prefixes and ``[retry:N]`` description markers are no longer consulted
    # or written.  Legacy tasks with a stale ``[RETRY N]`` prefix retain it
    # in the title until they complete, but the counter they report is the
    # typed field - never the regex match.
    retry_count = task.retry_count
    # Apply the same _MAX_REGULAR_TASK_RETRIES hard ceiling that
    # retry_or_fail_task uses (issue #2806). The two retry paths -- this
    # tick-loop path and the reap path -- must agree on the cap; otherwise a
    # structurally-dead lineage that the reap path would dead-letter keeps
    # getting retried here, exceeding the intended ceiling.
    if max_task_retries > 0:
        effective_max = min(task.max_retries, max_task_retries, _MAX_REGULAR_TASK_RETRIES)
    else:
        effective_max = min(task.max_retries, _MAX_REGULAR_TASK_RETRIES)

    if retry_count >= effective_max:
        quarantine.record_failure(task.title, "Max retries exhausted")
        logger.warning(
            "Task %r exhausted %d retries -- recorded cross-run failure in quarantine",
            task.title,
            effective_max,
        )
        return False

    next_retry = retry_count + 1
    base_delay = task.retry_delay_s if task.retry_delay_s > 0 else 30.0
    backoff_delay = min(base_delay * (2**retry_count), 300.0)

    current_model = task.model or "sonnet"
    current_effort = task.effort or "high"

    new_model, new_effort = _choose_retry_escalation(task, next_retry, current_model, current_effort)

    failure_context = _extract_failure_context(task, workdir, session_id)

    # Title is preserved unchanged so every retry of the same task carries
    # the same title - downstream dedup / lineage keys no longer need to
    # strip a prefix.  The retry_count field carries the attempt number.
    new_title = task.title
    new_description = task.description
    if failure_context:
        new_description = (
            f"{task.description}\n\n"
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
    retry_metadata.setdefault("original_task_id", task.metadata.get("original_task_id", task.id))
    retry_metadata = _stamp_checkpoint_retry_metadata_safe(
        task=task,
        retry_metadata=retry_metadata,
        workdir=workdir,
        reason=failure_context or str(task.terminal_reason or ""),
    )

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
        "meta_messages": list(task.meta_messages),
        "max_output_tokens": task.max_output_tokens,
        # Carry forward the explicit max_turns override (if any) so a retry
        # spawn doesn't silently fall back to complexity-based auto-computation.
        "max_turns": task.max_turns,
    }
    logger.info(
        "maybe_retry_task: carrying max_turns=%r forward from task %s to retry %d",
        task.max_turns,
        task.id,
        next_retry,
    )

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


_TRANSIENT_MARKERS = (
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
_FATAL_MARKERS = ("syntaxerror", "syntax error", "fatal")

# Match markers on token boundaries, not as bare substrings. The numeric
# HTTP-status markers ("503", "429", ...) are short digit runs that alias
# by chance inside opaque identifiers embedded in a failure reason - e.g. a
# terminal reason carrying "correlation=compact-a1503f2b" contains "503"
# and would otherwise be misclassified as a transient failure and granted a
# retry budget it must never get. ``\b`` anchors each marker between word
# and non-word characters, so "HTTP 503" / "429 Too Many Requests" / "rate
# limit" still match while a hex run like "a1503f2b" does not.
_TRANSIENT_MARKER_RE = re.compile("|".join(rf"\b{re.escape(m)}\b" for m in _TRANSIENT_MARKERS))
_FATAL_MARKER_RE = re.compile("|".join(rf"\b{re.escape(m)}\b" for m in _FATAL_MARKERS))


_META_TASK_OPEN_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PLANNED,
        TaskStatus.OPEN,
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.WAITING_FOR_SUBTASKS,
        TaskStatus.PENDING_APPROVAL,
    }
)


def _collect_open_meta_task_titles(
    tasks_snapshot: dict[str, list[Task]] | None,
    *,
    exclude_task_id: str,
) -> list[str]:
    """Titles of currently open/claimed auto-spawned meta-tasks, for AutoSpawnGuard dedupe.

    Mirrors the open-status set ``orchestrator_evolve._create_upgrade_tasks``
    uses at the meta-task creation site, so the retry path's dedupe view is
    consistent with the creation-time dedupe view. ``exclude_task_id`` omits
    the task currently being retried/failed (it is about to leave these
    statuses regardless of the guard's decision).
    """
    if not tasks_snapshot:
        return []
    titles: list[str] = []
    seen_ids: set[str] = set()
    for bucket in tasks_snapshot.values():
        for candidate in bucket:
            if candidate.id == exclude_task_id or candidate.id in seen_ids:
                continue
            if getattr(candidate, "status", None) not in _META_TASK_OPEN_STATUSES:
                continue
            if meta_task_kind(candidate.title) is None:
                continue
            seen_ids.add(candidate.id)
            titles.append(candidate.title)
    return titles


def _dynamic_retry_limit(reason: str, default_max: int) -> int:
    """Determine the retry limit based on failure reason keywords.

    Markers are matched on token boundaries (see ``_TRANSIENT_MARKER_RE``) so
    an opaque identifier embedded in the reason - such as a compaction
    ``correlation=compact-<hex>`` - cannot alias a numeric HTTP-status marker
    and wrongly promote a terminal failure to a transient (retryable) one.
    """
    reason_lower = reason.lower()
    if _TRANSIENT_MARKER_RE.search(reason_lower):
        return 3
    if _FATAL_MARKER_RE.search(reason_lower):
        return 0
    return default_max


def _capture_dead_letter(entry: Any, *, original_error: str) -> None:
    """Forward a dead-letter entry to the operator error sink, best-effort.

    A task reaching the DLQ has exhausted every retry; that is an
    unexpected terminal failure worth surfacing in the error sink. The capture
    helper is fail-closed, but the import is wrapped too so a missing
    optional dependency cannot disturb the primary failure path.
    """
    try:
        from bernstein.core.observability import error_capture

        error_capture.capture_message(
            f"task moved to dead-letter queue: {entry.reason}",
            category="dead_letter",
            tags={
                "task_id": str(entry.task_id),
                "role": str(entry.role),
                "reason": str(entry.reason),
            },
            extra={
                "title": entry.title,
                "retry_count": entry.retry_count,
                "original_error": original_error[:800],
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("DLQ telemetry capture skipped for task %s: %s", entry.task_id, exc)


def _enqueue_dlq_if_workdir(
    *,
    workdir: Path | None,
    task: Task,
    retry_count: int,
    reason: str,
    original_error: str,
) -> None:
    """Record a permanently-failed task in the Dead Letter Queue.

    Looks up ``<workdir>/.sdd`` and appends an entry to ``runtime/dlq.jsonl``.
    A ``None`` workdir preserves legacy behaviour (no DLQ), and any OS or
    serialisation error is logged and suppressed - the DLQ must never block
    the primary failure path.

    Args:
        workdir: Orchestrator working directory, or ``None`` to skip.
        task: The task being moved to the DLQ.
        retry_count: Number of retries already attempted.
        reason: Short failure tag (e.g. ``"max_retries_exceeded"``).
        original_error: Last error / reason string from the final attempt.
    """
    if workdir is None:
        return
    try:
        from bernstein.core.tasks.dead_letter_queue import DeadLetterQueue

        dlq = DeadLetterQueue(sdd_dir=workdir / ".sdd")
        entry = dlq.enqueue(
            task_id=task.id,
            title=task.title,
            role=task.role,
            reason=reason,
            retry_count=retry_count,
            original_error=original_error,
            metadata={
                "priority": task.priority,
                "scope": task.scope.value,
                "complexity": task.complexity.value,
                "model": task.model or "",
                "effort": task.effort or "",
                "original_task_id": task.metadata.get("original_task_id", task.id),
            },
        )
    except Exception as exc:
        # DLQ must never break the primary failure path - log and swallow.
        logger.warning(
            "DLQ enqueue failed for task %s (%s): %s",
            task.id,
            reason,
            exc,
        )
        return

    # A task reaching the dead-letter queue is a terminal, unexpected
    # failure: every retry has been exhausted. Route it to the operator's
    # error sink so it surfaces there rather than only on disk.
    # The helper is itself fail-closed; it never breaks this path.
    _capture_dead_letter(entry, original_error=original_error)

    # Synthesise an incident eval case for this terminal failure. Failure
    # to synthesise must never block the primary path either.
    try:
        from bernstein.eval.incident_synthesizer import IncidentSynthesizer

        synth = IncidentSynthesizer(workdir)
        case = synth.synthesize_from_dlq_entry(entry)
        if case is not None:
            existing = synth._load_existing_ids()
            if case.id not in existing:
                synth._write_case(case)
    except Exception as exc:
        logger.debug("incident synthesiser skipped for task %s: %s", task.id, exc)


def retry_or_fail_task(
    task_id: str,
    reason: str,
    *,
    client: httpx.Client,
    server_url: str,
    max_task_retries: int,
    retried_task_ids: set[str],
    tasks_snapshot: dict[str, list[Task]] | None = None,
    workdir: Path | None = None,
    role_model_policy: dict[str, dict[str, Any]] | None = None,
    default_adapter_name: str | None = None,
) -> None:
    """Re-queue a task for retry, or fail it permanently if max retries reached.

    Reads the current retry count from the typed ``task.retry_count`` field -
    the single source of truth. Title and description are copied
    verbatim; no ``[RETRY N]`` / ``[retry:N]`` markers are written.  If the
    typed counter is below ``min(task.max_retries, dynamic_limit(reason))`` a
    new open task is created with ``retry_count`` incremented; otherwise the
    task is moved to the Dead Letter Queue and failed with a
    ``"Max retries exceeded"`` reason.

    Args:
        task_id: ID of the task to retry or fail.
        reason: Human-readable reason for the failure / retry.
        client: httpx client.
        server_url: Task server base URL.
        max_task_retries: Orchestrator-wide retry ceiling.  The effective
            limit is ``min(task.max_retries, dynamic_limit(reason))``.
        retried_task_ids: Set of already-retried task IDs (mutated in-place).
        tasks_snapshot: Optional pre-fetched tasks snapshot to avoid an
            extra HTTP round-trip when the task is already in cache.
        workdir: Orchestrator working directory.  When provided, tasks that
            exhaust their retry budget are also enqueued into the Dead Letter
            Queue under ``<workdir>/.sdd/runtime/dlq.jsonl``.
            Callers without a workdir (e.g. ad-hoc scripts or legacy tests)
            fall back to the historical behaviour of plain failure.
        role_model_policy: Optional ``AgentSpawner.role_model_policy`` snapshot.
            When the retrying task's role has a non-Claude ``provider``/``model``
            pinned here, retry escalation stamps ``effort``/``max_output_tokens``
            only and leaves ``model`` alone instead of stamping a Claude tier
            name ("opus"/"sonnet") that is meaningless to that adapter. Callers
            that omit this (e.g. legacy tests, ad-hoc scripts) get the
            historical Claude-tier-name-always behavior, which is correct for
            Claude-only runs and was never wrong until a non-Claude adapter
            entered the picture.
        default_adapter_name: The spawner's default adapter name (e.g.
            ``AgentSpawner.default_adapter_name``), used as the fallback
            Claude-compatibility check when the retrying role has no
            role_model_policy entry of its own.
    """
    base = server_url
    dynamic_limit = _dynamic_retry_limit(reason, max_task_retries)

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

    # source of truth is ``task.retry_count`` (typed field).
    retry_count = task.retry_count
    per_task_limit = task.max_retries if task.max_retries > 0 else max_task_retries
    # Bug 2 hard ceiling (see _MAX_REGULAR_TASK_RETRIES docstring above):
    # applies to every lineage regardless of task.max_retries or the
    # reason-derived dynamic_limit, so a structurally-dead task (e.g. its
    # agent keeps dying for an environment reason no retry can fix) burns at
    # most 2 retries before permanent failure instead of riding whatever
    # higher ceiling those other two knobs would otherwise allow.
    effective_limit = min(per_task_limit, dynamic_limit, _MAX_REGULAR_TASK_RETRIES)
    _original_task_id = task.metadata.get("original_task_id", task.id) if isinstance(task.metadata, dict) else task.id
    logger.info(
        "retry_or_fail_task decision inputs: task=%s original_task_id=%s retry_count=%d "
        "per_task_limit=%d dynamic_limit=%d hard_cap=%d -> effective_limit=%d reason=%r",
        task_id,
        _original_task_id,
        retry_count,
        per_task_limit,
        dynamic_limit,
        _MAX_REGULAR_TASK_RETRIES,
        effective_limit,
        reason,
    )

    # Auto-spawn guard: this generic retry path is a SECOND spawn site for
    # auto-spawned meta-tasks (e.g. an evolution-loop "Upgrade: ..." proposal
    # or a watchdog "Watchdog triage: ..." task) - it recreates a brand-new
    # open task row with the same title whenever the meta-task's own agent
    # dies, completely bypassing the AutoSpawnGuard that
    # ``orchestrator_evolve._create_upgrade_tasks`` / the watchdog's
    # ``_create_triage_task`` consult at CREATION time. Left unguarded, a
    # meta-task that structurally cannot succeed (e.g. the environment
    # defect it exists to work around) gets re-spawned via retry up to
    # ``max_retries`` times with zero forward progress - the exact
    # "9 Upgrade: Improve task success rate" rows seen in
    # work/bernstein/proofs/d2/minimax/sdd-snapshot/runtime/tasks.jsonl,
    # where 2 of the 3 real-lineage recreations went through THIS function,
    # never through the guarded creation site, so the guard's dedupe/cap
    # counter never even saw them.
    #
    # Retrying a meta-task is itself an auto-spawn "about" that same
    # meta-task, so ``source_title=task.title`` deterministically computes
    # ancestry depth 2 (the source title already carries a
    # ``META_TASK_PREFIXES`` prefix) -- refused by the depth<=1 cap. Net
    # effect: a meta-task gets its normal first attempt, but a failed
    # meta-task is routed straight to permanent-fail/DLQ instead of being
    # resurrected under a new task id.
    meta_kind = meta_task_kind(task.title)
    if meta_kind is not None and retry_count < effective_limit:
        if workdir is not None:
            existing_open_titles = _collect_open_meta_task_titles(tasks_snapshot, exclude_task_id=task.id)
            guard = AutoSpawnGuard(workdir)
            decision = guard.evaluate(
                kind=f"retry:{meta_kind.rstrip(':')}",
                title=task.title,
                source_title=task.title,
                existing_open_titles=existing_open_titles,
            )
            if not decision.allowed:
                logger.info(
                    "Refusing to re-spawn meta-task %s (title=%r) via retry: auto-spawn guard reason=%s "
                    "ancestry_depth=%d current_count=%d cap=%d - routing to permanent failure instead of "
                    "creating a new task row",
                    task_id,
                    task.title,
                    decision.reason,
                    decision.ancestry_depth,
                    decision.current_count,
                    decision.cap,
                )
                retry_count = effective_limit  # force the permanent-fail/DLQ branch below
        else:
            logger.info(
                "Auto-spawn guard skipped for retry of meta-task %s (title=%r): no workdir supplied "
                "(legacy/ad-hoc caller) - falling back to historical unguarded retry behaviour",
                task_id,
                task.title,
            )

    if retry_count < effective_limit:
        # Escalate model on retry: large/architect/security always opus/max;
        # other roles: sonnet->opus on 2nd retry, effort->high on 1st retry.
        from bernstein.core.tasks.models import Scope as _Scope

        _high_stakes_roles = ("architect", "security")

        # Historically this block stamped a Claude tier name ("opus"/
        # "sonnet") onto the retried task.model unconditionally - correct
        # for Claude-only runs, but for a role pinned to a non-Claude
        # provider/model (role_model_policy) or running against a
        # non-Claude default adapter, a tier name is meaningless and gets
        # spawned literally (e.g. `qwen -m opus`, the run-9 attempt-8 class
        # of bug: retry stamped model="opus" against a MiniMax endpoint).
        # Determine Claude-compatibility for the retrying role BEFORE
        # choosing retry_model so the escalation itself never produces a
        # value that has to be coerced/papered over downstream in
        # spawner_core.py. Callers that don't pass role_model_policy /
        # default_adapter_name (legacy tests, ad-hoc scripts) get
        # ``adapter_for_role is None`` -> treated as Claude-compatible,
        # i.e. today's historical behavior, unchanged.
        role_policy_entry = role_model_policy.get(task.role, {}) if isinstance(role_model_policy, dict) else {}
        pinned_model = role_policy_entry.get("model") if isinstance(role_policy_entry, dict) else None
        adapter_for_role = (
            role_policy_entry.get("provider") if isinstance(role_policy_entry, dict) else None
        ) or default_adapter_name
        adapter_is_claude_compatible = True
        if isinstance(adapter_for_role, str) and adapter_for_role:
            from bernstein.core.bandit_router import BanditRouter

            adapter_is_claude_compatible = BanditRouter.router_applicable(adapter_for_role)
        logger.info(
            "Retry escalation adapter check for task %s (role=%s): "
            "role_policy_provider=%r role_policy_model=%r default_adapter_name=%r "
            "-> adapter_for_role=%r claude_compatible=%s",
            task_id,
            task.role,
            role_policy_entry.get("provider"),
            pinned_model,
            default_adapter_name,
            adapter_for_role,
            adapter_is_claude_compatible,
        )

        if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
            retry_model = "opus" if adapter_is_claude_compatible else (pinned_model or task.model)
            retry_effort = "max"
        elif retry_count >= 1:
            retry_model = "opus" if adapter_is_claude_compatible else (pinned_model or task.model)
            retry_effort = "high"
        else:
            retry_model = (task.model or "sonnet") if adapter_is_claude_compatible else (pinned_model or task.model)
            retry_effort = task.effort or "high"

        logger.info(
            "Retry model decision for task %s (role=%s, retry_count=%s, scope=%s): "
            "model=%r effort=%r (claude_compatible=%s, pinned_model=%r, prior_task_model=%r, reason=%r)",
            task_id,
            task.role,
            retry_count,
            task.scope,
            retry_model,
            retry_effort,
            adapter_is_claude_compatible,
            pinned_model,
            task.model,
            reason,
        )

        # Max output tokens escalation (T415)
        new_max_output_tokens = task.max_output_tokens
        if "max_output_tokens" in reason.lower() or "truncated" in reason.lower():
            # Canonical escalation: double the previous limit (default 4k -> 8k -> 16k...)
            current_limit = task.max_output_tokens or 4096
            new_max_output_tokens = min(current_limit * 2, 1_000_000)
            # "token" here is the LLM output budget, not a credential.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info(
                "Escalating max_output_tokens for task %s: %d -> %d",
                task_id,
                current_limit,
                new_max_output_tokens,
            )

        # Meta messages / Nudges (T423) - append the failure reason so the
        # retry agent sees the previous attempt's outcome without us having
        # to pollute the description with ``[retry:N]`` markers.
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
        retry_metadata.setdefault("original_task_id", task.metadata.get("original_task_id", task.id))
        retry_metadata = _stamp_checkpoint_retry_metadata_safe(
            task=task,
            retry_metadata=retry_metadata,
            workdir=workdir,
            reason=reason,
        )

        # Title and description are passed through verbatim (no prefix
        # mutation).  The retry agent sees the reason via meta_messages.
        task_body: dict[str, Any] = {
            "title": task.title,
            "description": task.description,
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
            "retry_count": retry_count + 1,
            "max_retries": task.max_retries,
            "retry_delay_s": task.retry_delay_s,
            # Carry forward the explicit max_turns override (if any) so the
            # retry spawn doesn't silently fall back to complexity-based
            # auto-computation in compute_max_turns().
            "max_turns": task.max_turns,
        }
        logger.info(
            "retry_or_fail_task: carrying max_turns=%r forward from task %s to retry %d",
            task.max_turns,
            task_id,
            retry_count + 1,
        )
        # Preserve completion signals on retry
        if task.completion_signals:
            task_body["completion_signals"] = [{"type": s.type, "value": s.value} for s in task.completion_signals]
        try:
            client.post(f"{base}/tasks", json=task_body).raise_for_status()
            logger.info(
                "retry_or_fail_task verdict=retry task=%s original_task_id=%s attempt=%d/%d reason=%r",
                task_id,
                _original_task_id,
                retry_count + 1,
                effective_limit,
                reason,
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to re-create task %s for retry: %s", task_id, exc)
            # Fall through to permanent fail (DLQ-eligible: re-create failure
            # is effectively an exhausted retry - the task will not run again).
            _enqueue_dlq_if_workdir(
                workdir=workdir,
                task=task,
                retry_count=retry_count,
                reason=f"retry_recreate_failed: {exc}",
                original_error=reason,
            )
            fail_task(client, base, task_id, f"Max retries exceeded: {reason}")
            return
        # Fail the old task silently (it has been replaced)
        with contextlib.suppress(httpx.HTTPError):
            fail_task(client, base, task_id, f"Retried: {reason}")
    else:
        # retry budget exhausted - move to Dead Letter Queue
        # before marking the task failed so permanently-failed work is not
        # silently dropped.
        logger.info(
            "retry_or_fail_task verdict=permanent_fail task=%s original_task_id=%s attempt=%d/%d reason=%r",
            task_id,
            _original_task_id,
            retry_count + 1,
            effective_limit,
            reason,
        )
        _enqueue_dlq_if_workdir(
            workdir=workdir,
            task=task,
            retry_count=retry_count,
            reason="max_retries_exceeded",
            original_error=reason,
        )
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
        _workdir: Repository root for coupling analysis (part of interface).
        force_parallel: If True, enable decomposition logic.

    Returns:
        True when force_parallel is set AND the task meets scope/retry criteria.
    """
    _ = workdir  # Part of interface; reserved for coupling analysis
    if not force_parallel:
        return False

    if task.id in decomposed_task_ids:
        return False

    if task.title.startswith("[DECOMPOSE]"):
        return False

    # use the typed retry counter (source of truth), falling back
    # to a legacy ``[RETRY N]`` title prefix only when the typed field is 0
    # (so in-flight pre-migration tasks still decompose correctly).
    import re

    from bernstein.core.tasks.models import Scope as _Scope

    retry_count = task.retry_count
    if retry_count == 0:
        retry_match = re.match(r"^\[RETRY\s+(\d+)\]", task.title)
        if retry_match is not None:
            retry_count = int(retry_match.group(1))

    # Decompose if LARGE scope or 2+ retries
    return task.scope == _Scope.LARGE or retry_count >= 2


def create_conflict_resolution_task(
    conflicting_task: Task,
    conflicting_files: list[str],
    *,
    client: httpx.Client,
    server_url: str,
    session_id: str | None,
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
        f"`{session_id}` (task: {conflicting_task.id} - {conflicting_task.title!r}).\n\n"
        f"## Conflicting files\n{files_list}\n\n"
        f"## Your job\n"
        f"1. For each conflicting file, read the conflict markers and understand both sides\n"
        f"2. Resolve each conflict - preserve intent from both sides where possible\n"
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
                with contextlib.suppress(Exception):
                    _seed = parse_seed(_seed_path)
                    _provider = _seed.internal_llm_provider
                    _model = _seed.internal_llm_model

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


def _await_pre_spawn_approvals(
    orch: Any,
    batch: list[Task],
    server_url: str,
    result: Any,
) -> bool:
    """Block until every task with an :class:`ApprovalSpec` is resolved.

    Implements the human-in-the-loop pre-spawn gate (#1110): each task
    that carries an ``approval_spec`` is run through
    :func:`bernstein.core.orchestration.approval_gate.wait_for_approval`,
    which writes a sentinel and emits HMAC-chained audit events. On
    rejection or reject-style timeout the entire batch is failed on the
    server and the spawn is skipped - partial spawns would defeat the
    point of the gate (denied tasks ride along with approved siblings).

    Args:
        orch: Orchestrator instance (provides ``_workdir`` and
            ``_client``).
        batch: Tasks scheduled to share an agent. All must be cleared
            before any can spawn.
        server_url: Base URL of the task server (for ``fail_task``).
        result: Tick result accumulator; rejected tasks are appended to
            ``result.errors`` with a ``approval-gate:`` prefix.

    Returns:
        ``True`` when the caller must skip the spawn (rejection or
        timeout-with-default-action=reject), ``False`` when the entire
        batch was approved and the body may run.
    """
    from bernstein.core.orchestration.approval_gate import wait_for_approval

    workdir = getattr(orch, "_workdir", None)
    if workdir is None:
        # Without a workdir we cannot persist the sentinel; treat as
        # rejected so we never silently bypass the gate.
        for task in batch:
            with contextlib.suppress(Exception):
                fail_task(orch._client, server_url, task.id, "approval-gate: no workdir")
        return True

    rejected_ids: list[str] = []
    for task in batch:
        spec = task.approval_spec
        if spec is None:
            continue
        try:
            outcome = wait_for_approval(task.id, spec, workdir=workdir)
        except Exception as exc:
            logger.exception("approval gate: wait_for_approval crashed for %s", task.id)
            outcome = "rejected"
            with contextlib.suppress(Exception):
                fail_task(orch._client, server_url, task.id, f"approval-gate crash: {exc}")
            result.errors.append(f"approval-gate:{task.id}: {exc}")
            rejected_ids.append(task.id)
            continue
        if outcome == "approved":
            logger.info("approval gate: task %s approved -- proceeding to spawn", task.id)
            continue
        # outcome is "rejected" or "timeout"; the gate has already
        # written a decision file mirroring the resolution.
        rejected_ids.append(task.id)
        reason = f"approval-gate {outcome} (default_action={spec.default_action})"
        with contextlib.suppress(Exception):
            fail_task(orch._client, server_url, task.id, reason)
        result.errors.append(f"approval-gate:{task.id}: {outcome}")
        logger.warning("approval gate: task %s -> %s; skipping spawn", task.id, outcome)
    return bool(rejected_ids)


def _pre_spawn_checks_pass(orch: Any, alive_count: int) -> bool:
    """Run pre-spawn guard checks; return False if spawning should be skipped."""
    if getattr(orch, "is_shutting_down", bool)():
        logger.debug("Skipping claim/spawn: orchestrator is shutting down")
        return False

    _adapter = getattr(getattr(orch, "_spawner", None), "_adapter", None)
    if _adapter is not None and _adapter.is_rate_limited():
        logger.warning("Provider rate-limited - skipping all spawns this tick")
        return False

    _cg = getattr(orch, "_convergence_guard", None)
    if _cg is None:
        return True

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
        logger.warning("Convergence guard blocking spawn wave: %s", "; ".join(_cg_status.reasons))
        return False
    return True


def _apply_fair_scheduling(orch: Any, batches: list[list[Task]]) -> list[list[Task]]:
    """Re-order batches using the weighted fair scheduler.

    Feeds one representative task per batch into a :class:`FairScheduler`
    keyed by ``task.tenant_id``.  The scheduler emits a deficit-round-robin
    sequence of tenants which is used to reorder the input batches so that
    tenants with higher weights receive proportionally more spawn slots.

    Batches missing ``tenant_id`` default to ``"default"``.  When every batch
    belongs to a single tenant, the input ordering is returned unchanged.

    The scheduler instance is cached on the orchestrator (``_fair_scheduler``)
    so deficit state persists across ticks.  Tenants are auto-registered
    with unit weight the first time they appear.

    Args:
        orch: Orchestrator instance; used as a handle for the cached scheduler.
        batches: Batches produced by :func:`group_by_role`.

    Returns:
        Batches re-ordered by tenant fair-share.  Never mutates the input.
    """
    if not batches:
        return batches

    # Early-out for the common single-tenant case - reordering is a no-op.
    tenant_ids = {(b[0].tenant_id if b and getattr(b[0], "tenant_id", None) else "default") for b in batches}
    if len(tenant_ids) <= 1:
        return batches

    from bernstein.core.tasks.fair_scheduler import FairScheduler

    scheduler = getattr(orch, "_fair_scheduler", None)
    if scheduler is None:
        scheduler = FairScheduler()
        orch._fair_scheduler = scheduler

    # Register tenants with default weight if unseen.
    for tid in tenant_ids:
        scheduler.register_tenant(tid)

    # Enqueue one synthetic task per batch. We use the batch index as the
    # scheduler-side task id so the emitted sequence maps back to batches.
    batch_by_key: dict[str, list[Task]] = {}
    for idx, batch in enumerate(batches):
        if not batch:
            continue
        key = f"_fs_batch_{idx}"
        tenant = batch[0].tenant_id or "default"
        scheduler.enqueue(key, tenant, priority=batch[0].priority)
        batch_by_key[key] = batch

    ordered: list[list[Task]] = []
    seen: set[str] = set()
    while True:
        decision = scheduler.dequeue()
        if decision is None:
            break
        seen.add(decision.task_id)
        batch = batch_by_key.get(decision.task_id)
        if batch is not None:
            ordered.append(batch)

    # Append any unscheduled batches (empty or not tracked) in original order.
    for key, batch in batch_by_key.items():
        if key not in seen:
            ordered.append(batch)

    logger.debug(
        "fair_scheduling: reordered %d batches across %d tenants",
        len(ordered),
        len(tenant_ids),
    )
    return ordered


def _refetch_task_for_claim_conflict(client: httpx.Client, base: str, task_id: str) -> Task | None:
    """Re-GET a single task after a claim 409 to discover its current state.

    Returns ``None`` if the task no longer exists (404) or the re-fetch
    itself fails -- callers must treat that as "stop claiming, move on"
    rather than retry against data we no longer trust.
    """
    try:
        resp = client.get(f"{base}/tasks/{task_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return Task.from_dict(resp.json())
    except httpx.HTTPError as exc:
        logger.warning(
            "claim-conflict re-GET of task %s failed: %s -- treating as unclaimable this attempt",
            task_id,
            exc,
        )
        return None
    except (KeyError, ValueError, TypeError) as exc:
        # A 2xx response whose body is missing/malformed (not a well-formed
        # task dict) is a re-fetch failure just like an HTTP error: we no
        # longer trust the data, so stop claiming and move on rather than
        # crashing the whole tick.
        logger.warning(
            "claim-conflict re-GET of task %s returned an unparseable body: %s -- treating as unclaimable this attempt",
            task_id,
            exc,
        )
        return None


def _claim_task_with_conflict_retry(
    orch: Any,
    task: Task,
    base: str,
    session_id: str | None,
) -> tuple[httpx.Response | None, str | None]:
    """POST ``/tasks/{id}/claim``, recovering from stale-version 409s instead of looping forever.

    Root cause (Bug 1, 2026-07-02, evidence in
    ``work/bernstein/proofs/d2/claim-loop-evidence``): the original call site
    sent one CAS claim attempt and, on 409, gave up for the current tick --
    but ``batches`` is recomputed fresh every tick from ``fetch_all_tasks``,
    and if the task's server-side state never actually changes (e.g. a
    dependency check or a stale file-ownership lock from a dead agent keeps
    failing the claim for a reason that has nothing to do with the version),
    the exact same stale ``expected_version`` gets resubmitted every tick,
    forever. Server log evidence: 144 consecutive identical
    ``POST /tasks/109ba3616f03/claim?expected_version=1`` -> 409 responses
    from one session against one task across an entire run, with the worker
    for that task never spawning.

    On every 409 this now:
      1. Re-GETs the task to discover its CURRENT version/status.
      2. If the task is gone (404) or has reached a terminal status
         (done/closed/failed/cancelled/abandoned) -> stop; there is nothing
         left to claim.
      3. If another session now holds a non-open claim on it -> stop and
         move on; retrying cannot help.
      4. Otherwise (still OPEN) -> retry with the freshly observed version,
         whether or not it actually moved -- a same-version 409 means some
         OTHER precondition (role mismatch, unmet dependency, file-ownership
         overlap) is blocking the claim, and that also deserves a few
         bounded retries rather than an infinite tight loop.
    Retries within one call are capped at ``_CLAIM_CONFLICT_MAX_ATTEMPTS``.

    Every attempt is logged at INFO with task_id, version sent, response
    code, the current version/status discovered on re-fetch, and the
    decision taken -- so a repeat of this bug shows up as readable log
    lines instead of a silent multi-hundred-line loop.

    Returns:
        ``(response, None)`` on a successful (non-409) response -- caller
        must still check ``response.status_code`` for 404/5xx as before.
        ``(last_response, reason)`` when claiming this task was abandoned.
    """
    attempts = 0
    current_task = task
    resp: httpx.Response | None = None
    while True:
        attempts += 1
        params: dict[str, Any] = {"expected_version": current_task.version}
        if session_id is not None:
            params["claimed_by_session"] = session_id
        resp = orch._client.post(f"{base}/tasks/{current_task.id}/claim", params=params)
        logger.info(
            "claim attempt %d/%d task=%s sent_version=%s -> HTTP %d",
            attempts,
            _CLAIM_CONFLICT_MAX_ATTEMPTS,
            current_task.id,
            current_task.version,
            resp.status_code,
        )
        if resp.status_code != 409:
            if attempts > 1:
                logger.info(
                    "claim conflict for task %s resolved after %d attempt(s)",
                    current_task.id,
                    attempts,
                )
            return resp, None

        if attempts >= _CLAIM_CONFLICT_MAX_ATTEMPTS:
            logger.warning(
                "claim conflict cap reached for task %s after %d attempts (last sent version=%s, "
                "detail=%r) -- giving up on this task this episode",
                current_task.id,
                attempts,
                current_task.version,
                resp.text[:300] if resp is not None else None,
            )
            return resp, f"CAS conflict, gave up after {attempts} attempts"

        refreshed = _refetch_task_for_claim_conflict(orch._client, base, current_task.id)
        if refreshed is None:
            logger.info(
                "claim conflict for task %s: re-GET found task gone (404 or fetch error) -- stopping, nothing to claim",
                current_task.id,
            )
            return resp, "task no longer exists or unfetchable"

        if refreshed.status in _CLAIM_TERMINAL_STATUSES:
            logger.info(
                "claim conflict for task %s: re-fetch shows terminal status=%s -- stopping, work is done",
                current_task.id,
                refreshed.status.value,
            )
            return resp, f"task reached terminal status {refreshed.status.value}"

        if refreshed.status != TaskStatus.OPEN:
            other_session = refreshed.claimed_by_session
            if other_session is not None and other_session != session_id:
                logger.info(
                    "claim conflict for task %s: now held by a different session %s (status=%s) -- "
                    "moving on, not retrying",
                    current_task.id,
                    other_session,
                    refreshed.status.value,
                )
                return resp, f"claimed by another session {other_session}"
            logger.info(
                "claim conflict for task %s: status=%s (not open, no foreign session to blame) -- "
                "stopping this episode",
                current_task.id,
                refreshed.status.value,
            )
            return resp, f"task not open (status={refreshed.status.value})"

        # Still OPEN. Whether or not the version actually moved, retry with
        # the freshest known version rather than resubmitting stale data.
        logger.info(
            "claim conflict for task %s: re-fetch shows status=OPEN, version %s -> %s -- retrying (attempt %d/%d)",
            current_task.id,
            current_task.version,
            refreshed.version,
            attempts + 1,
            _CLAIM_CONFLICT_MAX_ATTEMPTS,
        )
        current_task = refreshed
        task.version = refreshed.version  # keep caller's Task object in sync
        time.sleep(min(0.05 * (2 ** (attempts - 1)), 0.5))


def _claim_conflict_backoff_active(orch: Any, task_id: str) -> bool:
    """True if ``task_id`` is still within its cross-tick claim-conflict backoff window."""
    state: dict[str, tuple[int, float]] = getattr(orch, "_claim_conflict_state", None) or {}
    _episode_count, backoff_until = state.get(task_id, (0, 0.0))
    return time.time() < backoff_until


def _record_claim_conflict_episode(orch: Any, task_id: str) -> None:
    """Record one exhausted claim-conflict episode and set the next backoff window."""
    if not hasattr(orch, "_claim_conflict_state") or not isinstance(orch._claim_conflict_state, dict):
        orch._claim_conflict_state = {}
    episode_count, _ = orch._claim_conflict_state.get(task_id, (0, 0.0))
    episode_count += 1
    backoff_s = min(
        _CLAIM_CONFLICT_BACKOFF_BASE_S * (2 ** (episode_count - 1)),
        _CLAIM_CONFLICT_BACKOFF_MAX_S,
    )
    backoff_until = time.time() + backoff_s
    orch._claim_conflict_state[task_id] = (episode_count, backoff_until)
    logger.warning(
        "claim-conflict episode %d for task %s -- backing off %.1fs before the next attempt",
        episode_count,
        task_id,
        backoff_s,
    )


def _clear_claim_conflict_state(orch: Any, task_id: str) -> None:
    """Drop claim-conflict bookkeeping for a task once it claims successfully."""
    state = getattr(orch, "_claim_conflict_state", None)
    if isinstance(state, dict):
        state.pop(task_id, None)


def _lineage_id(task: Task) -> str:
    """Return the task's lineage id: ``metadata["original_task_id"]`` or its id.

    Every retry created by ``maybe_retry_task`` / ``retry_or_fail_task`` stamps
    ``metadata["original_task_id"]`` with the lineage root, so grouping by this
    value keeps a task and all its retries under one stable key even though each
    retry has a fresh task id.
    """
    metadata = task.metadata
    if isinstance(metadata, dict):
        original = metadata.get("original_task_id")
        if isinstance(original, str) and original:
            return original
    return task.id


def _batch_lineage_key(batch: list[Task]) -> frozenset[str]:
    """Lineage-stable spawn-backoff key for a batch (issue #2806).

    Keyed on each task's lineage id rather than its ephemeral task id so the
    spawn-failure counter and exponential backoff accumulate across retries
    instead of resetting every time a retry mints a new id.
    """
    return frozenset(_lineage_id(t) for t in batch)


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
        _done_ids: IDs of already-completed tasks (part of interface).
        result: TickResult accumulator for spawned/error lists.
    """
    _ = done_ids  # Part of interface; used for overlap detection by callers
    if not _pre_spawn_checks_pass(orch, alive_count):
        return

    # Fair scheduling: when enabled, re-order batches using
    # weighted deficit round-robin across tenants so multi-tenant workloads
    # get proportional service instead of FIFO starvation.  Runs before
    # the HTTP /claim calls below. Default-off via ``fair_scheduling_enabled``.
    if getattr(orch._config, "fair_scheduling_enabled", False):
        batches = _apply_fair_scheduling(orch, batches)

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
    # Count open task batches per role - direct cap prevents spawning more agents
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
        # Avoid in-place .sort(): batches is caller-owned. Rebind locally.
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
            _claimed_titles.update(agent.task_ids)

    for batch in batches:
        if getattr(orch, "is_shutting_down", bool)():
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
        # Check the semantic cache for a verified result - if found, complete the
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
                        "Cache hit for task '%s' (sim=%.2f) - skipping agent spawn",
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
        _ownership_sessions = orch._agents | (_batch_sessions if isinstance(_batch_sessions, dict) else {})
        if check_file_overlap(batch, orch._file_ownership, _ownership_sessions):
            continue

        # Skip if inferred paths overlap with files actively being edited
        # in other agents' worktrees (hot-file detection - CRITICAL-007).
        _active_files = _get_active_agent_files(orch)
        if _active_files:
            _batch_inferred: set[str] = set()
            for _t in batch:
                _batch_inferred |= infer_affected_paths(_t)
            _overlap = _batch_inferred & _active_files
            if _overlap:
                logger.info(
                    "Skipping batch - file overlap with active agent worktree: %s",
                    _overlap,
                )
                continue

        # Check spawn backoff: skip batches that recently failed.
        # Key on the task *lineage* (metadata["original_task_id"], falling back
        # to the task id) rather than the current attempt's ids: a retry mints a
        # brand-new task id, so an id-keyed backoff resets fail_count to 0 on
        # every attempt and the _MAX_SPAWN_FAILURES ceiling never accumulates
        # against a repeating spawn failure (issue #2806). Keying on the lineage
        # makes the consecutive-failure counter and exponential backoff
        # accumulate across retries so a structurally-dead spawn fails fast.
        batch_key = _batch_lineage_key(batch)
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
        # Respects auto_decompose config - disabled by default.
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

        # Pre-spawn human-in-the-loop approval gate (#1110). When any task
        # in this batch carries an ``approval_spec``, block until the
        # operator decides via ``bernstein approve <id>`` / ``reject``,
        # or the spec timeout fires. On rejection / reject-style timeout
        # we mark every task in the batch failed and skip the spawn so
        # the agent body never runs without explicit consent.
        if any(t.approval_spec is not None for t in batch) and _await_pre_spawn_approvals(orch, batch, base, result):
            continue

        # WAL: record pre-execution intent BEFORE the HTTP POST /claim so a
        # SIGKILL between the server-side claim transition and the local WAL
        # write can never produce a server-side "claimed" task with no WAL
        # trace. The legacy ``task_claimed`` decision_type is
        # reused so existing recovery wiring (``find_orphaned_claims``)
        # continues to force-claim abandoned tasks back to the open queue.
        # Worktree path is not yet known -- it is recorded in the follow-up
        # ``claim_confirmed`` entry after the spawner materialises it.
        _wal: WALWriter | None = getattr(orch, "_wal_writer", None)
        if _wal is not None:
            for task in batch:
                try:
                    _wal.write_entry(
                        decision_type="task_claimed",
                        inputs={"task_id": task.id, "role": task.role, "title": task.title},
                        output={"batch_size": len(batch), "phase": "claim_intent"},
                        actor="task_lifecycle",
                        committed=False,
                    )
                except OSError:
                    logger.debug("WAL write failed for task_claimed %s", task.id)

        # Claim tasks BEFORE spawning to prevent duplicate agents.
        # Pass expected_version for CAS (compare-and-swap) to prevent two
        # distributed nodes from claiming the same task simultaneously.
        # Abort on server errors (5xx), CAS conflicts (409, with bounded
        # re-fetch-and-retry -- see _claim_task_with_conflict_retry), or
        # transport failures.
        #
        # Bug 1b (2026-07-02, claim-then-never-spawn deadlock): a
        # multi-task batch used to be all-or-nothing -- if task N claimed
        # successfully but a LATER task in the same batch failed to claim,
        # the whole batch aborted via `continue`, leaving the already
        # server-side-claimed earlier task(s) claimed with no agent spawned
        # and no failure recorded. Evidence
        # (work/bernstein/proofs/d2/claude/attempt4-meridian-fixed/FAIL-NOTE.md):
        # duplicate-titled task pairs (created by an unrelated upstream
        # double-execution bug) meant one twin claimed fine while its
        # sibling's claim was rejected by the file-ownership-overlap check
        # (surfaced generically as a 409, misread as a version race); the
        # claimed twin then sat blocking the dependency graph for the full
        # 15-minute stale-claim-reaper window with `agents=0 spawned=0`.
        # Fix: track which tasks actually claimed and shrink ``batch`` to
        # that subset before spawning, instead of discarding the whole
        # batch -- a claimed task with no path to a worker is a worse
        # failure mode than a partially-sized agent batch. Tasks that never
        # claimed stay open and are retried (bounded, backed off) on a
        # later tick via the same claim-conflict machinery above.
        claim_failed = False
        claimed_tasks: list[Task] = []
        _orch_session_id: str | None = getattr(orch, "session_id", None)
        for task in batch:
            if _claim_conflict_backoff_active(orch, task.id):
                logger.debug(
                    "Skipping claim for task %s: still within claim-conflict backoff window",
                    task.id,
                )
                result.errors.append(f"claim:{task.id}: in claim-conflict backoff")
                claim_failed = True
                break
            try:
                resp, conflict_reason = _claim_task_with_conflict_retry(orch, task, base, _orch_session_id)
                if conflict_reason is not None:
                    _record_claim_conflict_episode(orch, task.id)
                    result.errors.append(f"claim:{task.id}: {conflict_reason}")
                    claim_failed = True
                    break
                assert resp is not None  # conflict_reason is None only on a real response
                if resp.status_code >= 500:
                    logger.error(
                        "Server error %d claiming task %s -- aborting spawn",
                        resp.status_code,
                        task.id,
                    )
                    result.errors.append(f"claim:{task.id}: server error {resp.status_code}")
                    claim_failed = True
                    break
                _clear_claim_conflict_state(orch, task.id)
                claimed_tasks.append(task)
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
            if not claimed_tasks:
                continue
            logger.warning(
                "Partial batch-claim failure: %d/%d task(s) claimed before the failure "
                "(%s) -- spawning for the claimed subset %s instead of leaving them "
                "claimed with no agent (claim-then-never-spawn deadlock)",
                len(claimed_tasks),
                len(batch),
                result.errors[-1] if result.errors else "unknown reason",
                [t.id for t in claimed_tasks],
            )
            batch = claimed_tasks

        # Response cache: if a functionally identical task was already completed,
        # return the cached result without spawning an agent (20-40% savings target).
        # Only applied to single-task batches - multi-task batches have complex
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
                try:
                    l1_cfg = get_l1_model_config()
                except ModelNotConfiguredError:
                    # fast_path.l1_model is not configured: skip the L1
                    # downgrade and let standard routing apply the
                    # operator-configured default_model (or refuse with a
                    # clear error if none is configured anywhere).
                    logger.info(
                        "Task %s classified L1 but fast_path.l1_model is not configured - skipping L1 downgrade",
                        batch[0].id,
                    )
                    l1_cfg = None
                if l1_cfg is not None:
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
        # The capability-gated route_batch decision (#2354, AC3) is the gate:
        # a batch-eligible task reaches the batch surface only on a
        # batch-capable adapter, a non-eligible task never does, and a
        # batch-eligible task on an adapter with no batch surface is refused
        # (dispatched interactively) rather than faked. The routing decision is
        # sealed as a cost.batch_route receipt.
        if len(batch) == 1:
            _batch_api = getattr(orch, "_batch_api", None)
            if _batch_api is not None:
                from bernstein.core.cost.scheduling.live_dispatch import (
                    decide_batch_route,
                    seal_batch_route,
                )

                _route = decide_batch_route(orch, batch[0])
                seal_batch_route(orch, _route)
                if _route.route == "batch":
                    _batch_result = _batch_api.try_submit(orch, batch[0])
                    if _batch_result.handled:
                        if _batch_result.submitted:
                            assigned_task_ids.add(batch[0].id)
                            _claimed_titles.add(_base_title(batch[0].title))
                            result.spawned.append(_batch_result.session_id or f"provider-batch:{batch[0].id}")
                        elif _batch_result.reason:
                            result.errors.append(f"batch:{batch[0].id}: {_batch_result.reason}")
                        continue
                elif _route.refused_reason:
                    logger.debug(
                        "cost: task %s batch-eligible but adapter %s has no batch surface (%s); "
                        "dispatching interactively",
                        batch[0].id,
                        _route.adapter,
                        _route.refused_reason,
                    )

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
                    # discarded - spawn a new one with the correct model override).
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
                    # This task is assigned to the primary model - record it
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
            # WAL: record the worktree materialisation.
            # This intermediate ``claim_confirmed`` entry ties every claimed
            # task_id to its concrete worktree_path BEFORE the final commit.
            # A SIGKILL between here and ``task_spawn_confirmed`` leaves an
            # uncommitted ``claim_confirmed`` the recovery path can use to
            # preserve the worktree directory and /fail the task back to the
            # open queue, instead of silently reaping the work.
            _worktree_path: Any = None
            try:
                _worktree_path = orch._spawner.get_worktree_path(session.id)
            except Exception:
                logger.debug("Could not resolve worktree_path for session %s", session.id)
            if _wal is not None:
                for _t in batch:
                    try:
                        _wal.write_entry(
                            decision_type="claim_confirmed",
                            inputs={
                                "task_id": _t.id,
                                "agent_id": session.id,
                                "worktree_path": str(_worktree_path) if _worktree_path else "",
                            },
                            output={"role": session.role, "phase": "worktree_created"},
                            actor="task_lifecycle",
                            committed=False,
                        )
                    except OSError:
                        logger.debug("WAL write failed for claim_confirmed %s", _t.id)
            # WAL: commit the claim - agent was successfully spawned.
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
                # Transient failure - release claimed tasks immediately so they
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


def _fail_verification(result: Any, task_id: str, failed: list[str]) -> None:
    """Mark a task as failed verification in the tick result."""
    with contextlib.suppress(ValueError):
        result.verified.remove(task_id)
    result.verification_failures.append((task_id, failed))


def _run_quality_gates(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
) -> tuple[bool, Any]:
    """Run quality gate checks. Returns (passed, qg_result)."""
    qg_config = getattr(orch, "_quality_gate_config", None)
    if qg_config is None:
        return True, None

    worktree = orch._spawner.get_worktree_path(session.id)
    gate_run_dir = worktree if worktree is not None else orch._workdir
    qg_result = orch._gate_coalescer.run(task, gate_run_dir, orch._workdir, qg_config)
    if not qg_result.passed:
        failed = [f"quality_gate:{r.gate}" for r in qg_result.gate_results if r.blocked and not r.passed]
        _fail_verification(result, task.id, failed)
        logger.info("Quality gates blocked merge for task %s: %s", task.id, ", ".join(failed))
        return False, qg_result
    return True, qg_result


def _run_rule_enforcement(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
) -> bool:
    """Run organizational rule enforcement. Returns True if passed."""
    rules_config: RulesConfig | None = load_rules_config(orch._workdir)
    if rules_config is None:
        return True

    worktree = orch._spawner.get_worktree_path(session.id)
    run_dir = worktree if worktree is not None else orch._workdir
    re_result = run_rule_enforcement(task, run_dir, orch._workdir, rules_config)
    if not re_result.passed:
        failed = [f"rule:{v.rule_id}: {v.fix_hint}" for v in re_result.violations if v.blocked]
        _fail_verification(result, task.id, failed)
        logger.info("Rule enforcement blocked merge for task %s: %s", task.id, ", ".join(failed))
        return False
    return True


def _run_verification_gates(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
    janitor_passed: bool,
) -> tuple[bool, Any]:
    """Run quality gates, rule enforcement, cross-model, and formal verification.

    Returns updated (janitor_passed, qg_result) tuple.
    """
    qg_result: Any = None

    if janitor_passed:
        janitor_passed, qg_result = _run_quality_gates(orch, task, session, result)

    if janitor_passed:
        janitor_passed = _run_rule_enforcement(orch, task, session, result)

    if janitor_passed:
        janitor_passed = _run_cross_model_check(orch, task, session, result)

    if janitor_passed:
        janitor_passed = _run_formal_verification_gate(orch, task, session, result)

    return janitor_passed, qg_result


def _run_formal_verification_gate(
    orch: Any,
    task: Task,
    session: AgentSession,
    result: Any,
) -> bool:
    """Run the Z3/Lean4 formal-verification gate when configured.

    Gated behind the ``formal_verification_enabled`` flag on
    :class:`OrchestratorConfig` (default ``False``) so deployments without
    Z3/Lean4 installed are never impacted.  When the flag is on *and*
    ``orch._formal_verification_config`` is populated, each configured
    property is checked.  Violations block merge when the config has
    ``block_on_violation`` set (the default).

    Args:
        orch: Orchestrator instance (duck-typed).
        task: The completed task being verified.
        session: Agent session that produced the task.
        result: Tick result accumulator used to record verification failures.

    Returns:
        True when the gate passes or is skipped, False when a violation
        blocks merge.
    """
    if not getattr(orch._config, "formal_verification_enabled", False):
        return True

    fv_config = getattr(orch, "_formal_verification_config", None)
    if fv_config is None:
        return True

    # Extract janitor-derived completion context if a log aggregator is present.
    try:
        summary = AgentLogAggregator(orch._workdir).parse_log(session.id)
        files_modified_count = len(summary.files_modified)
    except Exception as exc:
        logger.debug("formal_verification: could not read agent log for %s: %s", session.id, exc)
        files_modified_count = 0

    test_passed = True  # Preceding gates already validated test outcomes.

    try:
        from bernstein.core.quality.formal_verification import run_formal_verification

        fv_result = run_formal_verification(
            task,
            orch._workdir,
            fv_config,
            files_modified=files_modified_count,
            test_passed=test_passed,
        )
    except Exception as exc:
        logger.warning("formal_verification: gate raised unexpectedly for task %s: %s", task.id, exc)
        return True  # Never break tick pipeline on gateway bugs.

    if fv_result.skipped or fv_result.passed:
        return True

    if not getattr(fv_config, "block_on_violation", True):
        logger.info(
            "formal_verification: %d violation(s) for task %s (non-blocking): %s",
            len(fv_result.violations),
            task.id,
            ", ".join(v.property_name for v in fv_result.violations),
        )
        return True

    failed = [f"formal_verification:{v.property_name}: {v.detail}" for v in fv_result.violations]
    _fail_verification(result, task.id, failed)
    logger.info("Formal verification blocked merge for task %s: %s", task.id, ", ".join(failed))
    return False


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

    # Feed the verdict into the context-degradation detector so consecutive
    # rejects can trigger a SHUTDOWN-and-restart in the next tick.
    detector = getattr(orch, "_context_degradation", None)
    if detector is not None:
        try:
            detector.record_verdict(session.id, task.id, verdict)
        except Exception as exc:
            logger.debug("context_degradation: record_verdict failed: %s", exc)

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


def evict_degraded_sessions(orch: Any) -> list[str]:
    """Checkpoint degraded agents, store recovery context, and send SHUTDOWN.

    Called from the orchestrator tick.  For every session the context-
    degradation detector has flagged:

    1. :meth:`ContextDegradationDetector.checkpoint` snapshots progress to
       disk and produces a markdown recovery-context block.
    2. The block is stashed on :attr:`orch._context_recovery` keyed by every
       task id owned by the terminating session so the replacement agent's
       prompt can pick it up.
    3. A SHUTDOWN signal is written via :attr:`orch._signal_mgr` so the
       agent exits cleanly at its next heartbeat.
    4. Detector state for the session is cleared.

    Args:
        orch: Orchestrator instance (duck-typed).

    Returns:
        List of session ids that were evicted this tick.  Empty when the
        detector is disabled or no session is flagged.
    """
    detector = getattr(orch, "_context_degradation", None)
    if detector is None:
        return []

    evicted: list[str] = []
    for session_id in detector.degraded_sessions():
        session = orch._agents.get(session_id)
        if session is None or getattr(session, "status", None) == "dead":
            # Agent already gone - just flush tracking state so memory doesn't leak.
            detector.clear(session_id)
            continue

        try:
            checkpoint = detector.checkpoint(session)
        except Exception as exc:
            logger.warning("context_degradation: checkpoint failed for %s: %s", session_id, exc)
            detector.clear(session_id)
            continue

        recovery_store: dict[str, str] | None = getattr(orch, "_context_recovery", None)
        if recovery_store is not None:
            for tid in checkpoint.task_ids:
                recovery_store[tid] = checkpoint.recovery_context

        signal_mgr = getattr(orch, "_signal_mgr", None)
        if signal_mgr is not None:
            task_title = ", ".join(checkpoint.task_ids) if checkpoint.task_ids else "unknown"
            try:
                signal_mgr.write_shutdown(
                    session_id,
                    reason="context_degradation",
                    task_title=task_title,
                )
            except OSError as exc:
                logger.warning("context_degradation: SHUTDOWN write failed for %s: %s", session_id, exc)

        logger.warning(
            "context_degradation: evicted session %s (rejects=%d, verdicts=%d, tasks=%s)",
            session_id,
            checkpoint.consecutive_rejects,
            checkpoint.verdict_count,
            checkpoint.task_ids,
        )
        evicted.append(session_id)
        detector.clear(session_id)

    return evicted


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
        logger.exception(
            "approval_decision: task=%s session=%s decision=held FAIL-CLOSED (outer exception) -- "
            "exception raised in approval-gate evaluation flow (gate check or pre-gate step such as "
            "_resolve_approval_workflow); holding for approval, NOT auto-merging",
            task.id,
            session.id,
        )
        return True
    return False


def _resolve_approval_workflow(orch: Any, task: Task) -> tuple[Any, float | None]:
    """Resolve approval mode and timeout from workflow config."""
    wf = getattr(orch._config, "approval_workflow", None)
    if wf is None or not wf.enabled:
        return None, None

    risk = getattr(task, "risk_level", "low")
    mode_str = (
        {
            "low": wf.low_risk,
            "medium": wf.medium_risk,
            "high": wf.high_risk,
            "critical": getattr(wf, "critical_risk", wf.high_risk),
        }
    ).get(risk, "auto")

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

    task_m = get_collector(orch._workdir / ".sdd" / "metrics").task_metrics.get(task.id)
    cost_usd = task_m.cost_usd if task_m else 0.0
    test_summary = (
        (completion_data or {"files_modified": [], "test_results": {}}).get("test_results", {}).get("summary", "")
    )
    pr_url = orch._approval_gate.create_pr(
        task,
        worktree_path=worktree_path,
        session_id=session.id,
        labels=orch._config.pr_labels,
        _role=session.role,
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
    _completion_data: CompletionData | None,
    cache_diff_lines: int,
) -> tuple[bool, int, bool]:
    """Reap agent, handle merge, cleanup worktree.

    Returns ``(cache_verified, cache_diff_lines, merge_failed)`` where
    ``merge_failed`` is True only when a merge-back was attempted and failed
    for a non-conflict reason (issue #2792). The caller routes that case
    through the bounded reopen/permanent-fail budget instead of leaving the
    task on the open queue for uncapped retry.
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
        # issue #2362 (AC1): seal a verification-evidence bundle for the task
        # now that its changes are merged, before the worktree is reclaimed.
        # No-op when the task declares no producers; fail-open otherwise so a
        # producer/gate error can never block, delay, or fail the completion.
        seal_evidence_on_completion(orch._workdir, task)
        # issue #2365: chain the merge decision into the run journal so the
        # review board's merged column is a projection of the journal, not a
        # side inference. No-op when the orchestrator has no recorder.
        record_task_merged(getattr(orch, "_recorder", None), task_id=task.id, agent_id=session.id)

    # issue #2365: capture the task diff as a content-addressed review artifact
    # (the bytes a reviewer inspects on the board) before the worktree is
    # reclaimed, for merged and unmerged tasks alike. Chained into the run
    # journal so the diff identity is a journal fact and the board can serve
    # and verify it against a detached run. Fail-open: never blocks completion.
    _capture_review_diff(orch, task, session)

    # issue #2792: a merge-back that failed for a *non-conflict* reason (an
    # untracked operator-tree file, the forbidden-path guard, unrelated
    # histories, a missing branch) leaves the worker's only committed copy on
    # the agent/<id> branch. ``cleanup_worktree`` force-deletes both the
    # worktree and that branch, so gating it on merge success preserves the
    # committed work for inspection or a resolver. Conflicts are handled by
    # ``_handle_merge_result`` (a resolver task) and a merge that was
    # intentionally skipped (approval-gate PR path) is not a failure; both
    # still clean up as before.
    merge_failed = (
        not skip_merge and merge_result is not None and not merge_result.success and not merge_result.conflicting_files
    )
    if merge_failed:
        assert merge_result is not None  # narrowed above
        logger.error(
            "Merge-back failed for task %s (session %s); preserving worktree and "
            "branch agent/%s so the committed work is not force-deleted: %s",
            task.id,
            session.id,
            session.id,
            merge_result.error or "unknown reason",
        )
    else:
        orch._spawner.cleanup_worktree(session.id)
    return cache_verified, cache_diff_lines, merge_failed


def _capture_review_diff(orch: Any, task: Task, session: AgentSession) -> None:
    """Store the session worktree's diff as a chained review-board artifact.

    Fail-open: any error (no worktree, git failure, no recorder) leaves the
    board without a diff for this task rather than disturbing completion.
    """
    from pathlib import Path

    try:
        get_worktree = getattr(orch._spawner, "get_worktree_path", None)
        if not callable(get_worktree):
            return
        worktree = get_worktree(session.id)
        if worktree is None:
            return
        diff_text = _get_git_diff_text_in_worktree(Path(worktree))
        if not diff_text:
            return
        recorder = getattr(orch, "_recorder", None)
        run_id = getattr(recorder, "run_id", "")
        if recorder is None or not run_id:
            return
        summary = store_task_diff(orch._workdir / ".sdd", run_id, task.id, diff_text)
        if summary is None:
            return
        record_task_diff_captured(recorder, task_id=task.id, summary=summary)
    except Exception as exc:
        logger.debug("review diff capture failed for task %s: %s", task.id, exc)


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
    _result: Any,
    merge_result: MergeResult | None,
    _janitor_passed: bool,
    skip_merge: bool,
) -> bool:
    """Handle merge conflicts and return whether merge succeeded."""
    if merge_result is None or merge_result.success:
        return True
    if skip_merge:
        return False
    if not merge_result.conflicting_files:
        # issue #2792: non-conflict merge-back failure (untracked operator-tree
        # file, forbidden-path guard, unrelated histories, missing branch). The
        # worker's only committed copy is preserved on agent/<id> by the caller;
        # surface the failure loudly so a run does not report a healthy state
        # while work is being held back.
        orch._post_bulletin(
            "alert",
            f"merge-back failed (non-conflict) for task {task.id}: "
            f"{merge_result.error or 'unknown reason'} - worktree/branch preserved for recovery",
        )
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
        f"merge conflict in {len(merge_result.conflicting_files)} files - resolver task created (task {task.id})",
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


def _record_cost_and_convergence(
    orch: Any,
    task: Task,
    session: AgentSession | None,
    task_m: Any,
    cost_usd: float,
    janitor_passed: bool,
    tokens_sidecar_source: str = "",
) -> None:
    """Record cost tracking, convergence, and completion budget.

    ``tokens_sidecar_source`` (item 31, 2026-07-02) rides the ledger mutation
    so the emitted ``ledger_update:`` line records whether these token counts
    came from an alive-exit /complete sidecar ingestion (``alive_exit``) vs a
    dead-session recovery (``dead_exit``) vs the collector metrics (``""``).

    The response-style profile applied at spawn also
    rides the ledger entry (``response_profile`` + ``profile_content_sha256``
    cost tags) so downstream cost analysis can group spend per profile. The
    session carries the authoritative stamp; when the session is already
    gone (dead-exit recovery), the copy stamped on ``task.metadata`` at
    spawn is used instead. Pre-change sessions carry neither, keeping the
    ledger mutation byte-identical for them.
    """
    agent_id = session.id if session else "unknown"
    model = session.model_config.model if session else "unknown"
    tokens_in = task_m.tokens_prompt if task_m else 0
    tokens_out = task_m.tokens_completion if task_m else 0
    cost_tags: dict[str, str] = {}
    if tokens_sidecar_source:
        cost_tags["tokens_sidecar_source"] = tokens_sidecar_source
    response_profile = getattr(session, "response_profile", "") if session else ""
    profile_sha = getattr(session, "profile_content_sha256", "") if session else ""
    if not response_profile and isinstance(task.metadata, dict):
        response_profile = str(task.metadata.get("response_profile") or "")
        profile_sha = str(task.metadata.get("profile_content_sha256") or "")
    if response_profile:
        cost_tags["response_profile"] = response_profile
        cost_tags["profile_content_sha256"] = profile_sha
    orch._cost_tracker.record_cumulative(
        agent_id=agent_id,
        task_id=task.id,
        model=model,
        total_input_tokens=tokens_in,
        total_output_tokens=tokens_out,
        total_cost_usd=cost_usd if cost_usd > 0 else None,
        tenant_id=task.tenant_id,
        cost_tags=cost_tags or None,
    )
    try:
        orch._cost_tracker.save(orch._workdir / ".sdd")
    except OSError as exc:
        logger.warning("Failed to persist cost tracker: %s", exc)

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
    tokens_prompt = task_m.tokens_prompt if task_m else 0
    tokens_completion = task_m.tokens_completion if task_m else 0
    cost_source = "collector.task_metrics"

    # D2 canary-host-99d0eac0 (2026-07-03): ``task_m.cost_usd`` is populated
    # by nothing on the normal-completion path - the live-cost loop
    # (orchestrator._record_live_costs) feeds CostTracker only and never
    # writes back into collector.task_metrics - so normally-completed tasks
    # with REAL spend (e.g. canary qa task 325c200e1985, whose .tokens
    # sidecar carried 51,880/1,401 tokens) recorded ``cost_usd: 0.0`` in
    # ``.sdd/metrics/tasks.jsonl``. The runner writes its priced usage to
    # the orchestrator-root ``.tokens`` sidecar BEFORE its process exits
    # (including on exception paths since the MaxTurnsExceeded fix), so at
    # completion time the sidecar is ground truth for what the session
    # actually spent - reading it here also closes the race where the
    # sidecar lands after the live-cost loop's last tick. Prefer it
    # whenever it knows more than the (typically zero) collector figure.
    # Lazy import: agent_lifecycle imports this module at its top, so a
    # top-level import here would be circular.
    # Bug 14 (D2 minimax attempt-e938bd33, 2026-07-02): when the agent died
    # BEFORE the completion sweep processed its task (MaxTurnsExceeded fires
    # a nonzero exit and the reaper drops the session from _agents /
    # _task_to_session), _find_session_for_task returns None here, the
    # sidecar branch below never ran, and the metrics row recorded $0
    # (task 7bb98dc57345: sidecar carried 54,003/1,026 tokens ~ $0.0116,
    # row said cost_usd=0.0, model=null). The sidecar file itself survives
    # (keyed by agent id at .sdd/runtime/<agent_id>.tokens), and
    # task.assigned_agent still names the dead agent, so reconstruct a
    # minimal session-shaped shim (id + model from the collector's
    # AgentMetrics, recorded at spawn) and read the sidecar anyway.
    sidecar_session: Any = session
    if session is None and getattr(task, "assigned_agent", None):
        agent_id = task.assigned_agent
        agent_m = collector.agent_metrics.get(agent_id) if hasattr(collector, "agent_metrics") else None
        dead_model = getattr(agent_m, "model", "") or ""
        sidecar_session = SimpleNamespace(
            id=agent_id,
            model_config=SimpleNamespace(model=dead_model),
        )
        logger.info(
            "completion_cost_fallback: task_id=%s session gone (agent reaped before "
            "completion sweep) - reading sidecar via task.assigned_agent=%s model=%r",
            task.id,
            agent_id,
            dead_model,
        )
    if sidecar_session is not None:
        from pathlib import Path as _CliPath

        from bernstein.core.agents.agent_lifecycle import _read_runner_cost_usd
        from bernstein.core.cost.cli_adapter_usage import capture_cli_adapter_usage

        # Issue #2797: plain CLI adapters (qwen etc.) write no .tokens sidecar
        # during the run, so recover per-call usage from the adapter's
        # structured session log and materialise the sidecar the recovery
        # below already consumes. No-op when a sidecar already exists
        # (openai_agents / Claude wrapper wrote one) so counts are never
        # double-recorded. Also yields the model/route id for attribution.
        _cli_session_log = getattr(session, "log_path", "") or ""
        _cli_in, _cli_out, _cli_model = capture_cli_adapter_usage(
            orch._workdir,
            str(getattr(sidecar_session, "id", "") or ""),
            _CliPath(_cli_session_log) if _cli_session_log else None,
        )

        sidecar_cost, sidecar_in, sidecar_out = _read_runner_cost_usd(orch._workdir, sidecar_session, task.id)
        if sidecar_cost > cost_usd or (cost_usd <= 0.0 and (sidecar_in > 0 or sidecar_out > 0)):
            cost_usd = sidecar_cost
            tokens_prompt = sidecar_in
            tokens_completion = sidecar_out
            cost_source = "tokens_sidecar" if session is not None else "tokens_sidecar_dead_session"
            if task_m is not None:
                # Reconcile the in-memory record too: retrospective.py's
                # cost-aggregation fallback reads collector._task_metrics
                # directly, and _record_cost_and_convergence below reads
                # task_m.tokens_prompt/tokens_completion for CostTracker.
                task_m.cost_usd = cost_usd
                task_m.tokens_prompt = sidecar_in
                task_m.tokens_completion = sidecar_out
                task_m.tokens_used = sidecar_in + sidecar_out
        # Attribute the model/route where the CLI log knows it but the record
        # does not (dead-session completions record model=None -> "unknown").
        if _cli_model and task_m is not None and not (task_m.model or "").strip():
            task_m.model = _cli_model
    logger.info(
        "completion_cost_source: task_id=%s agent_id=%s source=%s cost_usd=%.6f tokens_prompt=%d tokens_completion=%d",
        task.id,
        session.id if session else getattr(sidecar_session, "id", "none") if sidecar_session else "none",
        cost_source,
        cost_usd,
        tokens_prompt,
        tokens_completion,
    )

    # item 31 (2026-07-02): classify the ledger ingestion origin so the
    # alive-exit /complete path is distinguishable from an orphan/dead-exit
    # recovery in the ledger_update: log line. cost_source is already the
    # authoritative selector chosen above.
    if cost_source == "tokens_sidecar":
        tokens_sidecar_source = "alive_exit"
    elif cost_source == "tokens_sidecar_dead_session":
        tokens_sidecar_source = "dead_exit"
    else:
        tokens_sidecar_source = ""

    _record_cost_and_convergence(orch, task, session, task_m, cost_usd, janitor_passed, tokens_sidecar_source)
    collector.complete_task(
        task.id,
        success=janitor_passed,
        janitor_passed=janitor_passed,
        cost_usd=cost_usd,
        tokens_used=tokens_prompt + tokens_completion,
    )

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
            _model=session.model_config.model,
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
            HookEvent.TASK_COMPLETED.value,
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
            HookEvent.TASK_FAILED.value,
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


def _compute_task_duration(
    session: AgentSession | None,
    task_m: Any,
) -> float:
    """Compute task duration from metrics or session spawn time."""
    if task_m and task_m.end_time:
        return task_m.end_time - task_m.start_time
    if session and session.spawn_ts > 0:
        return time.time() - session.spawn_ts
    return 0.0


def _set_downstream_affinity(
    orch: Any,
    task: Task,
) -> None:
    """Propagate agent affinity to downstream open tasks."""
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
        duration = _compute_task_duration(session, task_m)
        try:
            orch._evolution.record_task_completion(
                task=task,
                duration_seconds=round(duration, 2),
                cost_usd=cost_usd,
                janitor_passed=janitor_passed,
                # Fall back to the record's model when the session is gone
                # (dead-session completions) so a known CLI-adapter route id,
                # recovered from the session log in _record_completion_metrics,
                # is attributed instead of writing model=None -> "unknown"
                # (issue #2797).
                model=(session.model_config.model if session else None)
                or (str(getattr(task_m, "model", "") or "") or None if task_m else None),
                provider=session.provider if session else None,
                # task_m was reconciled with the runner's .tokens sidecar in
                # _record_completion_metrics, so these carry the real token
                # counts into .sdd/metrics/tasks.jsonl alongside cost_usd.
                tokens_prompt=task_m.tokens_prompt if task_m else 0,
                tokens_completion=task_m.tokens_completion if task_m else 0,
            )
        except Exception as exc:
            logger.warning("Evolution record_task_completion failed: %s", exc)

    if janitor_passed and task.assigned_agent:
        _set_downstream_affinity(orch, task)


def _has_llm_judge_signal(task: Task) -> bool:
    """Return True when any completion signal requires async llm_judge evaluation.

    The sync ``verify_task`` path cannot evaluate ``llm_judge`` signals and
    always reports them as failed. Such tasks must be routed through the
    async ``run_janitor`` pipeline instead.

    Args:
        task: Task to inspect.

    Returns:
        True if any completion signal has type ``"llm_judge"``.
    """
    return any(signal.type == "llm_judge" for signal in task.completion_signals)


def _verify_via_janitor(
    task: Task,
    workdir: Path,
    server_url: str | None,
    judge_model: str | None = None,
    judge_provider: str | None = None,
) -> tuple[bool, list[str]]:
    """Run the async ``run_janitor`` pipeline for a single task synchronously.

    Translates a ``JanitorResult`` back into the ``(passed, failed_signals)``
    shape expected by the rest of ``process_completed_tasks``. Executed inside
    the orchestrator's thread-pool executor, so a dedicated event loop is used
    per invocation to avoid touching any ambient loop from the caller.

    Args:
        task: Task to evaluate.
        workdir: Project root for signal evaluation and git diff lookups.
        server_url: Optional task-server URL forwarded to ``run_janitor`` for
            fix-task creation.
        judge_model: Optional operator-configured model for the janitor's
            llm_judge signal evaluation (from ``bernstein.yaml``'s
            ``judge_model``). Falls back to the janitor's hardcoded default
            when unset.
        judge_provider: Optional operator-configured provider counterpart
            to ``judge_model``.

    Returns:
        Tuple of (all_passed, list_of_failed_signal_descriptions).
    """
    results = asyncio.run(
        run_janitor(
            [task],
            workdir,
            server_url=server_url,
            judge_model=judge_model,
            judge_provider=judge_provider,
        )
    )
    if not results:
        # Task had no completion signals (shouldn't reach here in practice).
        return True, []
    janitor_result = results[0]
    failed_descs = [desc for desc, passed, _ in janitor_result.signal_results if not passed]
    return janitor_result.passed, failed_descs


def _enqueue_alive_exit_janitor_pass(
    orch: Any,
    task: Task,
    *,
    reason: str,
) -> concurrent.futures.Future[tuple[bool, list[str]]] | None:
    """Enqueue a janitor pass for a task whose worker exited via /complete.

    Mirrors the dead-exit scheduling in
    ``bernstein.core.agents.agent_lifecycle.handle_orphaned_task``: that path
    runs ``verify_task_completion`` synchronously, then issues ``POST /complete`` or
    ``retry_or_fail_task``. The alive-exit path has been wired through
    ``process_completed_tasks`` + ``_process_single_completed_task`` for
    months, but in practice it can be skipped when the orchestrator
    self-stops before the next tick (item 30 defect evidence:
    attempt-83808a8a - backend/qa tasks had ``metrics/tasks.jsonl`` rows
    missing because ``_apply_janitor_verdict_action`` was never invoked).
    This helper makes the enqueue observable and reachable from callers
    outside the orchestrator's main tick (drain, retry, manual invocations).

    Log shape mirrors the dead-exit path: every enqueue emits ``janitor:
    enqueued pass task=... session=... role=... reason=...`` at INFO so a
    silent no-op in the tick loop is impossible to overlook.

    Args:
        orch: Orchestrator instance (or any object exposing ``_executor``
            and ``_processed_done_tasks``).
        task: The just-done task whose worker exited via ``/complete``.
        reason: Short string describing WHY the janitor pass is being
            scheduled (e.g. ``"alive_exit_tick"``, ``"alive_exit_drain"``).

    Returns:
        The future tracking the verification result, or None if the
        task has no completion signals (a no-op enqueue; a subsequent
        process_completed_tasks iteration can still process it as
        auto-verified).
    """
    # An artifact-mode task is enqueued even with no declared signals: its
    # completion identity *is* the signed receipt this pass records, so
    # skipping the pass would leave the task with nothing to complete on
    # (issue #2608). A signal-less coding task keeps the auto-verify default.
    if not task.completion_signals and not is_artifact_mode(task):
        logger.info(
            "janitor: enqueued pass task=%s session=%s role=%s reason=%s "
            "no_completion_signals=true (will be marked verified by default)",
            task.id,
            getattr(orch, "_task_to_session", {}).get(task.id, "<none>"),
            task.role,
            reason,
        )
        return None

    session_id = None
    try:
        _find_session = getattr(orch, "_find_session_for_task", None)
        if callable(_find_session):
            sess = _find_session(task.id)
            if sess is not None:
                session_id = sess.id
    except Exception:
        session_id = None

    logger.info(
        "janitor: enqueued pass task=%s session=%s role=%s reason=%s",
        task.id,
        session_id or "<none>",
        task.role,
        reason,
    )

    _orch_config = getattr(orch, "_config", None)
    server_url: str | None = getattr(_orch_config, "server_url", None)
    judge_model: str | None = getattr(_orch_config, "judge_model", None)
    judge_provider: str | None = getattr(_orch_config, "judge_provider", None)
    executor = getattr(orch, "_executor", None)
    if executor is None:
        # Defensive: if the orchestrator has no executor we still want a
        # synchronous verify so the task does not silently vanish.
        try:
            return _JanitorSyncFuture(verify_task_completion(task, orch._workdir))
        except Exception as exc:  # pragma: no cover - defensive only
            logger.warning(
                "janitor: sync-verify failed for task=%s reason=%s exc=%s",
                task.id,
                reason,
                exc,
            )
            return None

    if _has_llm_judge_signal(task):
        return executor.submit(
            _verify_via_janitor,
            task,
            orch._workdir,
            server_url,
            judge_model,
            judge_provider,
        )
    return executor.submit(verify_task_completion, task, orch._workdir)


class _JanitorSyncFuture:
    """Backport of ``concurrent.futures.Future``-like used when no executor exists.

    Holds a pre-computed verification result and exposes ``result()`` /
    ``done()`` to look like a Future, so the rest of the pipeline can
    treat it uniformly.
    """

    def __init__(self, value: tuple[bool, list[str]]) -> None:
        self._value = value

    def result(self, timeout: float | None = None) -> tuple[bool, list[str]]:
        return self._value

    def done(self) -> bool:
        return True


def process_completed_tasks(
    orch: Any,  # Orchestrator instance
    done_tasks: list[Task],
    result: Any,  # TickResult
) -> None:
    """Run janitor verification and record evolution metrics for done tasks.

    Skips tasks already processed in a prior tick. For each new done task,
    submits verification calls in parallel via ``orch._executor``, then
    processes post-verification steps (sync backlog, append decision,
    record evolution) after all verifications complete.

    Tasks whose completion signals include any ``llm_judge`` entry dispatch
    to the async ``run_janitor`` pipeline (wrapped via ``asyncio.run`` in a
    worker thread), since the sync path is sync-only and rejects
    ``llm_judge`` signals outright. All other tasks keep the sync
    ``verify_task_completion`` fast path, which dispatches on the task's
    declared output mode: an artifact-mode task (issue #2608) is verified
    against its produced artifact and completes on a signed lineage receipt,
    every other task on the filesystem/git signals it has always used.

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

    # Fan-out: submit verification calls in parallel. llm_judge signals need
    # the async run_janitor pipeline; everything else uses
    # verify_task_completion.
    #
    # DEFECT 30 FIX: previously the alive-exit janitor enqueue was implicit
    # in the executor.submit() call below; an ops decision (premature
    # self-stop predicate fires BEFORE this iteration can append a row
    # to .sdd/metrics/tasks.jsonl, the orchestrator exits, and the worker
    # task vanishes from janitor's view). The explicit
    # ``_enqueue_alive_exit_janitor_pass`` helper now both logs the
    # enqueue (so a silent no-op is impossible) and exposes a reusable
    # janitor pass entrypoint that drain and retry paths can call outside
    # the orchestrator tick.
    verify_futures: dict[str, concurrent.futures.Future[tuple[bool, list[str]]]] = {}
    for task in new_tasks:
        future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")
        if future is not None:
            verify_futures[task.id] = future

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
        logger.warning("janitor verification raised for %s - treating as failed", task.id)
        passed = False
        failed_signals = ["janitor verification exception"]

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
    merge_failed = False

    # DEFECT 30 FIX: the alive-exit /complete path runs the janitor+verdict
    # action here. Log at INFO so a silent no-op in the orchestrator tick
    # is impossible to overlook (attempt-83808a8a had zero janitor
    # log lines because the tick exited before this ever ran).
    _proc_session = None
    try:
        _find_session = getattr(orch, "_find_session_for_task", None)
        if callable(_find_session):
            _proc_session = _find_session(task.id)
    except Exception:
        _proc_session = None
    _proc_session_id = _proc_session.id if _proc_session is not None else "<none>"
    _proc_alive = bool(_proc_session is not None and _proc_session.status != "dead")
    logger.info(
        "janitor: alive-exit pass starting task=%s session=%s alive_session=%s role=%s",
        task.id,
        _proc_session_id,
        _proc_alive,
        task.role,
    )

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
        cache_verified, cache_diff_lines, merge_failed = _reap_and_cleanup_session(
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

    if task.result_summary:
        try:
            append_decision(orch._workdir, task.id, task.result_summary or task.title, task.result_summary)
        except Exception as exc:
            logger.warning("append_decision failed for task %s: %s", task.id, exc)

    _record_evolution_completion(orch, task, session, task_m, cost_usd, janitor_passed)

    # issue #2792: when the worker's own work passed the janitor but the
    # merge-back failed for a non-conflict reason, the task must not silently
    # return to the open queue for uncapped retry. Route it through the same
    # bounded reopen/permanent-fail budget as a janitor FAIL. A janitor FAIL
    # takes precedence (it already reopens/fails) so the two paths never both
    # act on the same completion.
    if janitor_passed and merge_failed:
        _apply_merge_failure_action(orch, task)
    else:
        _apply_janitor_verdict_action(orch, task, janitor_passed)


_JANITOR_REOPEN_MAX_DEFAULT = 2


def _janitor_reopen_max() -> int:
    """Max janitor-reopen cycles per task (env BERNSTEIN_JANITOR_REOPEN_MAX, default 2)."""
    raw = os.environ.get("BERNSTEIN_JANITOR_REOPEN_MAX", "")
    try:
        value = int(raw) if raw else _JANITOR_REOPEN_MAX_DEFAULT
    except ValueError:
        logger.warning(
            "janitor_verdict_action: invalid BERNSTEIN_JANITOR_REOPEN_MAX=%r, using default %d",
            raw,
            _JANITOR_REOPEN_MAX_DEFAULT,
        )
        return _JANITOR_REOPEN_MAX_DEFAULT
    return max(0, value)


def _apply_janitor_verdict_action(orch: Any, task: Task, janitor_passed: bool) -> None:
    """Act on the janitor verdict for a completed task.

    A task the janitor FAILed must not stay silently ``done``:

    * If the task's janitor-reopen budget (default 2 cycles, override via
      ``BERNSTEIN_JANITOR_REOPEN_MAX``) is not exhausted, the task is
      reopened under the SAME id via ``POST /tasks/{id}/reopen`` and will
      be re-claimed by the normal scheduling path (AutoSpawnGuard and the
      retry machinery are untouched - no new task is created).
    * Otherwise it is permanently failed via ``POST /tasks/{id}/fail``.

    Every decision is logged as ``janitor_verdict_action: ...`` with its
    inputs so a silent no-op is impossible.

    Args:
        orch: Orchestrator instance.
        task: The completed task whose janitor verdict was just resolved.
        janitor_passed: Final janitor + verification-gate verdict.
    """
    if janitor_passed:
        logger.debug("janitor_verdict_action: task=%s verdict=PASS action=none", task.id)
        return

    server_url: str | None = getattr(orch._config, "server_url", None)
    if not server_url:
        logger.warning(
            "janitor_verdict_action: task=%s verdict=FAIL action=skip reason=no_server_url",
            task.id,
        )
        return

    max_cycles = _janitor_reopen_max()
    prior_cycles = int(task.metadata.get("janitor_reopen_count", 0) or 0)

    if prior_cycles < max_cycles:
        cycle = prior_cycles + 1
        try:
            resp = orch._client.post(
                f"{server_url}/tasks/{task.id}/reopen",
                json={"reason": f"janitor verification failed (reopen cycle {cycle}/{max_cycles})"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "janitor_verdict_action: task=%s verdict=FAIL action=reopen cycle=%d/%d FAILED: %s",
                task.id,
                cycle,
                max_cycles,
                exc,
            )
            return
        # Allow the re-completed task to be janitor-verified again on the
        # next completion instead of being skipped as already-processed.
        try:
            orch._processed_done_tasks.pop(task.id, None)
        except Exception:  # pragma: no cover - defensive, dict-like expected
            logger.debug("janitor_verdict_action: could not clear processed marker for %s", task.id)
        logger.info(
            "janitor_verdict_action: task=%s verdict=FAIL action=reopen cycle=%d/%d",
            task.id,
            cycle,
            max_cycles,
        )
    else:
        try:
            resp = orch._client.post(
                f"{server_url}/tasks/{task.id}/fail",
                json={
                    "reason": (
                        f"reopen_budget_exhausted: janitor verification failed after "
                        f"{prior_cycles} reopen cycle(s) (max {max_cycles})"
                    )
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "janitor_verdict_action: task=%s verdict=FAIL action=permanent_fail "
                "reason=reopen_budget_exhausted FAILED: %s",
                task.id,
                exc,
            )
            return
        logger.info(
            "janitor_verdict_action: task=%s verdict=FAIL action=permanent_fail reason=reopen_budget_exhausted",
            task.id,
        )


def _apply_merge_failure_action(orch: Any, task: Task) -> None:
    """Route a non-conflict merge-back failure through the bounded retry budget.

    Issue #2792: a merge-back that failed for a non-conflict reason leaves the
    worker's committed work only on ``agent/<id>`` (preserved by
    :func:`_reap_and_cleanup_session`) and the task DONE-but-unmerged. Without
    this the claim is released back to the open queue and the same merge fails
    on every subsequent worker with no cap. Reopen the task under the same
    bounded budget as a janitor FAIL (``BERNSTEIN_JANITOR_REOPEN_MAX``, shared
    ``metadata['janitor_reopen_count']``) and permanently fail it once the
    budget is spent, so the loop terminates and the failure is visible instead
    of silently burning workers.

    Args:
        orch: Orchestrator instance.
        task: The completed task whose merge-back failed for a non-conflict
            reason.
    """
    server_url: str | None = getattr(orch._config, "server_url", None)
    if not server_url:
        logger.error(
            "merge_failure_action: task=%s action=skip reason=no_server_url",
            task.id,
        )
        return

    max_cycles = _janitor_reopen_max()
    prior_cycles = int(task.metadata.get("janitor_reopen_count", 0) or 0)

    if prior_cycles < max_cycles:
        cycle = prior_cycles + 1
        try:
            resp = orch._client.post(
                f"{server_url}/tasks/{task.id}/reopen",
                json={"reason": f"merge-back failed (non-conflict); bounded retry (reopen cycle {cycle}/{max_cycles})"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error(
                "merge_failure_action: task=%s action=reopen cycle=%d/%d FAILED: %s",
                task.id,
                cycle,
                max_cycles,
                exc,
            )
            return
        # Allow the re-completed task to be verified again on the next
        # completion instead of being skipped as already-processed.
        try:
            orch._processed_done_tasks.pop(task.id, None)
        except Exception:  # pragma: no cover - defensive, dict-like expected
            logger.debug("merge_failure_action: could not clear processed marker for %s", task.id)
        logger.warning(
            "merge_failure_action: task=%s action=reopen cycle=%d/%d reason=merge_back_failed",
            task.id,
            cycle,
            max_cycles,
        )
    else:
        try:
            resp = orch._client.post(
                f"{server_url}/tasks/{task.id}/fail",
                json={
                    "reason": (
                        f"merge_back_failed: non-conflict merge-back failed after "
                        f"{prior_cycles} reopen cycle(s) (max {max_cycles})"
                    )
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error(
                "merge_failure_action: task=%s action=permanent_fail reason=merge_back_failed FAILED: %s",
                task.id,
                exc,
            )
            return
        logger.error(
            "merge_failure_action: task=%s action=permanent_fail reason=merge_back_failed_budget_exhausted",
            task.id,
        )


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


def _get_git_diff_text_in_worktree(worktree_path: Path) -> str:
    """Return the full ``git diff HEAD`` text for a worktree.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        The unified diff of tracked changes against HEAD, or ``""`` on any
        error or when there are no tracked changes.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception as exc:
        logger.debug("_get_git_diff_text_in_worktree failed for %s: %s", worktree_path, exc)
    return ""


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
            with contextlib.suppress(OSError):
                source_file.rename(closed_dir / source_file.name)
                _log.info(
                    "Moved ticket %s to closed/ (exact source match, task: %s)", source_file.name, task.title[:50]
                )
            return

    # --- Strategy 2: exact normalised-title match (no substring!) ---
    title_slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")
    for md_file in (*open_dir.glob("*.yaml"), *open_dir.glob("*.md")):
        # Parse the ticket heading and normalise it
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("# "):
                heading = re.sub(r"^[0-9a-fA-F]+\s*[:-]\s*", "", line[2:].strip())
                heading_slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                if heading_slug == title_slug:
                    with contextlib.suppress(OSError):
                        md_file.rename(closed_dir / md_file.name)
                        _log.info("Moved ticket %s to closed/ (title match, task: %s)", md_file.name, task.title[:50])
                    return
                break  # only check first heading


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
