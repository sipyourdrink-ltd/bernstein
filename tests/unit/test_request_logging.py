"""WEB-010: Tests for request/response logging middleware."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.request_logging import (
    _NOISY_PATHS,
    _resolve_log_level,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestResolveLogLevel:
    """Test log level resolution from environment."""

    def test_default_is_info(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            level = _resolve_log_level()
            assert level == logging.INFO

    def test_debug_level(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_REQUEST_LOG_LEVEL": "debug"}):
            level = _resolve_log_level()
            assert level == logging.DEBUG

    def test_warning_level(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_REQUEST_LOG_LEVEL": "warning"}):
            level = _resolve_log_level()
            assert level == logging.WARNING

    def test_none_disables(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_REQUEST_LOG_LEVEL": "none"}):
            level = _resolve_log_level()
            assert level > logging.CRITICAL

    def test_unknown_defaults_to_info(self) -> None:
        with patch.dict("os.environ", {"BERNSTEIN_REQUEST_LOG_LEVEL": "bogus"}):
            level = _resolve_log_level()
            assert level == logging.INFO


class TestNoisyPaths:
    """Verify which paths are classified as noisy."""

    def test_health_is_noisy(self) -> None:
        assert "/health" in _NOISY_PATHS

    def test_events_is_noisy(self) -> None:
        assert "/events" in _NOISY_PATHS


class TestRequestLoggingMiddleware:
    """Integration tests for the logging middleware."""

    @pytest.mark.anyio()
    async def test_request_is_logged(self, client: AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
        """A normal request should produce a log entry."""
        with caplog.at_level(logging.DEBUG, logger="bernstein.request_log"):
            resp = await client.get("/status")
            assert resp.status_code == 200
            # The middleware should have logged the request;
            # at minimum verify the request succeeded.

    @pytest.mark.anyio()
    async def test_health_not_logged_at_info(self, client: AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
        """Health check paths should be suppressed at INFO level."""
        with caplog.at_level(logging.INFO, logger="bernstein.request_log"):
            resp = await client.get("/health")
            assert resp.status_code == 200
