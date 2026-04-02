"""Tick pipeline helpers: task fetching, batching, and server interaction.

Pure functions and TypedDicts extracted from orchestrator.py to reduce file size
while keeping the Orchestrator class as the single entry point.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from typing_extensions import TypedDict

from bernstein.core.backlog_parser import parse_backlog_text
from bernstein.core.models import Task, TaskType

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from bernstein.core.agent_log_aggregator import AgentLogSummary

logger = logging.getLogger(__name__)

# Fair scheduling: age threshold in seconds after which lower-priority tasks get boosted
_PRIORITY_AGE_THRESHOLD_SECONDS = 300  # 5 minutes
_PRIORITY_BOOST_AMOUNT = 1  # Boost priority by 1 (lower value = higher priority)


# ---------------------------------------------------------------------------
# TypedDicts shared across orchestrator sub-modules
# ---------------------------------------------------------------------------


class _RuffLocation(TypedDict, total=False):
    row: int
    column: int


class RuffViolation(TypedDict, total=False):
    """A single violation from ``ruff check --output-format=json``."""

    code: str
    filename: str
    message: str
    location: _RuffLocation


class TestResults(TypedDict, total=False):
    """Parsed pytest output with pass/fail counts and a one-line summary."""

    passed: int
    failed: int
    summary: str


class CompletionData(TypedDict, total=False):
    """Structured data extracted from an agent's runtime log after task completion."""

    files_modified: list[str]
    test_results: TestResults
    log_summary: AgentLogSummary | None


# ---------------------------------------------------------------------------
# Task server interaction helpers
# ---------------------------------------------------------------------------


def _task_from_dict(raw: dict[str, Any]) -> Task:  # type: ignore[reportUnusedFunction]
    """Deserialise a server JSON response into a domain Task (delegates to Task.from_dict)."""
    return Task.from_dict(raw)


def fetch_all_tasks(
    client: httpx.Client,
    base_url: str,
    statuses: list[str] | None = None,
) -> dict[str, list[Task]]:
    """Fetch all tasks from the server in a single GET /tasks call.

    Makes exactly one HTTP request and buckets the results client-side by
    status, keeping per-tick round-trips to a minimum.

    Args:
        client: httpx client.
        base_url: Server base URL.
        statuses: Status keys to include in the result dict.  Defaults to
            ["open", "claimed", "done", "failed"].

    Returns:
        Dict mapping status string -> list of Tasks.  Always includes keys for
        every requested status even if the list is empty.
        NOTE: "open" here includes tasks with unmet dependencies; callers
        that need the dependency-filtered view should apply their own dep check.
    """
    if statuses is None:
        statuses = ["open", "claimed", "done", "failed"]
    by_status: dict[str, list[Task]] = {s: [] for s in statuses}
    resp = client.get(f"{base_url}/tasks")
    resp.raise_for_status()
    for raw in resp.json():
        task = Task.from_dict(raw)
        key = task.status.value
        if key not in by_status:
            by_status[key] = []
        by_status[key].append(task)
    return by_status


def fail_task(client: httpx.Client, base_url: str, task_id: str, reason: str) -> None:
    """POST /tasks/{task_id}/fail to mark a task as failed.

    Args:
        client: httpx client.
        base_url: Server base URL.
        task_id: ID of the task to fail.
        reason: Why the task failed.
    """
    resp = client.post(f"{base_url}/tasks/{task_id}/fail", json={"reason": reason})
    resp.raise_for_status()


def block_task(client: httpx.Client, base_url: str, task_id: str, reason: str) -> None:
    """POST /tasks/{task_id}/block to mark a task as blocked (requires human intervention).

    Args:
        client: httpx client.
        base_url: Server base URL.
        task_id: ID of the task to block.
        reason: Why the task is blocked.
    """
    resp = client.post(f"{base_url}/tasks/{task_id}/block", json={"reason": reason})
    resp.raise_for_status()


def complete_task(client: httpx.Client, base_url: str, task_id: str, result_summary: str) -> None:
    """POST /tasks/{task_id}/complete to mark a task as done.

    Args:
        client: httpx client.
        base_url: Server base URL.
        task_id: ID of the task to complete.
        result_summary: Human-readable summary of what was accomplished.
    """
    resp = client.post(
        f"{base_url}/tasks/{task_id}/complete",
        json={"result_summary": result_summary},
    )
    resp.raise_for_status()


def prioritize_starving_roles(
    batches: list[list[Task]],
    alive_per_role: dict[str, int],
) -> list[list[Task]]:
    """Re-order batches so that roles with zero alive agents come first.

    Within each group (starving vs. non-starving) the original ordering
    produced by :func:`group_by_role` is preserved (stable sort).

    This prevents a scenario where a well-served role is still under its
    per-role cap and happens to appear first in the round-robin sequence,
    consuming the last available agent slot before a role with **no** agents
    gets one.

    Args:
        batches: Batches from :func:`group_by_role`, in round-robin order.
        alive_per_role: Number of alive (non-dead) agents per role.  Roles
            that have never had an agent are absent from this mapping and
            are treated as having zero alive agents (i.e. starving).

    Returns:
        Re-ordered list with starving-role batches first.  The caller's
        ``batches`` reference is not mutated.
    """
    if not alive_per_role:
        return batches
    return sorted(
        batches,
        key=lambda b: (0 if alive_per_role.get(b[0].role, 0) == 0 else 1) if b else 1,
    )


