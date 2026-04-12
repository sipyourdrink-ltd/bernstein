"""Status routes — backward-compatibility shim.

All route handlers have been decomposed into focused sub-modules:
- status_dashboard: Dashboard data, display, and shared operational helpers
- status_events: SSE events, badge, memory audit, broadcast
- status_lifecycle: Health, config, lifecycle, cache stats, metrics

This module re-assembles them into a single ``router`` for the server.
"""

from __future__ import annotations

from fastapi import APIRouter

from bernstein.core.routes.status_dashboard import router as _dashboard_router
from bernstein.core.routes.status_events import router as _events_router
from bernstein.core.routes.status_lifecycle import router as _lifecycle_router

router = APIRouter()
router.include_router(_dashboard_router)
router.include_router(_events_router)
router.include_router(_lifecycle_router)

# Re-export helpers that other modules import directly from this path.
from bernstein.core.routes.status_dashboard import build_alerts as build_alerts  # noqa: E402
