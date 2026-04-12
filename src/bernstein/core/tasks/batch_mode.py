"""Batch mode: combine multiple tasks into a single agent spawn.

When several tasks share the same role and collectively represent a large
refactoring effort, it is more efficient to delegate them to a single
Claude Code agent using ``claude --batch`` (stdin-piped prompt) rather
than spawning N parallel agents.  This module provides the decision
logic and prompt construction for that workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.defaults import TASK as _TASK_DEFAULTS

if TYPE_CHECKING:
    from bernstein.core.models import Task


@dataclass
class BatchConfig:
    """Tuning knobs for batch-mode spawning.

    Attributes:
        max_files_per_batch: Upper bound on owned_files across all tasks
            before splitting into separate batches.
        timeout_minutes: Per-batch wall-clock timeout.
        model: Model to use for the batch agent (e.g. "opus", "sonnet").
    """

    max_files_per_batch: int = 50
    timeout_minutes: int = 60
    model: str = "sonnet"


@dataclass
class BatchTask:
    """A merged representation of multiple tasks for single-agent execution.

    Attributes:
        original_task_ids: IDs of the tasks that were combined.
        combined_prompt: Merged prompt with section markers per original task.
        scope: The effective scope of the combined work ("large" for batches).
    """

    original_task_ids: list[str] = field(default_factory=list[str])
    combined_prompt: str = ""
    scope: str = "large"


# ---------------------------------------------------------------------------
# Decision: should a set of tasks use batch mode?
# ---------------------------------------------------------------------------


def should_use_batch(tasks: list[Task]) -> bool:
    """Decide whether a list of tasks should be combined into a single batch.

    Returns True when ALL of the following hold:
    - More than ``TASK.min_batch_size`` tasks (default 3).
    - All tasks share the same role.
    - The combined scope would be "large" OR the tasks share many owned_files.

    Args:
        tasks: Candidate tasks (must be non-empty).

    Returns:
        True if batch mode is recommended for this set.
    """
    if len(tasks) <= _TASK_DEFAULTS.min_batch_size:
        return False

    roles = {t.role for t in tasks}
    if len(roles) != 1:
        return False

    # Check scope: any "large" task makes the whole batch large-scope.
    any_large = any(t.scope.value == "large" for t in tasks)
    if any_large:
        return True

    # Check file overlap: if tasks collectively touch many files, batch is
    # worthwhile to avoid merge conflicts from parallel agents.
    all_files: set[str] = set()
    for t in tasks:
        all_files.update(t.owned_files)
    return len(all_files) >= 5


# ---------------------------------------------------------------------------
# Combine tasks into a single batch prompt
# ---------------------------------------------------------------------------

_SECTION_MARKER = "---"


def combine_tasks_for_batch(tasks: list[Task]) -> BatchTask:
    """Merge multiple task descriptions into one combined prompt.

    Each original task gets a clearly delimited section so the agent can
    track which sub-goal it is working on and report back per task.

    Args:
        tasks: Tasks to merge (must be non-empty, should share the same role).

    Returns:
        A ``BatchTask`` with the merged prompt and original IDs.
    """
    sections: list[str] = []
    task_ids: list[str] = []

    for i, task in enumerate(tasks, 1):
        task_ids.append(task.id)
        header = f"{_SECTION_MARKER}\n## Task {i}: {task.title} (id={task.id})"
        body = task.description or task.title
        files_note = ""
        if task.owned_files:
            files_note = f"\nAffected files: {', '.join(task.owned_files)}"
        sections.append(f"{header}\n{body}{files_note}")

    preamble = (
        "You are completing multiple related tasks in a single session.\n"
        "Work through each task sequentially. After finishing ALL tasks,\n"
        "output a summary with one section per task using the exact format:\n"
        "## Result: <task_id>\n<summary>\n\n"
    )
    combined = preamble + "\n\n".join(sections) + "\n"

    return BatchTask(
        original_task_ids=task_ids,
        combined_prompt=combined,
        scope="large",
    )


# ---------------------------------------------------------------------------
# Parse combined result back into per-task summaries
# ---------------------------------------------------------------------------


def split_batch_result(
    result_summary: str,
    original_task_ids: list[str],
) -> dict[str, str]:
    """Parse a combined batch result into per-task summaries.

    Looks for ``## Result: <task_id>`` section headers in the result text.
    Tasks without a matching section get a fallback summary.

    Args:
        result_summary: The full result text from the batch agent.
        original_task_ids: Task IDs to extract summaries for.

    Returns:
        Mapping of task_id -> summary string.
    """
    results: dict[str, str] = {}
    id_set = set(original_task_ids)

    # Split on "## Result:" headers
    parts = result_summary.split("## Result:")
    for part in parts[1:]:  # skip text before first header
        # First line after the header is the task ID
        lines = part.strip().splitlines()
        if not lines:
            continue
        # The task_id is the first token on the first line
        first_line = lines[0].strip()
        matched_id = ""
        for tid in id_set:
            if first_line.startswith(tid):
                matched_id = tid
                break
        if matched_id:
            # Everything after the first line is the summary
            summary = "\n".join(lines[1:]).strip()
            results[matched_id] = summary if summary else "Completed (no details)."

    # Fill in fallback for tasks without explicit sections
    for tid in original_task_ids:
        if tid not in results:
            results[tid] = "Completed as part of batch (no individual summary)."

    return results
