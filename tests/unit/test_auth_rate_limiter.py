"""Tests for auth endpoint rate limiting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from bernstein.core.auth_rate_limiter import (
    AuthRateLimiter,
    RequestRateLimitMiddleware,
    _request_client_id,
)
from bernstein.core.seed import RateLimitBucketConfig, RateLimitConfig, SeedConfig
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from bernstein.core.server import create_app


class TestAuthRateLimiter:
    def test_allows_up_to_max_requests(self) -> None:
        limiter = AuthRateLimiter(max_requests=10, window_seconds=60)
        for _ in range(10):
            assert limiter.check("1.2.3.4") is None

    def test_blocks_after_max_requests(self) -> None:
        limiter = AuthRateLimiter(max_requests=10, window_seconds=60)
        for _ in range(10):
            limiter.check("1.2.3.4")

        retry_after = limiter.check("1.2.3.4")
        assert retry_after is not None
        assert retry_after >= 1.0

    def test_11th_request_returns_retry_after(self) -> None:
        """AC-5: 11th request within 60s returns 429-equivalent."""
        limiter = AuthRateLimiter(max_requests=10, window_seconds=60)
        for i in range(11):
            result = limiter.check("10.0.0.1")
            if i < 10:
                assert result is None, f"Request {i + 1} should be allowed"
            else:
                assert result is not None, "11th request must be blocked"
                assert result >= 1.0

    def test_different_ips_tracked_independently(self) -> None:
        limiter = AuthRateLimiter(max_requests=2, window_seconds=60)
        assert limiter.check("1.1.1.1") is None
        assert limiter.check("1.1.1.1") is None
        assert limiter.check("1.1.1.1") is not None  # blocked

        # Different IP still allowed
        assert limiter.check("2.2.2.2") is None

    def test_cleanup_removes_expired_entries(self) -> None:
        limiter = AuthRateLimiter(max_requests=5, window_seconds=60, cleanup_every=1)
        limiter.check("old-ip")
        # Manually expire the timestamps
        limiter._hits[("auth", "old-ip")] = [0.0]
        # Next check triggers cleanup
        limiter.check("new-ip")
        assert ("auth", "old-ip") not in limiter._hits

    def test_retry_after_is_positive(self) -> None:
        limiter = AuthRateLimiter(max_requests=1, window_seconds=60)
        limiter.check("5.5.5.5")
        retry = limiter.check("5.5.5.5")
        assert retry is not None
        assert retry > 0

    def test_failure_backoff_is_capped_and_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bernstein.core.security.auth_rate_limiter as mod

        now = [1000.0]
        monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
        limiter = AuthRateLimiter(cleanup_every=1)
        for index in range(15):
            delay = limiter.record_failure("client")
            assert delay == min(2**index, 900)
            assert limiter.check("client") == delay
            now[0] += delay
        now[0] += 900
        assert limiter.check("client") is None
        assert limiter.record_failure("client") == 1

    def test_success_retains_attempt_count(self) -> None:
        limiter = AuthRateLimiter(max_requests=1)
        assert limiter.check("client") is None
        limiter.record_success("client")
        assert limiter.check("client") is not None

    def test_cleanup_preserves_configured_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bernstein.core.security.auth_rate_limiter as mod

        now = [1000.0]
        monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
        limiter = AuthRateLimiter(cleanup_every=1)
        assert limiter.check("client", window_seconds=120, max_requests=1) is None
        now[0] += 61
        assert limiter.check("other") is None
        assert limiter.check("client", window_seconds=120, max_requests=1) == 120

    def test_cleanup_drops_idle_client_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bernstein.core.security.auth_rate_limiter as mod

        now = [1000.0]
        monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
        limiter = AuthRateLimiter(max_requests=1, cleanup_every=1)
        assert limiter.check("client") is None
        assert limiter.check("client") == 60
        now[0] += 60
        assert limiter.check("client") is None
        assert limiter.check("client") == 120
        now[0] += 61
        assert limiter.check("other") is None
        assert limiter.check("client") == 59
        now[0] += 960
        assert limiter.check("other") is None
        key = ("auth", "client")
        assert key not in limiter._hits
        assert key not in limiter._backoff
        assert key not in limiter._windows


class TestAuthRateLimiterHTTP:
    """Test the rate limiter through the FastAPI dependency."""

    def test_returns_429_with_retry_after_header(self) -> None:
        from bernstein.core.auth_rate_limiter import AuthRateLimiter, check_auth_rate_limit
        from fastapi import APIRouter, Depends, FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        app.state.auth_rate_limiter = AuthRateLimiter(max_requests=3, window_seconds=60)
        rt = APIRouter(
            prefix="/auth",
            dependencies=[Depends(check_auth_rate_limit)],
        )

        @rt.get("/test")
        async def test_endpoint() -> dict[str, str]:
            return {"status": "ok"}

        app.include_router(rt)
        client = TestClient(app)

        for _ in range(3):
            resp = client.get("/auth/test")
            assert resp.status_code == 200

        resp = client.get("/auth/test")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) >= 1


class TestRequestRateLimitMiddleware:
    """Test generic request bucket enforcement through the app middleware."""

    @pytest.mark.anyio
    async def test_tasks_bucket_returns_429_with_retry_after(self, tmp_path: Path) -> None:
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        app.state.seed_config = SeedConfig(
            goal="Test",
            rate_limit=RateLimitConfig(
                buckets=(RateLimitBucketConfig(name="tasks", requests=2, path_prefixes=("/tasks",)),)
            ),
        )
        transport = ASGITransport(app=app, client=("198.51.100.4", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/tasks")).status_code == 200
            assert (await client.get("/tasks")).status_code == 200
            response = await client.get("/tasks")

        assert response.status_code == 429
        assert response.json()["bucket"] == "tasks"
        assert int(response.headers["Retry-After"]) >= 1

    @pytest.mark.anyio
    async def test_auth_bucket_uses_method_scoping(self, tmp_path: Path) -> None:
        del tmp_path
        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware)
        app.state.seed_config = SeedConfig(
            goal="Test",
            rate_limit=RateLimitConfig(
                buckets=(RateLimitBucketConfig(name="auth", requests=1, path_prefixes=("/auth",), methods=("POST",)),)
            ),
        )

        @app.get("/auth/providers")
        async def providers() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/auth/cli/device")
        async def device() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("198.51.100.5", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            providers = await client.get("/auth/providers")
            first_post = await client.post("/auth/cli/device")
            second_post = await client.post("/auth/cli/device")

        assert providers.status_code == 200
        assert first_post.status_code == 200
        assert second_post.status_code == 429

    @pytest.mark.anyio
    async def test_default_write_limit_blocks_after_threshold(self) -> None:
        """Default write bucket enforces 30 req/min for POST/PUT/DELETE."""
        from bernstein.core.auth_rate_limiter import RequestRateLimiter

        app = FastAPI()
        limiter = RequestRateLimiter()
        app.add_middleware(RequestRateLimitMiddleware, limiter=limiter, write_rpm=2, read_rpm=300)
        app.state.seed_config = None

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("198.51.100.6", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/data")
            second = await client.post("/data")
            third = await client.post("/data")

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert "Retry-After" in third.headers
        assert third.json()["bucket"] == "default_write"

    @pytest.mark.anyio
    async def test_default_read_allows_more_than_write(self) -> None:
        """Default read bucket is more permissive than write bucket."""
        from bernstein.core.auth_rate_limiter import RequestRateLimiter

        app = FastAPI()
        limiter = RequestRateLimiter()
        # Write limit=1, read limit=3 - reads should not be blocked after 2 writes
        app.add_middleware(RequestRateLimitMiddleware, limiter=limiter, write_rpm=1, read_rpm=3)
        app.state.seed_config = None

        @app.get("/data")
        async def read_data() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("198.51.100.7", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust write limit
            await client.post("/data")
            blocked_write = await client.post("/data")
            # Reads should still work
            first_read = await client.get("/data")
            second_read = await client.get("/data")

        assert blocked_write.status_code == 429
        assert first_read.status_code == 200
        assert second_read.status_code == 200

    @pytest.mark.anyio
    async def test_sse_concurrency_limit_rejects_excess_connections(self) -> None:
        """SSE /events endpoint enforces max_concurrent connection limit (429 when at cap)."""
        from bernstein.core.auth_rate_limiter import RequestRateLimiter

        # Setting sse_max_concurrent=0 simulates already-at-cap state
        app = FastAPI()
        rl = RequestRateLimiter()
        app.add_middleware(RequestRateLimitMiddleware, limiter=rl, sse_max_concurrent=0)
        app.state.seed_config = None

        @app.get("/events")
        async def events() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/events/cost")
        async def cost_events() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/events/thread")
        async def thread_events() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("198.51.100.8", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp_events = await client.get("/events")
            resp_cost = await client.get("/events/cost")
            resp_thread = await client.get("/events/thread")

        assert resp_events.status_code == 429
        assert resp_events.json()["bucket"] == "sse"
        assert "Retry-After" in resp_events.headers
        assert resp_cost.status_code == 429
        assert resp_cost.json()["bucket"] == "sse"
        assert resp_thread.status_code == 429
        assert resp_thread.json()["bucket"] == "sse"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/events", "/events/cost", "/events/thread"])
    @pytest.mark.parametrize("termination", ["finish", "disconnect", "cancel", "error"])
    async def test_sse_cap_holds_until_stream_closes(self, path: str, termination: str) -> None:
        """Two open bodies reject a third stream; closing one admits a replacement."""
        app = FastAPI()
        finishes: list[asyncio.Event] = []

        async def body() -> AsyncIterator[bytes]:
            finish = asyncio.Event()
            finishes.append(finish)
            yield b"data: ready\n\n"
            await finish.wait()
            if termination == "error":
                raise RuntimeError("stream failed")

        async def events() -> StreamingResponse:
            return StreamingResponse(body(), media_type="text/event-stream")

        for route in ("/events", "/events/cost", "/events/thread"):
            app.add_api_route(route, events, methods=["GET"])
        middleware = RequestRateLimitMiddleware(app, sse_max_concurrent=2)
        exchanges: list[_OpenSSE] = []

        async def open_stream(route: str) -> _OpenSSE:
            exchange = _OpenSSE(middleware, route)
            exchanges.append(exchange)
            await asyncio.wait_for(exchange.ready.wait(), timeout=5)
            return exchange

        try:
            first = await open_stream(path)
            second = await open_stream("/events/thread")
            assert first.status == second.status == 200
            assert not first.task.done() and not second.task.done()

            excess = await open_stream(path)
            assert excess.status == 429
            assert excess.headers[b"retry-after"] == b"5"
            assert middleware.sse_connections == 2
            await asyncio.wait_for(excess.task, timeout=5)

            if termination == "disconnect":
                first.disconnect.set()
                await asyncio.wait_for(first.task, timeout=5)
            elif termination == "cancel":
                first.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first.task
            else:
                finishes[0].set()
                if termination == "error":
                    with pytest.raises(RuntimeError, match="stream failed"):
                        await asyncio.wait_for(first.task, timeout=5)
                else:
                    await asyncio.wait_for(first.task, timeout=5)

            assert middleware.sse_connections == 1
            replacement = await open_stream(path)
            assert replacement.status == 200
        finally:
            for exchange in exchanges:
                exchange.task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*(exchange.task for exchange in exchanges), return_exceptions=True), timeout=5
            )
        assert middleware.sse_connections == 0


class _OpenSSE:
    """Drive a real streaming response without buffering it like ASGITransport."""

    def __init__(self, app: RequestRateLimitMiddleware, path: str) -> None:
        self.ready = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.status = 0
        self.headers: dict[bytes, bytes] = {}
        self._sent_request = False
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("198.51.100.8", 123),
            "server": ("test", 80),
        }
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self) -> Message:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = dict(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.ready.set()


class TestVersionedRateLimitAliases:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/events", "/events/cost", "/events/thread"])
    @pytest.mark.parametrize(
        "first_prefix,second_prefix", [("", ""), ("", "/api/v1"), ("/api/v1", ""), ("/api/v1", "/api/v1")]
    )
    async def test_sse_aliases_share_connection_cap(self, path: str, first_prefix: str, second_prefix: str) -> None:
        app = FastAPI()
        finishes: list[asyncio.Event] = []

        async def body() -> AsyncIterator[bytes]:
            finish = asyncio.Event()
            finishes.append(finish)
            yield b"data: ready\n\n"
            await finish.wait()

        async def events() -> StreamingResponse:
            return StreamingResponse(body(), media_type="text/event-stream")

        for route in (path, "/api/v1" + path):
            app.add_api_route(route, events, methods=["GET"])
        middleware = RequestRateLimitMiddleware(app, sse_max_concurrent=1)
        exchanges: list[_OpenSSE] = []

        async def with_app(scope: Scope, receive: Receive, send: Send) -> None:
            # FastAPI normally sets app before invoking its middleware stack.
            scope["app"] = app
            await middleware(scope, receive, send)

        async def open_stream(route: str) -> _OpenSSE:
            exchange = _OpenSSE(with_app, route)  # type: ignore[arg-type]
            exchanges.append(exchange)
            await asyncio.wait_for(exchange.ready.wait(), timeout=5)
            return exchange

        try:
            first = await open_stream(first_prefix + path)
            assert first.status == 200
            assert not first.task.done()
            assert middleware.sse_connections == 1

            excess = await open_stream(second_prefix + path)
            assert excess.status == 429
            assert excess.headers[b"retry-after"] == b"5"
            assert not first.task.done()
            assert middleware.sse_connections == 1
            await asyncio.wait_for(excess.task, timeout=5)

            finishes[0].set()
            await asyncio.wait_for(first.task, timeout=5)
            assert middleware.sse_connections == 0
            replacement = await open_stream(second_prefix + path)
            assert replacement.status == 200
            assert not replacement.task.done()
            assert middleware.sse_connections == 1
        finally:
            for exchange in exchanges:
                exchange.task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*(exchange.task for exchange in exchanges), return_exceptions=True), timeout=5
            )
        assert middleware.sse_connections == 0

    @pytest.mark.anyio
    @pytest.mark.parametrize("path,method", [("/tasks", "GET"), ("/auth/cli/device", "POST")])
    @pytest.mark.parametrize("first_prefix,second_prefix", [("", "/api/v1"), ("/api/v1", "")])
    @pytest.mark.parametrize("fallback_prefix", [None, "/", "/api/v1"])
    async def test_seed_bucket_is_shared_by_versioned_alias(
        self, path: str, method: str, first_prefix: str, second_prefix: str, fallback_prefix: str | None
    ) -> None:
        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware)
        buckets = (RateLimitBucketConfig(name="shared", requests=1, path_prefixes=(path,), methods=(method,)),)
        if fallback_prefix is not None:
            buckets += (RateLimitBucketConfig(name="fallback", requests=10, path_prefixes=(fallback_prefix,)),)
        app.state.seed_config = SeedConfig(
            goal="Test",
            rate_limit=RateLimitConfig(buckets=buckets),
        )

        async def endpoint() -> dict[str, str]:
            return {"status": "ok"}

        app.add_api_route(path, endpoint, methods=[method])
        app.add_api_route("/api/v1" + path, endpoint, methods=[method])
        transport = ASGITransport(app=app, client=("198.51.100.9", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.request(method, first_prefix + path)).status_code == 200
            response = await client.request(method, second_prefix + path)

        assert response.status_code == 429
        assert response.json()["bucket"] == "shared"
        assert int(response.headers["Retry-After"]) >= 1

    @pytest.mark.anyio
    async def test_explicit_versioned_seed_bucket_is_preserved(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware)
        app.state.seed_config = SeedConfig(
            goal="Test",
            rate_limit=RateLimitConfig(
                buckets=(RateLimitBucketConfig(name="versioned", requests=1, path_prefixes=("/api/v1/tasks",)),)
            ),
        )

        @app.get("/api/v1/tasks")
        async def tasks() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("198.51.100.10", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/tasks")).status_code == 200
            response = await client.get("/api/v1/tasks")

        assert response.status_code == 429
        assert response.json()["bucket"] == "versioned"


class _FakeClient:
    """Minimal stand-in for ``request.client`` in unit tests."""

    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for ``fastapi.Request`` that exposes ``client`` + ``headers``."""

    def __init__(self, host: str, headers: dict[str, str] | None = None) -> None:
        self.client = _FakeClient(host)
        self.headers = headers or {}


