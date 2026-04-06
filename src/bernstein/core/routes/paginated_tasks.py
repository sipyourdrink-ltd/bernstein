"""WEB-011: Enhanced paginated task list with sorting and filtering.

GET /tasks/search?page=1&per_page=20&sort=created_at&order=desc&status=open&role=backend
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from bernstein.core.server import TaskResponse, TaskStore, task_to_response
from bernstein.core.tenanting import request_tenant_id

if TYPE_CHECKING:
    from bernstein.core.models import Task

router = APIRouter()

_SORTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "created_at",
        "priority",
        "title",
        "role",
        "status",
    }
)


class PaginatedSearchResponse(BaseModel):
    """Paginated task search response with metadata."""

    tasks: list[TaskResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
    sort: str
    order: str
    filters: dict[str, str] = Field(default_factory=dict)


def _sort_key(task: Task, sort_field: str) -> Any:
    """Return a sortable key for a task field."""
    val = getattr(task, sort_field, None)
    if val is None:
        return ""
    if hasattr(val, "value"):
        return val.value
    return val


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


@router.get("/tasks/search", response_model=PaginatedSearchResponse)
def search_tasks(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    sort: str = "created_at",
    order: str = "desc",
    status: str | None = None,
    role: str | None = None,
    assigned_agent: str | None = None,
) -> PaginatedSearchResponse:
    """Search tasks with pagination, sorting, and filtering.

    Query params:
        page: Page number (1-based, default 1).
        per_page: Items per page (1-100, default 20).
        sort: Sort field (created_at, priority, title, role, status).
        order: Sort order (asc, desc; default desc).
        status: Filter by task status.
        role: Filter by task role.
        assigned_agent: Filter by assigned agent.
    """
    store = _get_store(request)
    tenant_id = request_tenant_id(request)

    # Get all tasks for tenant
    tasks = store.list_tasks(
        status=status,
        tenant_id=tenant_id,
    )

    # Additional filtering
    filters: dict[str, str] = {}
    if status:
        filters["status"] = status
    if role:
        tasks = [t for t in tasks if t.role == role]
        filters["role"] = role
    if assigned_agent:
        tasks = [t for t in tasks if t.assigned_agent == assigned_agent]
        filters["assigned_agent"] = assigned_agent

    # Sorting
    effective_sort = sort if sort in _SORTABLE_FIELDS else "created_at"
    effective_order = order if order in ("asc", "desc") else "desc"
    tasks.sort(
        key=lambda t: _sort_key(t, effective_sort),
        reverse=(effective_order == "desc"),
    )

    # Pagination
    effective_per_page = max(1, min(per_page, 100))
    effective_page = max(1, page)
    total = len(tasks)
    total_pages = max(1, (total + effective_per_page - 1) // effective_per_page)
    start = (effective_page - 1) * effective_per_page
    page_tasks = tasks[start : start + effective_per_page]

    return PaginatedSearchResponse(
        tasks=[task_to_response(t) for t in page_tasks],
        total=total,
        page=effective_page,
        per_page=effective_per_page,
        total_pages=total_pages,
        sort=effective_sort,
        order=effective_order,
        filters=filters,
    )
