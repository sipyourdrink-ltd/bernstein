"""Tests for structured API access logging."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for the server under test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    """Create a fresh FastAPI app instance."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Return an async client wired to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def _read_access_entries(tmp_path: Path) -> list[dict[str, object]]:
    """Read structured access log entries from the temporary runtime dir."""
    access_log = tmp_path / "access.jsonl"
    assert access_log.exists()
    return [json.loads(line) for line in access_log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.anyio
async def test_access_log_records_request_shape(client: AsyncClient, tmp_path: Path) -> None:
    """Middleware should emit one JSON entry per request with stable fields."""
    response = await client.get("/health", headers={"user-agent": "pytest-agent", "x-tenant-id": "acme"})

    assert response.status_code == 200
    assert response.headers["x-request-id"]

    entries = _read_access_entries(tmp_path)
    entry = entries[-1]
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status"] == 200
    assert entry["tenant_id"] == "acme"
    assert entry["actor"] == "anonymous"
    assert entry["user_agent"] == "pytest-agent"
    assert cast("float", entry["duration_ms"]) >= 0
    assert entry["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
async def test_access_log_preserves_request_id_and_actor(client: AsyncClient, tmp_path: Path) -> None:
    """Middleware should preserve caller request IDs and mark authenticated actors."""
    response = await client.get(
        "/health",
        headers={
            "authorization": "Bearer token-123",
            "x-request-id": "req-abc",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-abc"

    entries = _read_access_entries(tmp_path)
    entry = entries[-1]
    assert entry["request_id"] == "req-abc"
    assert entry["actor"] == "authenticated"
    assert entry["tenant_id"] == "default"


@pytest.mark.anyio
async def test_access_log_rotation_called(client: AsyncClient, tmp_path: Path) -> None:
    """Middleware should call rotate_log_file before each append."""
    with patch("bernstein.core.access_log.rotate_log_file") as mock_rotate:
        await client.get("/health")
        mock_rotate.assert_called_once()
        # The argument should be the access.jsonl path
        call_path = mock_rotate.call_args[0][0]
        assert str(call_path).endswith("access.jsonl")