def group_by_role(
    tasks: list[Task],
    max_per_batch: int,
    alive_per_role: dict[str, int] | None = None,
    priority_overrides: dict[str, int] | None = None,
    task_created_at: dict[str, float] | None = None,
) -> list[list[Task]]:
    """Group open tasks by role into batches of up to max_per_batch.

    Tasks are sorted by priority (ascending, 1=critical first) within each
    role before batching. Upgrade proposal tasks get a priority boost
    (effective priority reduced by 1) to ensure self-evolution tasks are
    processed promptly.

    Batches are interleaved in round-robin order across roles so that no
    single role monopolises all agent slots. Within each round, the most
    critical role (lowest priority value) is emitted first, preserving
    priority ordering while guaranteeing fair distribution.

    If alive_per_role is provided, batches are further reordered to prioritize
    roles with zero alive agents (starving roles) before well-served roles.

    Fair scheduling: tasks waiting longer than PRIORITY_AGE_THRESHOLD_SECONDS
    get their effective priority boosted to prevent P1 tasks from starving P2/P3.

    Example: backend(5 tasks) + qa(3 tasks) → [b1,q1, b2,q2, b3,q3, b4, b5]
    The orchestrator iterates this list and stops at max_agents, so qa never
    starves even though backend has more work.

    Args:
        tasks: Open tasks to batch.
        max_per_batch: Maximum tasks per batch (typically 1-3).
        alive_per_role: Optional map of role -> alive agent count. If provided,
            batches are reordered to prioritize starving roles.
        priority_overrides: Optional per-task effective priority overrides.
            Used by the orchestrator for temporary critical-path promotion
            without mutating persisted task priority.
        task_created_at: Optional map of task_id -> creation timestamp.
            Used for fair scheduling to age-boost older tasks.

    Returns:
        List of batches, each a list of same-role tasks, round-robin interleaved.
        If alive_per_role is provided, starving roles come first.
    """
    by_role: dict[str, list[Task]] = defaultdict(list)
    for task in tasks:
        by_role[task.role].append(task)

    # Calculate current time for age-based priority boosting
    current_time = time.time() if task_created_at else None

    def _sort_key(t: Task) -> tuple[int, int, int]:
        # Priority boost for upgrade proposals: subtract 1 from priority value
        # (lower = higher priority). Second element is original priority for ties.
        priority_boost = t.priority - 1 if t.task_type == TaskType.UPGRADE_PROPOSAL else t.priority

        # Apply priority overrides if provided
        if priority_overrides is not None and t.id in priority_overrides:
            priority_boost = priority_overrides[t.id]

        # Fair scheduling: boost priority of tasks that have been waiting too long
        age_boost = 0
        if current_time is not None and task_created_at and t.id in task_created_at:
            age_seconds = current_time - task_created_at[t.id]
            if age_seconds > _PRIORITY_AGE_THRESHOLD_SECONDS:
                # Boost priority by 1 for each threshold period exceeded
                age_boost = int(age_seconds / _PRIORITY_AGE_THRESHOLD_SECONDS) * _PRIORITY_BOOST_AMOUNT

        # Effective priority: lower is better
        effective_priority = priority_boost - age_boost

        return (effective_priority, t.priority, -age_boost)  # Third element for stable sort

    # Build per-role batch queues, sorted by priority within each role
    role_batch_queues: dict[str, list[list[Task]]] = {}
    for role, role_tasks in by_role.items():
        role_tasks.sort(key=_sort_key)

        # 1. First pass: group by file affinity (transitive overlap)
        affinity_groups: list[list[Task]] = []
        for task in role_tasks:
            task_files = set(task.owned_files)
            matching_groups: list[int] = []
            if task_files:
                for i, group in enumerate(affinity_groups):
                    group_files: set[str] = set().union(*(set(t.owned_files) for t in group))  # type: ignore[reportUnknownVariableType]
                    if task_files & group_files:
                        matching_groups.append(i)

            if matching_groups:
                # Merge into the first matching group
                first_idx = matching_groups[0]
                affinity_groups[first_idx].append(task)
                # If it matched multiple groups, merge them all (transitive)
                for other_idx in sorted(matching_groups[1:], reverse=True):
                    affinity_groups[first_idx].extend(affinity_groups.pop(other_idx))
            else:
                affinity_groups.append([task])

        # 2. Second pass: pack affinity groups into batches of max_per_batch
        role_batches: list[list[Task]] = []
        for group in affinity_groups:
            # Try to pack this affinity group into an existing batch that has room.
            # Since affinity groups are disjoint by file overlap, any existing batch
            # is guaranteed not to conflict with this group.
            added = False
            # Optimization: only try to pack small groups. If a group is already
            # at or near max_per_batch, just give it its own batch(es).
            if len(group) < max_per_batch:
                for batch in role_batches:
                    if len(batch) + len(group) <= max_per_batch:
                        batch.extend(group)
                        added = True
                        break

            if not added:
                # If group is too large to fit or we couldn't find a batch with room,
                # create new batch(es) for it.
                for i in range(0, len(group), max_per_batch):
                    role_batches.append(group[i : i + max_per_batch])

        role_batch_queues[role] = role_batches

    # Round-robin interleave: emit one batch per role per round.
    # Within each round, the most critical roles (lowest priority value) go first.
    result: list[list[Task]] = []
    while any(role_batch_queues.values()):
        round_batches: list[list[Task]] = []
        for role in list(role_batch_queues.keys()):
            if role_batch_queues[role]:
                round_batches.append(role_batch_queues[role].pop(0))
        round_batches.sort(key=lambda b: b[0].priority)
        result.extend(round_batches)

    # If alive_per_role info is available, prioritize starving roles
    if alive_per_role is not None:
        result = prioritize_starving_roles(result, alive_per_role)

    return result


