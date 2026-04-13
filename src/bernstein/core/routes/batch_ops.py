"""WEB-017: Batch operations endpoint for task management.

POST /tasks/batch-ops — execute bulk cancel, retry, reprioritize, or tag
operations on up to 200 tasks in a single request.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from bernstein.core.task_store import TaskStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batch-operations"])

_MAX_BATCH_SIZE: int = 200


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BatchAction(StrEnum):
    """Supported batch operation types."""

    CANCEL = "cancel"
    RETRY = "retry"
    REPRIORITIZE = "reprioritize"
    TAG = "tag"


class BatchRequest(BaseModel):
    """Request body for POST /tasks/batch-ops."""

    action: BatchAction
    ids: list[str]
    priority: int | None = None
    tags: list[str] | None = None


class BatchResult(BaseModel):
    """Response body for POST /tasks/batch-ops."""

    succeeded: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total number of tasks processed (succeeded + failed)."""
        return len(self.succeeded) + len(self.failed)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_batch_request(req: BatchRequest) -> list[str]:
    """Validate a batch request and return a list of error messages.

    Checks:
    - ``ids`` must not be empty.
    - ``ids`` must contain at most ``_MAX_BATCH_SIZE`` entries.
    - ``reprioritize`` action requires a ``priority`` value.
    - ``tag`` action requires a non-empty ``tags`` list.

    Returns:
        A list of human-readable error strings (empty when valid).
    """
    errors: list[str] = []
    if not req.ids:
        errors.append("ids must not be empty")
    if len(req.ids) > _MAX_BATCH_SIZE:
        errors.append(f"ids exceeds maximum batch size of {_MAX_BATCH_SIZE}")
    if req.action == BatchAction.REPRIORITIZE and req.priority is None:
        errors.append("priority is required for reprioritize action")
    if req.action == BatchAction.TAG and (not req.tags or len(req.tags) == 0):
        errors.append("tags is required for tag action")
    return errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


async def _cancel_task(store: TaskStore, task_id: str) -> None:
    """Cancel a single task, raising on error."""
    await store.cancel(task_id, reason="batch cancel")


async def _retry_task(store: TaskStore, task_id: str) -> None:
    """Retry a failed task by transitioning it back to OPEN."""
    from bernstein.core.lifecycle import transition_task
    from bernstein.core.models import TaskStatus

    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    if task.status != TaskStatus.FAILED:
        raise ValueError(f"Task '{task_id}' is not in failed state (current: {task.status.value})")
    store._index_remove(task)
    transition_task(task, TaskStatus.OPEN, actor="batch_ops", reason="batch retry")
    task.assigned_agent = None
    task.result_summary = None
    task.retry_count += 1
    task.version += 1
    store._index_add(task)
    await store._append_jsonl(store._task_to_record(task))


async def _reprioritize_task(store: TaskStore, task_id: str, priority: int) -> None:
    """Update the priority of a single task."""
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    task.priority = priority
    task.version += 1
    store._index_add(task)
    await store._append_jsonl(store._task_to_record(task))


async def _tag_task(store: TaskStore, task_id: str, tags: list[str]) -> None:
    """Add tags to a task's metadata."""
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    existing_tags: list[str] = task.metadata.get("tags", [])
    merged = list(dict.fromkeys(existing_tags + tags))  # deduplicate, preserve order
    task.metadata["tags"] = merged
    task.version += 1
    await store._append_jsonl(store._task_to_record(task))


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/tasks/batch-ops", responses={422: {"description": "Invalid batch request"}})
async def batch_operations(body: BatchRequest, request: Request) -> BatchResult:
    """Execute a batch operation on multiple tasks.

    Supported actions:
    - **cancel**: Cancel all specified tasks.
    - **retry**: Reset failed tasks back to open.
    - **reprioritize**: Update priority on all specified tasks (requires ``priority``).
    - **tag**: Add tags to all specified tasks (requires ``tags``).

    Returns a result with lists of succeeded and failed task IDs.
    """
    errors = validate_batch_request(body)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    store = _get_store(request)
    result = BatchResult()

    for raw_id in body.ids:
        task_id = re.sub(r"[^\w\-]", "", raw_id)[:64]
        try:
            if body.action == BatchAction.CANCEL:
                await _cancel_task(store, task_id)
            elif body.action == BatchAction.RETRY:
                await _retry_task(store, task_id)
            elif body.action == BatchAction.REPRIORITIZE:
                assert body.priority is not None  # validated above
                await _reprioritize_task(store, task_id, body.priority)
            elif body.action == BatchAction.TAG:
                assert body.tags is not None  # validated above
                await _tag_task(store, task_id, body.tags)
            result.succeeded.append(task_id)
        except KeyError:
            result.failed[task_id] = "not found"
        except Exception as exc:
            result.failed[task_id] = str(exc)
            logger.warning("batch %s failed for task %s: %s", body.action, task_id[:64], type(exc).__name__)

    return result
