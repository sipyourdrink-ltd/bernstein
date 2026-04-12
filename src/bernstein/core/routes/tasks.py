"""Task routes — backward-compatibility shim.

All route handlers have been decomposed into focused sub-modules:
- task_crud: Task CRUD, agent heartbeats, bulletin, direct channel
- task_a2a: A2A federation routes
- task_cluster: Cluster management routes

This module re-assembles them into a single ``router`` for the server.
"""

from __future__ import annotations

from fastapi import APIRouter

from bernstein.core.routes.task_a2a import router as _a2a_router
from bernstein.core.routes.task_cluster import router as _cluster_router
from bernstein.core.routes.task_crud import router as _crud_router

router = APIRouter()
router.include_router(_crud_router)
router.include_router(_cluster_router)
router.include_router(_a2a_router)

# Re-export helpers that other modules may import directly from this path.
from bernstein.core.routes.task_crud import (  # noqa: E402
    _get_sse_bus as _get_sse_bus,
)
from bernstein.core.routes.task_crud import _get_store as _get_store  # noqa: E402
from bernstein.core.routes.task_crud import (  # noqa: E402
    _require_task_access as _require_task_access,
)
from bernstein.core.routes.task_crud import (  # noqa: E402
    _resolve_request_tenant_scope as _resolve_request_tenant_scope,
)
