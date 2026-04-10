"""Tests for iframe embedding response headers."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest_asyncio.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.delenv("BERNSTEIN_FRAME_ANCESTORS", raising=False)
    app = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


@pytest.mark.asyncio
async def test_default_frame_headers_are_same_origin(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"


@pytest.mark.asyncio
async def test_custom_frame_ancestors_omit_x_frame_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BERNSTEIN_FRAME_ANCESTORS", "'self' https://portal.example.com")
    app = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    assert response.headers["content-security-policy"] == "frame-ancestors 'self' https://portal.example.com"
