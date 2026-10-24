"""TaskStore projection for signed MCP claims, without a second mutable backlog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.tasks.claim import BacklogEntry

if TYPE_CHECKING:
    from bernstein.core.tasks.claim import ClaimFilter
    from bernstein.core.tasks.models import Task
    from bernstein.core.tasks.task_store_core import TaskStore


def task_claim_entry(task: Task) -> BacklogEntry:
    """Project a task into the canonical, independently hashable receipt row."""
    return BacklogEntry(
        id=task.id,
        role=task.role,
        status=task.status.value,
        claimer=task.claimed_by_session,
        depends_on=list(task.depends_on),
        attempts=task.retry_count,
        metadata={
            "title": task.title,
            "description": task.description,
            "priority": task.priority,
            "scope": task.scope.value,
            "tenant_id": task.tenant_id,
            "version": task.version,
        },
    )


def task_claim_snapshot(
    store: TaskStore, tenant_id: str, task_ids: frozenset[str] | None = None
) -> list[dict[str, object]]:
    """Return the tenant's deterministic receipt snapshot from its owning store."""
    return [
        task_claim_entry(task).to_dict()
        for task in sorted(store.list_tasks(tenant_id=tenant_id), key=lambda t: t.id)
        if task_ids is None or task.id in task_ids
    ]


async def claim_store_entry(
    store: TaskStore, tenant_id: str, claim_filter: ClaimFilter, claimer_id: str
) -> BacklogEntry | None:
    """Acquire a filtered task using the store's atomic CAS and dependency gate."""
    for task in sorted(store.list_tasks(tenant_id=tenant_id), key=lambda t: (t.priority, t.id)):
        entry = task_claim_entry(task)
        if not claim_filter.allows(entry):
            continue
        try:
            claimed = await store.claim_by_id(
                task.id, expected_version=task.version, agent_role=claim_filter.role, claimed_by_session=claimer_id
            )
        except ValueError:
            # Another worker acquired it, or an authoritative dependency/file
            # ownership gate refused it. Caller-supplied completed_ids cannot
            # override the store's dependency state.
            continue
        return task_claim_entry(claimed)
    return None