# ---------------------------------------------------------------------------
# Backlog parsing
# ---------------------------------------------------------------------------


def parse_backlog_file(filename: str, content: str) -> dict[str, Any]:
    """Parse a backlog markdown file into a task creation payload.

    Extracts title, role, priority, and description from the markdown.
    Falls back to safe defaults for any missing fields.

    Args:
        filename: The filename (e.g. "100-fix-the-bug.md"), used to derive a
            slug for the title when no H1 heading is found.
        content: Full markdown text of the backlog file.

    Returns:
        Dict suitable for POST /tasks.
    """
    parsed = parse_backlog_text(filename, content)
    if parsed is None:
        title = filename.replace(".md", "").replace("-", " ")
        return {
            "title": title,
            "description": content.strip(),
            "role": "backend",
            "priority": 2,
            "scope": "medium",
            "complexity": "medium",
        }
    return parsed.to_task_payload()


# ---------------------------------------------------------------------------
# Cost tracking helpers
# ---------------------------------------------------------------------------

# Cache for compute_total_spent: maps absolute metrics_dir path ->
# (cached_total, {file_path_str: (mtime_ns, file_total)}).
total_spent_cache: dict[str, tuple[float, dict[str, tuple[int, float]]]] = {}


def _parse_file_total(jsonl_file: Path) -> float:
    """Parse cost contributions from a single cost_efficiency JSONL file.

    Streams line-by-line to avoid loading the entire file into memory
    (files can grow to 100MB+ during long runs).
    """
    file_total = 0.0
    try:
        with open(jsonl_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    point = json.loads(line)
                    if "task_id" in point.get("labels", {}):
                        file_total += point.get("value", 0.0)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return file_total


def compute_total_spent(workdir: Path) -> float:
    """Sum cost_efficiency metric values recorded for individual tasks.

    Reads all cost_efficiency_*.jsonl files in .sdd/metrics/ and returns the
    total cost in USD for entries that have a ``task_id`` label, avoiding
    double-counting the per-agent average entries that lack that label.

    Results are mtime-cached: files that have not changed since the last call
    are not re-read, making repeated calls on an unchanged metrics directory
    effectively free.

    Args:
        workdir: Project root directory.

    Returns:
        Total USD spent as recorded in metrics files.
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    cache_key = str(metrics_dir)
    cached_total, cached_file_data = total_spent_cache.get(cache_key, (0.0, {}))

    try:
        current_files = list(metrics_dir.glob("cost_efficiency_*.jsonl"))
    except OSError:
        return cached_total

    current_paths = {str(f) for f in current_files}
    cached_paths = set(cached_file_data.keys())

    # If any previously-seen file was removed, subtract its contribution
    # from the cached total incrementally.
    removed_paths = cached_paths - current_paths
    total = cached_total
    new_file_data: dict[str, tuple[int, float]] = dict(cached_file_data)
    for removed in removed_paths:
        _, old_file_total = new_file_data.pop(removed)
        total -= old_file_total

    for jsonl_file in current_files:
        path_str = str(jsonl_file)
        try:
            mtime_ns = os.stat(jsonl_file).st_mtime_ns
        except OSError:
            continue

        cached_entry = new_file_data.get(path_str)
        if cached_entry is not None and cached_entry[0] == mtime_ns:
            # File unchanged - skip re-parsing.
            continue

        # Subtract old contribution for this file (if any), then add new.
        old_file_total = cached_entry[1] if cached_entry is not None else 0.0
        new_file_total = _parse_file_total(jsonl_file)
        total += new_file_total - old_file_total
        new_file_data[path_str] = (mtime_ns, new_file_total)

    total_spent_cache[cache_key] = (total, new_file_data)
    return total
