"""Unit tests for CachingAdapter prompt-prefix caching and response reuse wrapper."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import ModelConfig
from bernstein.core.semantic_cache import ResponseCacheManager

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.caching_adapter import CachingAdapter


@pytest.fixture
def mock_inner() -> MagicMock:
    """Mock CLIAdapter that records spawn calls."""
    inner = MagicMock(spec=CLIAdapter)
    inner.name.return_value = "mock-adapter"
    inner.spawn.return_value = SpawnResult(pid=1234, log_path=Path("/tmp/agent.log"))
    inner.is_rate_limited.return_value = False
    return inner


@pytest.fixture
def adapter(mock_inner: MagicMock, tmp_path: Path) -> CachingAdapter:
    """CachingAdapter instance with temp workdir."""
    return CachingAdapter(mock_inner, tmp_path)


def _model_config(model: str) -> ModelConfig:
    """Build a ModelConfig for caching-adapter tests."""

    return ModelConfig(model=model, effort="high")


def _response_cache(adapter: CachingAdapter) -> ResponseCacheManager:
    """Access the adapter response cache for white-box cache tests."""
    return cast(ResponseCacheManager, cast(Any, adapter)._response_cache)


def test_cache_miss_delegates_to_inner(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that a cache miss calls the inner adapter's spawn."""
    res = adapter.spawn(
        prompt="Initial task",
        workdir=Path("/tmp"),
        model_config=_model_config("sonnet"),
        session_id="session-1",
    )

    assert res.pid == 1234
    assert mock_inner.spawn.call_count == 1


def test_cache_hit_returns_pid_0_without_inner_call(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that a verified cache hit returns PID 0 and skips inner spawn."""
    prompt = "Identical task content"

    # 1. First run: cache miss, stores result (if we manually store it)
    cache = _response_cache(adapter)
    cache.store(
        cache.task_key("mock-adapter", prompt[:100], prompt),
        "Result summary",
        verified=True,
    )
    cache.save()

    # 2. Second run: should be a hit
    res = adapter.spawn(
        prompt=prompt,
        workdir=Path("/tmp"),
        model_config=_model_config("sonnet"),
        session_id="session-2",
    )

    assert res.pid == 0
    # Inner spawn should NOT have been called (it was only called by Orchestrator usually,
    # but here we are testing the adapter's own bypass).
    assert mock_inner.spawn.call_count == 0


def test_ttl_expiry_causes_re_delegation(tmp_path: Path, mock_inner: MagicMock) -> None:
    """Verify that cache hits expire after TTL."""
    prompt = "Temporary task"

    # Create short-lived adapter
    adapter = CachingAdapter(mock_inner, tmp_path, ttl_seconds=1)

    # 1. Store verified entry
    cache = _response_cache(adapter)
    cache.store(
        cache.task_key("mock-adapter", prompt[:100], prompt),
        "Result summary",
        verified=True,
    )
    cache.save()

    # 2. Immediate hit
    res1 = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=_model_config("s"), session_id="s1")
    assert res1.pid == 0
    assert mock_inner.spawn.call_count == 0

    # 3. Wait for TTL expiry
    time.sleep(1.1)

    # 4. Should be a miss now
    res2 = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=_model_config("s"), session_id="s2")
    assert res2.pid == 1234
    assert mock_inner.spawn.call_count == 1


def test_unverified_cache_entry_is_ignored(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that unverified cache entries (failed/in-progress) are ignored."""
    prompt = "Unverified task"

    # Store unverified entry
    cache = _response_cache(adapter)
    cache.store(
        cache.task_key("mock-adapter", prompt[:100], prompt),
        "Failed summary",
        verified=False,
    )
    cache.save()

    # Should still delegate to inner adapter
    res = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=_model_config("s"), session_id="s1")
    assert res.pid == 1234
    assert mock_inner.spawn.call_count == 1


def test_kill_delegates_to_inner_unless_pid_0(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify kill delegation logic."""
    adapter.kill(1234)
    mock_inner.kill.assert_called_with(1234)

    mock_inner.kill.reset_mock()
    adapter.kill(0)
    assert mock_inner.kill.call_count == 0


def test_is_alive_always_false_for_pid_0(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify is_alive logic for virtual PIDs."""
    assert not adapter.is_alive(0)
    assert mock_inner.is_alive.call_count == 0

    mock_inner.is_alive.return_value = True
    assert adapter.is_alive(1234)
    mock_inner.is_alive.assert_called_with(1234)


def test_spawn_forwards_budget_multiplier_and_system_addendum(
    adapter: CachingAdapter,
    mock_inner: MagicMock,
) -> None:
    """Regression for audit-129: cache miss must forward all base-interface kwargs.

    The wrapper previously dropped ``budget_multiplier`` and ``system_addendum``
    when delegating to the inner adapter, silently disabling retry budget
    scaling and role-scoped system prompt injection.
    """
    res = adapter.spawn(
        prompt="task that will miss the cache",
        workdir=Path("/tmp"),
        model_config=_model_config("sonnet"),
        session_id="audit-129",
        budget_multiplier=2.5,
        system_addendum="x",
    )

    assert res.pid == 1234
    assert mock_inner.spawn.call_count == 1

    kwargs = mock_inner.spawn.call_args.kwargs
    assert kwargs["budget_multiplier"] == 2.5
    assert kwargs["system_addendum"] == "x"
    # Sanity: other kwargs from the base interface are still forwarded.
    assert kwargs["prompt"] == "task that will miss the cache"
    assert kwargs["session_id"] == "audit-129"
    assert kwargs["task_scope"] == "medium"


def test_concurrency_safe_spawns(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that multiple threads can safely spawn through the adapter."""
    import threading

    prompt = "Shared concurrent task"
    results: list[SpawnResult] = []
    errors: list[Exception] = []

    def target() -> None:
        try:
            res = adapter.spawn(
                prompt=prompt,
                workdir=Path("/tmp"),
                model_config=_model_config("sonnet"),
                session_id=f"thread-{threading.get_ident()}",
            )
            results.append(res)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=target) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    # Should only have spawned once in the inner adapter if they all hit the cache,
    # or multiple times if they raced before first write.
    # The key is that it didn't crash.
    assert mock_inner.spawn.call_count > 0
