"""Deterministic task-splitting helpers backed by the manager decomposer."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bernstein.core.models import Scope, Task

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class TaskDecomposer(Protocol):
    """Protocol for manager-backed task decomposition."""

    def decompose_sync(self, task: Task, *, min_subtasks: int = 2, max_subtasks: int = 5) -> list[Task]:
        """Return 2-5 decomposed subtasks for the given task."""
        ...


@dataclass(frozen=True)
class TaskSplitter:
    """Create Bernstein subtasks and park the parent task until they finish."""

    client: httpx.Client
    server_url: str
    min_subtasks: int = 2
    max_subtasks: int = 5

    def should_split(self, task: Task) -> bool:
        """Return whether a task exceeds the direct-execution heuristic."""
        if task.estimated_minutes > 60:
            return True
        return len(task.description.split()) > 200

    def split(self, task: Task, manager: TaskDecomposer) -> list[str]:
        """Decompose a task, create subtasks, and mark the parent as waiting."""
        drafts = manager.decompose_sync(task, min_subtasks=self.min_subtasks, max_subtasks=self.max_subtasks)
        if not (self.min_subtasks <= len(drafts) <= self.max_subtasks):
            raise ValueError(
                f"Manager returned {len(drafts)} subtasks; expected {self.min_subtasks}-{self.max_subtasks}"
            )

        created_ids: list[str] = []
        for draft in drafts[: self.max_subtasks]:
            description = draft.description
            if f"[subtask of {task.id}]" not in description:
                description = f"{description.rstrip()}\n\n[subtask of {task.id}]"
            scope = draft.scope if draft.scope in {Scope.SMALL, Scope.MEDIUM} else Scope.SMALL
            body = {
                "title": draft.title,
                "description": description,
                "role": draft.role or task.role,
                "tenant_id": task.tenant_id,
                "priority": task.priority,
                "scope": scope.value,
                "complexity": draft.complexity.value,
                "estimated_minutes": min(draft.estimated_minutes or 30, 60),
                "owned_files": list(draft.owned_files),
                "repo": task.repo,
                "parent_task_id": task.id,
            }
            response = self.client.post(f"{self.server_url}/tasks", json=body)
            response.raise_for_status()
            created_ids.append(str(response.json()["id"]))

        wait_response = self.client.post(
            f"{self.server_url}/tasks/{task.id}/wait-for-subtasks",
            json={"subtask_count": len(created_ids)},
        )
        wait_response.raise_for_status()
        logger.info("Split task %s into %d subtasks", task.id, len(created_ids))
        return created_ids
