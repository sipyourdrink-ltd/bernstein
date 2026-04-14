"""Tests for initialization timeout guard — bootstrap.with_init_timeout."""

from __future__ import annotations

import asyncio

import pytest
from bernstein.core.bootstrap import (
    INIT_TIMEOUT_SECONDS,
    with_init_timeout,
)


class TestWithInitTimeout:
    """Tests for the with_init_timeout initialization guard (T768)."""

    @pytest.mark.asyncio
    async def test_returns_result_when_fast(self) -> None:
        """Awaitable completes successfully within timeout."""
        result = await with_init_timeout(asyncio.sleep(0, result=42))
        assert result == 42

    @pytest.mark.asyncio
    async def test_raises_timeout_on_slow_awaitable(self) -> None:
        """Exceeding the timeout raises TimeoutError."""
        with pytest.raises(TimeoutError):
            await with_init_timeout(asyncio.sleep(10.0), timeout=0.05)

    @pytest.mark.asyncio
    async def test_custom_context_in_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Timeout log includes the custom context string."""
        with pytest.raises(TimeoutError):
            await with_init_timeout(
                asyncio.sleep(10.0),
                timeout=0.05,
                context="posting tasks from plan file",
            )
        assert "posting tasks from plan file" in caplog.text

    @pytest.mark.asyncio
    async def test_default_timeout_is_30_seconds(self) -> None:
        """Default timeout parameter is 30 seconds."""
        assert pytest.approx(30.0) == INIT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_does_not_intercept_non_timeout_errors(self) -> None:
        """Non-timeout exceptions are re-raised unchanged."""
        with pytest.raises(ValueError, match="boom"):
            await with_init_timeout(_async_raise(ValueError("boom")))


async def _async_raise(exc: Exception) -> None:
    """Helper: an awaitable that immediately raises."""
    await asyncio.sleep(0)
    raise exc


class TestInitTimeoutInBootstrap:
    """Verify that the bootstrap code wraps initialization with the timeout."""

    def test_post_all_is_wrapped(self) -> None:
        """The _post_all() async block is wrapped with with_init_timeout."""
        # Read the source to confirm the function call is present.
        import inspect

        from bernstein.core.orchestration.bootstrap import _post_plan_tasks

        source = inspect.getsource(_post_plan_tasks)
        assert "with_init_timeout" in source
        assert "context=" in source
