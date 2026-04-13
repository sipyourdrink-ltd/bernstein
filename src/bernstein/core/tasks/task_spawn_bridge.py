"""Auto-decomposition and conflict resolution for tasks.

Extracted from task_lifecycle.py — contains should_auto_decompose,
auto_decompose_task, and create_conflict_resolution_task.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)


def should_auto_decompose(
    task: Task,
    decomposed_task_ids: set[str],
    _workdir: Path | None = None,
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
