"""Compact small tasks into larger batches for efficiency.

When several tiny tasks (complexity=LOW, or estimated_minutes below
threshold) would otherwise each consume a full agent session, this module
opportunistically merges them into a single batch so that one agent can
burn through all of them in one go.

This operates **after** ``group_by_role`` affinity grouping: tasks in the
same batch already share a role and non-conflicting file sets.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bernstein.core.models import Complexity, Scope

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# Upper bound on combined estimated minutes before we stop merging.
_MAX_COMBINED_ESTIMATED_MINUTES = 60

# Maximum number of tasks a single compacted batch may contain.
_MAX_TASKS_PER_COMPACTED_BATCH = 5


def _is_small_task(task: Task) -> bool:
    """Return True if the task is small enough to be a batching candidate.

    A task is "small" when:
    - Complexity is LOW, **or**
    - Scope is SMALL **and** estimated_minutes ≤ 15
    """
    if task.complexity == Complexity.LOW:
        return True
    return task.scope == Scope.SMALL and task.estimated_minutes <= 15


def _batch_files_conflict(batch_a: list[Task], batch_b: list[Task]) -> bool:
    """Return True when two batches share owned files.

    Args:
        batch_a: First task batch.
        batch_b: Second task batch.

    Returns:
        True if any file from batch_a appears in batch_b.
    """
    files_a: set[str] = set()
    for task in batch_a:
        files_a.update(task.owned_files)

    files_b: set[str] = set()
    for task in batch_b:
        files_b.update(task.owned_files)

    return bool(files_a & files_b)


def _estimated_sum(tasks: list[Task]) -> int:
    """Sum of estimated_minutes.

    Args:
        tasks: List of tasks.

    Returns:
        Combined estimated minutes.
    """
    return sum(t.estimated_minutes for t in tasks)


def compact_small_tasks(
    batches: list[list[Task]],
    max_per_batch: int = 3,
) -> list[list[Task]]:
    """Compact batches of small tasks into larger batches for efficiency.

    Strategy:
    1. Identify "small" batches where **all** tasks are small tasks.
    2. Identify batches that contain only one small task.
    3. Merge single-small-task batches into same-role small batches when:
       - Total task count ≤ max_per_batch
       - Combined estimated minutes ≤ _MAX_COMBINED_ESTIMATED_MINUTES
       - No file conflicts between batches
    4. Preserve non-small batches as-is.

    This reduces agent spawn churn when there are many tiny tasks that
    would each consume a full 1-3 minute agent session individually.

    Args:
        batches: Batches from :func:`group_by_role` (same role per batch,
            round-robin interleaved, respecting file affinity).
        max_per_batch: Maximum tasks per resulting batch.

    Returns:
        Compacted list of batches (same structure as input).
    """
    if len(batches) <= 1:
        return batches

    effective_max = min(max_per_batch, _MAX_TASKS_PER_COMPACTED_BATCH)

    # Classify each batch as "small" (all tasks are small) or "other".
    small_batches: list[int] = []  # indices of all-small batches
    other_indices: list[int] = []
    for i, batch in enumerate(batches):
        if batch and all(_is_small_task(t) for t in batch):
            small_batches.append(i)
        else:
            other_indices.append(i)

    # Nothing to compact if no small batches exist.
    if not small_batches:
        return batches

    # Only compact single-task small batches into neighbouring small batches.
    # We iterate small batch indices and try to merge consecutive pairs.
    result = [list(batch) for batch in batches]
    merged_indices: set[int] = set()

    for pos, idx in enumerate(small_batches):
        if idx in merged_indices:
            continue
        # Find the next small batch of the **same role** to merge with.
        batch = result[idx]
        role = batch[0].role

        # Look ahead within small_batches for same-role candidates.
        for next_pos in range(pos + 1, len(small_batches)):
            next_idx = small_batches[next_pos]
            if next_idx in merged_indices:
                continue
            next_batch = result[next_idx]

            # Must be same role.
            if next_batch[0].role != role:
                continue

            # Check capacity.
            if len(batch) + len(next_batch) > effective_max:
                continue

            # Check combined estimated minutes.
            if _estimated_sum(batch) + _estimated_sum(next_batch) > _MAX_COMBINED_ESTIMATED_MINUTES:
                continue

            # Check file conflicts.
            if _batch_files_conflict(batch, next_batch):
                continue

            # Merge.
            batch.extend(next_batch)
            merged_indices.add(next_idx)
            result[next_idx] = []  # Mark for removal
            break  # Only merge one neighbour per batch

    # Filter out emptied batches and return.
    compacted = [b for b in result if b]

    if len(compacted) < len(batches):
        logger.info(
            "Compacted %d → %d batches (removed %d single-task batches)",
            len(batches),
            len(compacted),
            len(batches) - len(compacted),
        )

    return compacted
