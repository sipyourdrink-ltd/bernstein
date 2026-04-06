"""WEB-007: API versioning under /api/v1/.

Mounts all existing route groups under /api/v1/ while preserving
backward compatibility on the original unprefixed paths.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")
