"""Helpers for tenant-aware request and record tagging."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

DEFAULT_TENANT_ID = "default"


def normalize_tenant_id(raw: str | None) -> str:
    """Normalize a raw tenant ID into a stable non-empty value."""

    value = (raw or "").strip()
    return value or DEFAULT_TENANT_ID


def request_tenant_id(request: Request) -> str:
    """Return the normalized tenant ID for a request."""

    state_value = getattr(request.state, "tenant_id", None)
    if isinstance(state_value, str) and state_value.strip():
        return normalize_tenant_id(state_value)
    return normalize_tenant_id(request.headers.get("x-tenant-id"))
