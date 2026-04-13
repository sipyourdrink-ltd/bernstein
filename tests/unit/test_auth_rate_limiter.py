"""Tests for auth endpoint rate limiting."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.auth_rate_limiter import AuthRateLimiter, RequestRateLimitMiddleware
from bernstein.core.seed import RateLimitBucketConfig, RateLimitConfig, SeedConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
        limiter._hits["old-ip"] = [0.0]
        # Next check triggers cleanup
        limiter.check("new-ip")
        assert "old-ip" not in limiter._hits

    def test_retry_after_is_positive(self) -> None:
        limiter = AuthRateLimiter(max_requests=1, window_seconds=60)
        limiter.check("5.5.5.5")
        retry = limiter.check("5.5.5.5")
        assert retry is not None
        assert retry > 0


class TestAuthRateLimiterHTTP:
    """Test the rate limiter through the FastAPI dependency."""

    def test_returns_429_with_retry_after_header(self) -> None:
        from bernstein.core.auth_rate_limiter import AuthRateLimiter, check_auth_rate_limit
        from fastapi import APIRouter, Depends, FastAPI
        from starlette.testclient import TestClient

        # Patch the module-level limiter with a low-limit one for testing
        import bernstein.core.security.auth_rate_limiter as mod

        original = mod._auth_limiter
        mod._auth_limiter = AuthRateLimiter(max_requests=3, window_seconds=60)
        try:
            app = FastAPI()
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
        finally:
            mod._auth_limiter = original


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
        # Write limit=1, read limit=3 — reads should not be blocked after 2 writes
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

        transport = ASGITransport(app=app, client=("198.51.100.8", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp_events = await client.get("/events")
            resp_cost = await client.get("/events/cost")

        assert resp_events.status_code == 429
        assert resp_events.json()["bucket"] == "sse"
        assert "Retry-After" in resp_events.headers
        assert resp_cost.status_code == 429
        assert resp_cost.json()["bucket"] == "sse"
