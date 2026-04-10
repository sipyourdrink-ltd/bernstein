"""WEB-019: Audit log endpoint with search and filtering.

Exposes audit log entries via GET /audit with pagination,
event_type filtering, time range, and full-text search.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


class AuditLogQuery(BaseModel):
    """Query parameters for audit log search."""

    event_type: str | None = None
    from_ts: str | None = Field(None, alias="from")
    to_ts: str | None = Field(None, alias="to")
    search: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)

    model_config = {"populate_by_name": True}

    @property
    def offset(self) -> int:
        """Compute offset from page number."""
        return (self.page - 1) * self.page_size


def filter_events(
    events: list[dict[str, Any]],
    *,
    event_type: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Filter audit events by criteria.

    Args:
        events: Raw event dicts.
        event_type: Filter by event_type field.
        from_ts: ISO timestamp lower bound (inclusive).
        to_ts: ISO timestamp upper bound (inclusive).
        search: Full-text search across event details.

    Returns:
        Filtered list of events.
    """
    result: list[dict[str, Any]] = []
    for ev in events:
        if event_type and ev.get("event_type") != event_type:
            continue
        ts = ev.get("timestamp", "")
        if from_ts and ts < from_ts:
            continue
        if to_ts and ts > to_ts:
            continue
        if search:
            text = json.dumps(ev.get("details", {})).lower()
            if search.lower() not in text:
                continue
        result.append(ev)
    return result


def paginate(items: list[Any], page: int, page_size: int) -> list[Any]:
    """Return a page slice of items.

    Args:
        items: Full list.
        page: 1-based page number.
        page_size: Items per page.

    Returns:
        Slice of items for the requested page.
    """
    start = (page - 1) * page_size
    return items[start : start + page_size]


@router.get("/audit")
async def query_audit_log(
    request: Request,
    event_type: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Query the audit log with filtering and pagination.

    Returns:
        Dict with items, total, page, page_size.
    """
    from_ts = request.query_params.get("from")
    to_ts = request.query_params.get("to")

    audit_dir = Path(".sdd/audit")
    events: list[dict[str, Any]] = []

    if audit_dir.is_dir():
        for log_file in sorted(audit_dir.glob("*.jsonl")):
            for line in log_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    filtered = filter_events(
        events,
        event_type=event_type,
        from_ts=from_ts,
        to_ts=to_ts,
        search=search,
    )

    page_items = paginate(filtered, page, page_size)

    return {
        "items": page_items,
        "total": len(filtered),
        "page": page,
        "page_size": page_size,
    }
