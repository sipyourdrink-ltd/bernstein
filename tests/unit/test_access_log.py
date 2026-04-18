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
from starlette.requests import Request

from bernstein.core.server import create_app
from bernstein.core.server.access_log import StructuredAccessLogMiddleware


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
    """Middleware should call rotate_log_file on the first request it handles."""
    with patch("bernstein.core.server.access_log.rotate_log_file") as mock_rotate:
        await client.get("/health")
        mock_rotate.assert_called_once()
        # The argument should be the access.jsonl path
        call_path = mock_rotate.call_args[0][0]
        assert str(call_path).endswith("access.jsonl")


@pytest.mark.anyio
async def test_access_log_rotation_debounced_across_many_requests(tmp_path: Path) -> None:
    """100 requests within the debounce window must only probe rotation once.

    This validates the audit-080 fix: ``rotate_log_file`` (which calls
    ``os.stat``) must be invoked once for the first request and then skipped
    for subsequent requests until either the time window elapses or the
    in-memory byte counter crosses the rotation threshold.
    """

    log_path = tmp_path / "access.jsonl"

    async def _noop(scope: dict[str, object], receive: object, send: object) -> None:
        return None

    middleware = StructuredAccessLogMiddleware(_noop, log_path=log_path)

    async def _call_next(_request: Request) -> object:
        class _Resp:
            status_code = 200
            headers: dict[str, str] = {}

            def setdefault(self, key: str, value: str) -> None:
                self.headers.setdefault(key, value)

        resp = _Resp()
        # Starlette response stub — just needs ``headers.setdefault`` and ``status_code``.
        resp.headers = _HeaderDict()
        return resp

    class _HeaderDict(dict[str, str]):
        def setdefault(self, key: str, value: str) -> str:  # type: ignore[override]
            return super().setdefault(key, value)

    def _build_request() -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
        }
        return Request(scope)  # type: ignore[arg-type]

    with patch("bernstein.core.server.access_log.rotate_log_file", return_value=False) as mock_rotate:
        for _ in range(100):
            await middleware.dispatch(_build_request(), _call_next)

        assert mock_rotate.call_count == 1

    await middleware.aclose()

    # All 100 payloads must have landed in the log.
    contents = log_path.read_text(encoding="utf-8").splitlines()
    assert len(contents) == 100


@pytest.mark.anyio
async def test_access_log_rotation_triggers_at_byte_threshold(tmp_path: Path) -> None:
    """Crossing the in-memory byte threshold must re-probe rotation."""

    log_path = tmp_path / "access.jsonl"

    async def _noop(scope: dict[str, object], receive: object, send: object) -> None:
        return None

    class _HeaderDict(dict[str, str]):
        def setdefault(self, key: str, value: str) -> str:  # type: ignore[override]
            return super().setdefault(key, value)

    class _Resp:
        status_code = 200

        def __init__(self) -> None:
            self.headers = _HeaderDict()

    async def _call_next(_request: Request) -> object:
        return _Resp()

    def _build_request() -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
        }
        return Request(scope)  # type: ignore[arg-type]

    # Tiny threshold so one request + one payload write crosses it.
    middleware = StructuredAccessLogMiddleware(
        _noop,
        log_path=log_path,
        rotate_interval_seconds=3600.0,  # effectively disable time-based probe
        rotate_bytes_threshold=64,
    )

    with patch("bernstein.core.server.access_log.rotate_log_file", return_value=False) as mock_rotate:
        # First request: probes once (first_call path).
        await middleware.dispatch(_build_request(), _call_next)
        assert mock_rotate.call_count == 1

        # Subsequent requests keep appending; once cumulative bytes exceed 64
        # bytes the threshold triggers another probe.
        for _ in range(10):
            await middleware.dispatch(_build_request(), _call_next)

        assert mock_rotate.call_count >= 2

    await middleware.aclose()


@pytest.mark.anyio
async def test_access_log_handle_reused_across_requests(tmp_path: Path) -> None:
    """The append-mode file handle must be reused — not reopened per request."""

    log_path = tmp_path / "access.jsonl"

    async def _noop(scope: dict[str, object], receive: object, send: object) -> None:
        return None

    class _HeaderDict(dict[str, str]):
        def setdefault(self, key: str, value: str) -> str:  # type: ignore[override]
            return super().setdefault(key, value)

    class _Resp:
        status_code = 200

        def __init__(self) -> None:
            self.headers = _HeaderDict()

    async def _call_next(_request: Request) -> object:
        return _Resp()

    def _build_request() -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "query_string": b"",
        }
        return Request(scope)  # type: ignore[arg-type]

    middleware = StructuredAccessLogMiddleware(_noop, log_path=log_path)

    with patch("bernstein.core.server.access_log.rotate_log_file", return_value=False):
        await middleware.dispatch(_build_request(), _call_next)
        first_handle = middleware._log_fh
        await middleware.dispatch(_build_request(), _call_next)
        assert middleware._log_fh is first_handle

    await middleware.aclose()
    assert middleware._log_fh is None
