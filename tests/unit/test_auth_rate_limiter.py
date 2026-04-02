"""Tests for auth endpoint rate limiting."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.auth_rate_limiter import AuthRateLimiter, RequestRateLimitMiddleware
from bernstein.core.seed import RateLimitBucketConfig, RateLimitConfig, SeedConfig
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
        from fastapi import APIRouter, Depends, FastAPI
        from starlette.testclient import TestClient

        # Patch the module-level limiter with a low-limit one for testing
        import bernstein.core.auth_rate_limiter as mod
        from bernstein.core.auth_rate_limiter import AuthRateLimiter, check_auth_rate_limit

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
                buckets=(
                    RateLimitBucketConfig(name="auth", requests=1, path_prefixes=("/auth",), methods=("POST",)),
                )
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
