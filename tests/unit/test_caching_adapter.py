"""Unit tests for CachingAdapter prompt-prefix caching and response reuse wrapper."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.caching_adapter import CachingAdapter
from bernstein.core.models import ModelConfig
from bernstein.core.semantic_cache import ResponseCacheManager


@pytest.fixture
def mock_inner() -> MagicMock:
    """Mock CLIAdapter that records spawn calls."""
    inner = MagicMock(spec=CLIAdapter)
    inner.name.return_value = "mock-adapter"
    inner.spawn.return_value = SpawnResult(pid=1234, log_path="/tmp/agent.log")
    return inner


@pytest.fixture
def adapter(mock_inner: MagicMock, tmp_path: Path) -> CachingAdapter:
    """CachingAdapter instance with temp workdir."""
    return CachingAdapter(mock_inner, tmp_path)


def test_cache_miss_delegates_to_inner(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that a cache miss calls the inner adapter's spawn."""
    res = adapter.spawn(
        prompt="Initial task",
        workdir=Path("/tmp"),
        model_config=ModelConfig(model="sonnet"),
        session_id="session-1",
    )

    assert res.pid == 1234
    assert mock_inner.spawn.call_count == 1


def test_cache_hit_returns_pid_0_without_inner_call(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that a verified cache hit returns PID 0 and skips inner spawn."""
    prompt = "Identical task content"
    
    # 1. First run: cache miss, stores result (if we manually store it)
    adapter._response_cache.store(
        adapter._response_cache.task_key("mock-adapter", prompt[:100], prompt),
        "Result summary",
        verified=True,
    )
    adapter._response_cache.save()

    # 2. Second run: should be a hit
    res = adapter.spawn(
        prompt=prompt,
        workdir=Path("/tmp"),
        model_config=ModelConfig(model="sonnet"),
        session_id="session-2",
    )

    assert res.pid == 0
    # Inner spawn should NOT have been called (it was only called by Orchestrator usually, 
    # but here we are testing the adapter's own bypass).
    assert mock_inner.spawn.call_count == 0


def test_ttl_expiry_causes_re_delegation(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that cache hits expire after TTL."""
    prompt = "Temporary task"
    
    # Create short-lived adapter
    adapter = CachingAdapter(mock_inner, adapter._workdir, ttl_seconds=1)
    
    # 1. Store verified entry
    adapter._response_cache.store(
        adapter._response_cache.task_key("mock-adapter", prompt[:100], prompt),
        "Result summary",
        verified=True,
    )
    adapter._response_cache.save()

    # 2. Immediate hit
    res1 = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=ModelConfig(model="s"), session_id="s1")
    assert res1.pid == 0
    assert mock_inner.spawn.call_count == 0

    # 3. Wait for TTL expiry
    time.sleep(1.1)
    
    # 4. Should be a miss now
    res2 = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=ModelConfig(model="s"), session_id="s2")
    assert res2.pid == 1234
    assert mock_inner.spawn.call_count == 1


def test_unverified_cache_entry_is_ignored(adapter: CachingAdapter, mock_inner: MagicMock) -> None:
    """Verify that unverified cache entries (failed/in-progress) are ignored."""
    prompt = "Unverified task"
    
    # Store unverified entry
    adapter._response_cache.store(
        adapter._response_cache.task_key("mock-adapter", prompt[:100], prompt),
        "Failed summary",
        verified=False,
    )
    adapter._response_cache.save()

    # Should still delegate to inner adapter
    res = adapter.spawn(prompt=prompt, workdir=Path("/tmp"), model_config=ModelConfig(model="s"), session_id="s1")
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
