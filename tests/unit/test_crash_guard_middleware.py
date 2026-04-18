"""Tests for CrashGuardMiddleware (audit-124).

Covers three guarantees:

1.  SSE requests (``Accept: text/event-stream`` or ``/events`` path) are
    NOT wrapped — the exception propagates so Uvicorn closes the
    connection cleanly instead of sending a JSON 500 into an SSE stream.
2.  Non-SSE requests are converted to a JSON 500 response as before.
3.  In production (no ``BERNSTEIN_DEBUG`` env var) the log line is a
    one-line summary plus a SHA256 hash; full traceback is only emitted
    when ``BERNSTEIN_DEBUG=1``.
"""

# pyright: reportPrivateUsage=false, reportUnusedFunction=false, reportUntypedFunctionDecorator=false, reportUnknownMemberType=false

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from bernstein.core.server.server_middleware import CrashGuardMiddleware, _is_sse_request
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CrashGuardMiddleware)

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("kaboom: /secret/path/leaks")

    @app.get("/events")
    def events(request: Request) -> StreamingResponse:  # noqa: ARG001
        async def gen() -> AsyncGenerator[bytes, None]:
            yield b"event: hello\ndata: 1\n\n"
            raise RuntimeError("sse-fail: /another/secret")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def test_non_sse_request_returns_json_500() -> None:
    """Regular requests that raise continue to produce a JSON 500."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "Internal server error (crash guard caught)"}


def test_sse_request_by_path_reraises() -> None:
    """SSE endpoints (path /events) let the exception propagate."""
    client = TestClient(_build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="sse-fail"):
        # TestClient materialises the stream — the exception raised
        # inside the generator must propagate, NOT be swallowed by the
        # crash guard.
        with client.stream("GET", "/events") as response:
            for _ in response.iter_bytes():
                pass


def test_sse_request_by_accept_header_is_detected() -> None:
    """``Accept: text/event-stream`` is sufficient to skip the guard."""

    # Build a synthetic request object with just enough shape for the
    # detector — avoid a full HTTP roundtrip.
    class _URL:
        path = "/not-events"

    class _Req:
        headers = {"accept": "text/event-stream"}
        url = _URL()

    assert _is_sse_request(_Req()) is True  # type: ignore[arg-type]


def test_non_sse_accept_is_not_detected() -> None:
    class _URL:
        path = "/tasks"

    class _Req:
        headers = {"accept": "application/json"}
        url = _URL()

    assert _is_sse_request(_Req()) is False  # type: ignore[arg-type]


def test_production_mode_redacts_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without BERNSTEIN_DEBUG the log line is one line + sha256 hash."""
    monkeypatch.delenv("BERNSTEIN_DEBUG", raising=False)
    client = TestClient(_build_app(), raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="bernstein.core.server.server_middleware"):
        response = client.get("/boom", headers={"user-agent": "pytest-agent/1.0"})

    assert response.status_code == 500
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected one ERROR-level log record in production mode"
    record = error_records[0]
    msg = record.getMessage()
    # One line, no multi-line traceback
    assert "\n" not in msg
    # Includes hash tag
    assert "tb_sha256=" in msg
    # Includes client/UA enrichment
    assert "pytest-agent/1.0" in msg
    # Should NOT embed the full traceback (no "Traceback (most recent call last)")
    assert "Traceback (most recent call last)" not in msg


def test_debug_mode_emits_full_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With BERNSTEIN_DEBUG=1 logger.exception emits the full traceback."""
    monkeypatch.setenv("BERNSTEIN_DEBUG", "1")
    client = TestClient(_build_app(), raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="bernstein.core.server.server_middleware"):
        response = client.get("/boom")

    assert response.status_code == 500
    # logger.exception attaches exc_info on the record
    exception_records = [r for r in caplog.records if r.exc_info is not None]
    assert exception_records, "expected a record with exc_info in debug mode"