class TestRateLimitKeyingAgainstHeaderSpoofing:
    """audit-049: rate-limit buckets must key on real peer, not spoofable headers."""

    def test_default_ignores_x_forwarded_for_and_uses_real_peer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without BERNSTEIN_TRUSTED_PROXY_IPS set, XFF is fully ignored."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        # Attacker cycles XFF values. Each call *must* be keyed on the real
        # peer (203.0.113.99), not the attacker-controlled header.
        request_a = _FakeRequest("203.0.113.99", {"X-Forwarded-For": "1.1.1.1"})
        request_b = _FakeRequest("203.0.113.99", {"X-Forwarded-For": "2.2.2.2"})
        request_c = _FakeRequest("203.0.113.99", {"X-Forwarded-For": "3.3.3.3"})

        assert _request_client_id(request_a) == "203.0.113.99"  # type: ignore[arg-type]
        assert _request_client_id(request_b) == "203.0.113.99"  # type: ignore[arg-type]
        assert _request_client_id(request_c) == "203.0.113.99"  # type: ignore[arg-type]

    def test_default_loopback_does_not_implicitly_trust_xff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback peer with XFF still keys on 127.0.0.1 unless explicitly trusted."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)
        request = _FakeRequest("127.0.0.1", {"X-Forwarded-For": "9.9.9.9"})
        assert _request_client_id(request) == "127.0.0.1"  # type: ignore[arg-type]

    def test_trusted_proxy_uses_rightmost_non_trusted_hop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When peer is a trusted proxy, the right-most non-trusted hop is used."""
        monkeypatch.setenv("BERNSTEIN_TRUSTED_PROXY_IPS", "10.0.0.1,10.0.0.2")

        # Chain: client -> trusted proxy2 -> trusted proxy1 -> us.
        # XFF from the trusted chain: "203.0.113.5, 10.0.0.2" (proxy1 is the
        # peer, so it doesn't appear in XFF). Right-most non-trusted is the
        # original client.
        request = _FakeRequest("10.0.0.1", {"X-Forwarded-For": "203.0.113.5, 10.0.0.2"})
        assert _request_client_id(request) == "203.0.113.5"  # type: ignore[arg-type]

    def test_trusted_proxy_single_hop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_TRUSTED_PROXY_IPS", "10.0.0.1")
        request = _FakeRequest("10.0.0.1", {"X-Forwarded-For": "198.51.100.42"})
        assert _request_client_id(request) == "198.51.100.42"  # type: ignore[arg-type]

    def test_untrusted_peer_xff_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Peer not in trusted list: XFF is ignored even when env is configured."""
        monkeypatch.setenv("BERNSTEIN_TRUSTED_PROXY_IPS", "10.0.0.1")
        # Attacker host 203.0.113.77 tries to spoof XFF.
        request = _FakeRequest("203.0.113.77", {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
        assert _request_client_id(request) == "203.0.113.77"  # type: ignore[arg-type]

    def test_trusted_chain_with_only_trusted_hops_falls_back_to_peer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If every XFF hop is a trusted proxy, fall back to the direct peer IP."""
        monkeypatch.setenv("BERNSTEIN_TRUSTED_PROXY_IPS", "10.0.0.1,10.0.0.2,10.0.0.3")
        request = _FakeRequest("10.0.0.1", {"X-Forwarded-For": "10.0.0.2, 10.0.0.3"})
        assert _request_client_id(request) == "10.0.0.1"  # type: ignore[arg-type]

    def test_bucket_resets_after_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A full bucket reopens when both the window and lockout expire."""
        import bernstein.core.security.auth_rate_limiter as mod

        now = [1000.0]
        monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
        limiter = AuthRateLimiter(max_requests=2, window_seconds=60)
        assert limiter.check("203.0.113.10") is None
        assert limiter.check("203.0.113.10") is None
        assert limiter.check("203.0.113.10") is not None
        now[0] += 60
        assert limiter.check("203.0.113.10") is None

    @pytest.mark.anyio
    async def test_middleware_rotating_xff_cannot_bypass_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: attacker rotates XFF per request, all must share one bucket."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=2, read_rpm=300)
        app.state.seed_config = None

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("203.0.113.200", 40000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/data", headers={"X-Forwarded-For": "1.1.1.1"})
            second = await client.post("/data", headers={"X-Forwarded-For": "2.2.2.2"})
            third = await client.post("/data", headers={"X-Forwarded-For": "3.3.3.3"})

        assert first.status_code == 200
        assert second.status_code == 200
        # All three requests share the real-peer bucket; third is blocked.
        assert third.status_code == 429
        assert third.json()["bucket"] == "default_write"

    @pytest.mark.anyio
    async def test_middleware_loopback_with_xff_is_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback peer carrying XFF is treated as a proxied external caller."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=1, read_rpm=300)
        app.state.seed_config = None

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        # Local reverse-proxy scenario: peer is loopback but request carries
        # XFF - must NOT be exempted from rate limiting.
        transport = ASGITransport(app=app, client=("127.0.0.1", 40001))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/data", headers={"X-Forwarded-For": "8.8.8.8"})
            second = await client.post("/data", headers={"X-Forwarded-For": "8.8.8.8"})

        assert first.status_code == 200
        assert second.status_code == 429

    @pytest.mark.anyio
    async def test_middleware_pure_loopback_still_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback peer with no XFF is still exempt (orchestrator/spawner traffic)."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=1, read_rpm=300)
        app.state.seed_config = None

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("127.0.0.1", 40002))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/data")
            second = await client.post("/data")
            third = await client.post("/data")

        # All three succeed - loopback callers without XFF remain exempt.
        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 200

    @pytest.mark.anyio
    async def test_middleware_x_bernstein_internal_header_no_longer_bypasses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """audit-049 regression: X-Bernstein-Internal must NOT bypass rate limits."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=1, read_rpm=300)
        app.state.seed_config = None

        @app.post("/data")
        async def write_data() -> dict[str, str]:
            return {"status": "ok"}

        # Loopback peer + attacker-controlled XFF + forged internal header.
        # Pre-fix this combination bypassed rate limiting entirely.
        transport = ASGITransport(app=app, client=("127.0.0.1", 40003))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/data",
                headers={"X-Forwarded-For": "10.0.0.5", "X-Bernstein-Internal": "true"},
            )
            second = await client.post(
                "/data",
                headers={"X-Forwarded-For": "10.0.0.5", "X-Bernstein-Internal": "true"},
            )

        assert first.status_code == 200
        assert second.status_code == 429


class TestAgentIdBucketKey:
    """Tests for agent-id-keyed rate-limit buckets (issue #6609)."""

    def test_returns_agent_key_when_bearer_and_agent_id_present(self):
        from bernstein.core.auth_rate_limiter import _agent_bucket_key
        from fastapi import Request as FastAPIRequest

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer cluster-secret-token"),
                (b"x-bernstein-agent-id", b"agent-abc-123"),
            ],
            "client": ("10.0.0.5", 5000),
        }
        req = FastAPIRequest(scope)
        assert _agent_bucket_key(req) == "agent:agent-abc-123"

    def test_returns_none_without_authorization_header(self):
        from bernstein.core.auth_rate_limiter import _agent_bucket_key
        from fastapi import Request as FastAPIRequest

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "query_string": b"",
            "headers": [
                (b"x-bernstein-agent-id", b"agent-abc-123"),
            ],
            "client": ("10.0.0.5", 5000),
        }
        req = FastAPIRequest(scope)
        assert _agent_bucket_key(req) is None

    def test_returns_none_without_agent_id_header(self):
        from bernstein.core.auth_rate_limiter import _agent_bucket_key
        from fastapi import Request as FastAPIRequest

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer cluster-secret-token"),
            ],
            "client": ("10.0.0.5", 5000),
        }
        req = FastAPIRequest(scope)
        assert _agent_bucket_key(req) is None

    def test_returns_none_for_overlong_agent_id(self):
        from bernstein.core.auth_rate_limiter import _AGENT_ID_MAX_LEN, _agent_bucket_key
        from fastapi import Request as FastAPIRequest

        long_id = "a" * (_AGENT_ID_MAX_LEN + 1)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer cluster-secret-token"),
                (b"x-bernstein-agent-id", long_id.encode()),
            ],
            "client": ("10.0.0.5", 5000),
        }
        req = FastAPIRequest(scope)
        assert _agent_bucket_key(req) is None

    def test_non_bearer_auth_does_not_enable_agent_keying(self):
        from bernstein.core.auth_rate_limiter import _agent_bucket_key
        from fastapi import Request as FastAPIRequest

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/tasks",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Basic dXNlcjpwYXNz"),
                (b"x-bernstein-agent-id", b"agent-abc-123"),
            ],
            "client": ("10.0.0.5", 5000),
        }
        req = FastAPIRequest(scope)
        assert _agent_bucket_key(req) is None

    @pytest.mark.anyio
    async def test_two_agents_behind_same_ip_get_independent_budgets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Workers sharing an IP but carrying distinct agent-ids have separate buckets."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=1, read_rpm=300)
        app.state.seed_config = None

        @app.post("/tasks")
        async def create_task() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("10.0.0.5", 5000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/tasks",
                headers={
                    "Authorization": "Bearer cluster-token",
                    "x-bernstein-agent-id": "agent-alpha",
                },
            )
            second = await client.post(
                "/tasks",
                headers={
                    "Authorization": "Bearer cluster-token",
                    "x-bernstein-agent-id": "agent-beta",
                },
            )

        assert first.status_code == 200
        assert second.status_code == 200, "distinct agent IDs must not share a rate-limit bucket"

    @pytest.mark.anyio
    async def test_same_agent_id_shares_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Requests from the same agent-id exhaust the shared bucket."""
        monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)

        app = FastAPI()
        app.add_middleware(RequestRateLimitMiddleware, write_rpm=1, read_rpm=300)
        app.state.seed_config = None

        @app.post("/tasks")
        async def create_task() -> dict[str, str]:
            return {"status": "ok"}

        transport = ASGITransport(app=app, client=("10.0.0.5", 5000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/tasks",
                headers={
                    "Authorization": "Bearer cluster-token",
                    "x-bernstein-agent-id": "agent-alpha",
                },
            )
            second = await client.post(
                "/tasks",
                headers={
                    "Authorization": "Bearer cluster-token",
                    "x-bernstein-agent-id": "agent-alpha",
                },
            )

        assert first.status_code == 200
        assert second.status_code == 429
