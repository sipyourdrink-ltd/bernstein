"""Unit tests for CachingAdapter prompt-prefix caching wrapper."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.caching_adapter import CachingAdapter
from bernstein.core.models import ModelConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAdapter(CLIAdapter):
    """Minimal concrete adapter for testing."""

    def __init__(self) -> None:
        self.spawn_calls: list[str] = []

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict | None = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        self.spawn_calls.append(prompt)
        return SpawnResult(pid=42, log_path=workdir / "log.txt")

    def name(self) -> str:
        return "fake"

    def is_alive(self, pid: int) -> bool:
        return pid == 42

    def kill(self, pid: int) -> None:
        pass

    def detect_tier(self) -> None:
        return None


def _spawn(adapter: CachingAdapter, prompt: str, tmp_path: Path) -> SpawnResult:
    """Shorthand to call spawn with defaults."""
    return adapter.spawn(
        prompt=prompt,
        workdir=tmp_path,
        model_config=ModelConfig(model="sonnet", effort="normal"),
        session_id="test-session",
    )


# Prompts with different system prefixes (split on "## Assigned tasks")
PROMPT_A = "You are a backend engineer.\n## Assigned tasks\nFix the login bug"
PROMPT_B = "You are a QA engineer.\n## Assigned tasks\nFix the login bug"
PROMPT_A_TASK2 = "You are a backend engineer.\n## Assigned tasks\nAdd unit tests"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCachingAdapterDelegation:
    """CachingAdapter delegates all CLIAdapter methods to the inner adapter."""

    def test_spawn_delegates_to_inner(self, tmp_path: Path) -> None:
        """Spawn always calls the inner adapter and returns its result."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        result = _spawn(adapter, PROMPT_A, tmp_path)

        assert len(inner.spawn_calls) == 1
        assert inner.spawn_calls[0] == PROMPT_A
        assert result.pid == 42

    def test_name_delegates(self, tmp_path: Path) -> None:
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)
        assert adapter.name() == "fake"

    def test_is_alive_delegates(self, tmp_path: Path) -> None:
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)
        assert adapter.is_alive(42) is True
        assert adapter.is_alive(99) is False

    def test_kill_delegates(self, tmp_path: Path) -> None:
        inner = MagicMock(spec=CLIAdapter)
        adapter = CachingAdapter(inner, tmp_path)
        adapter.kill(42)
        inner.kill.assert_called_once_with(42)

    def test_detect_tier_delegates(self, tmp_path: Path) -> None:
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)
        assert adapter.detect_tier() is None


class TestCachingAdapterCacheBehavior:
    """CachingAdapter tracks prompt prefix reuse via PromptCachingManager."""

    def test_cache_miss_delegates_to_inner(self, tmp_path: Path) -> None:
        """First call with a new prompt prefix is a cache miss; inner adapter is called."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        result = _spawn(adapter, PROMPT_A, tmp_path)

        assert len(inner.spawn_calls) == 1
        assert result.pid == 42

    def test_cache_hit_still_delegates(self, tmp_path: Path) -> None:
        """Second call with same prefix is a cache hit; inner adapter is still called.

        The CachingAdapter caches prompt prefixes for tracking, not results.
        """
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        _spawn(adapter, PROMPT_A, tmp_path)
        _spawn(adapter, PROMPT_A, tmp_path)

        assert len(inner.spawn_calls) == 2

    def test_same_prefix_different_task_reuses_cache(self, tmp_path: Path) -> None:
        """Same system prefix with different tasks shares cache entry."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        _spawn(adapter, PROMPT_A, tmp_path)
        _spawn(adapter, PROMPT_A_TASK2, tmp_path)

        # Both use the same prefix ("You are a backend engineer.")
        mgr = adapter._caching_mgr
        assert len(mgr._manifest.entries) == 1
        entry = next(iter(mgr._manifest.entries.values()))
        assert entry.hit_count == 1  # second call incremented

    def test_different_prefix_creates_separate_cache_entry(self, tmp_path: Path) -> None:
        """Different system prefixes get separate cache keys."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        _spawn(adapter, PROMPT_A, tmp_path)
        _spawn(adapter, PROMPT_B, tmp_path)

        mgr = adapter._caching_mgr
        assert len(mgr._manifest.entries) == 2

    def test_manifest_saved_after_spawn(self, tmp_path: Path) -> None:
        """save_manifest is called after each spawn."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        with patch.object(adapter._caching_mgr, "save_manifest") as mock_save:
            _spawn(adapter, PROMPT_A, tmp_path)
            mock_save.assert_called_once()

    def test_manifest_persisted_to_disk(self, tmp_path: Path) -> None:
        """Manifest file is written to .sdd/caching/manifest.jsonl."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)

        _spawn(adapter, PROMPT_A, tmp_path)

        manifest_path = tmp_path / ".sdd" / "caching" / "manifest.jsonl"
        assert manifest_path.exists()
        content = manifest_path.read_text()
        assert len(content) > 0

    def test_concurrent_spawns_no_corruption(self, tmp_path: Path) -> None:
        """Concurrent spawn calls don't corrupt the cache manifest."""
        inner = FakeAdapter()
        adapter = CachingAdapter(inner, tmp_path)
        errors: list[Exception] = []

        def spawn_n(prompt: str, n: int) -> None:
            try:
                for _ in range(n):
                    _spawn(adapter, prompt, tmp_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=spawn_n, args=(PROMPT_A, 5)),
            threading.Thread(target=spawn_n, args=(PROMPT_B, 5)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent spawns: {errors}"
        mgr = adapter._caching_mgr
        assert len(mgr._manifest.entries) == 2
        assert len(inner.spawn_calls) == 10
