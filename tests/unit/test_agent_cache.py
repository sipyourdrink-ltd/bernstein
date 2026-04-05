"""Unit tests for the file-based agent cache."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.agent_cache import AgentCache, CacheEntry, _key_hash, cache_key_for_file

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    """Return a temporary cache directory."""
    d = tmp_path / "cache"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# CacheEntry dataclass
# ---------------------------------------------------------------------------


class TestCacheEntry:
    def test_fields(self) -> None:
        entry = CacheEntry(key="a", value="b", created_at=1.0, size_bytes=1)
        assert entry.key == "a"
        assert entry.value == "b"
        assert entry.created_at == 1.0
        assert entry.size_bytes == 1


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestKeyHash:
    def test_deterministic(self) -> None:
        assert _key_hash("hello") == _key_hash("hello")

    def test_different_inputs(self) -> None:
        assert _key_hash("hello") != _key_hash("world")

    def test_length(self) -> None:
        assert len(_key_hash("anything")) == 64  # SHA-256 hex


class TestCacheKeyForFile:
    def test_format(self) -> None:
        key = cache_key_for_file("src/main.py", "abc123")
        assert key == "src/main.py@abc123"


# ---------------------------------------------------------------------------
# put / get round-trip
# ---------------------------------------------------------------------------


class TestPutGet:
    def test_roundtrip(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("mykey", "myvalue", parent_session="sess-1")
        result = cache.get("mykey", session="sess-1")
        assert result == "myvalue"

    def test_roundtrip_global(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("k", "v")
        assert cache.get("k") == "v"

    def test_miss_returns_none(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        assert cache.get("nonexistent") is None

    def test_get_falls_back_to_global(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("shared", "global_value")
        # Searching in a session that doesn't have it falls back to _global
        assert cache.get("shared", session="child-1") == "global_value"

    def test_session_overrides_global(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("k", "global_v")
        cache.put("k", "session_v", parent_session="s1")
        assert cache.get("k", session="s1") == "session_v"


# ---------------------------------------------------------------------------
# share_with
# ---------------------------------------------------------------------------


class TestShareWith:
    def test_creates_readable_entries(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("file1", "content1", parent_session="parent")
        cache.put("file2", "content2", parent_session="parent")

        shared = cache.share_with("child", ["file1", "file2"], parent_session="parent")
        assert shared == 2

        # Child can read the shared entries
        assert cache.get("file1", session="child") == "content1"
        assert cache.get("file2", session="child") == "content2"

    def test_share_nonexistent_key_skipped(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("exists", "val", parent_session="parent")

        shared = cache.share_with("child", ["exists", "missing"], parent_session="parent")
        assert shared == 1

    def test_share_creates_symlinks(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("k", "v", parent_session="parent")

        cache.share_with("child", ["k"], parent_session="parent")

        child_entry = cache_dir / "child" / f"{_key_hash('k')}.json"
        assert child_entry.is_symlink()

    def test_share_idempotent(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("k", "v", parent_session="parent")

        first = cache.share_with("child", ["k"], parent_session="parent")
        second = cache.share_with("child", ["k"], parent_session="parent")
        assert first == 1
        assert second == 0  # Already linked, skip

    def test_share_with_global(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("gk", "gv")

        shared = cache.share_with("child", ["gk"])
        assert shared == 1
        assert cache.get("gk", session="child") == "gv"


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_removes_old_entries(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("old", "old_val", parent_session="s")

        # Backdate the entry
        entry_path = cache_dir / "s" / f"{_key_hash('old')}.json"
        data = json.loads(entry_path.read_text())
        data["created_at"] = time.time() - 7200  # 2 hours ago
        entry_path.write_text(json.dumps(data))

        removed = cache.cleanup(max_age_seconds=3600.0)
        assert removed == 1
        assert cache.get("old", session="s") is None

    def test_keeps_recent_entries(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("recent", "val", parent_session="s")

        removed = cache.cleanup(max_age_seconds=3600.0)
        assert removed == 0
        assert cache.get("recent", session="s") == "val"

    def test_removes_empty_session_dirs(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("old", "v", parent_session="empty_soon")

        # Backdate
        entry_path = cache_dir / "empty_soon" / f"{_key_hash('old')}.json"
        data = json.loads(entry_path.read_text())
        data["created_at"] = time.time() - 7200
        entry_path.write_text(json.dumps(data))

        cache.cleanup(max_age_seconds=3600.0)
        assert not (cache_dir / "empty_soon").exists()

    def test_cleanup_on_empty_cache(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        assert cache.cleanup() == 0


# ---------------------------------------------------------------------------
# max_size enforcement
# ---------------------------------------------------------------------------


class TestMaxSizeEnforcement:
    def test_evicts_oldest_when_over_limit(self, cache_dir: Path) -> None:
        # 1 KiB limit
        cache = AgentCache(cache_dir, max_size_mb=0.001)

        # Write entries that together exceed 1 KiB
        cache.put("entry1", "x" * 600, parent_session="s")

        # Backdate entry1 so it's oldest
        p1 = cache_dir / "s" / f"{_key_hash('entry1')}.json"
        data = json.loads(p1.read_text())
        data["created_at"] = time.time() - 100
        p1.write_text(json.dumps(data))

        cache.put("entry2", "y" * 600, parent_session="s")

        # entry1 should have been evicted (oldest) to make room
        assert cache.get("entry1", session="s") is None
        assert cache.get("entry2", session="s") == "y" * 600


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty_cache_stats(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        s = cache.stats()
        assert s["hit_count"] == 0
        assert s["miss_count"] == 0
        assert s["hit_rate"] == 0.0
        assert s["total_size_bytes"] == 0
        assert s["entry_count"] == 0

    def test_stats_after_operations(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("a", "value_a", parent_session="s")
        cache.put("b", "value_b", parent_session="s")

        cache.get("a", session="s")  # hit
        cache.get("missing", session="s")  # miss

        s = cache.stats()
        assert s["hit_count"] == 1
        assert s["miss_count"] == 1
        assert s["hit_rate"] == pytest.approx(0.5)
        assert s["entry_count"] == 2
        assert s["total_size_bytes"] > 0

    def test_symlinks_not_counted_in_entry_count(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("k", "v", parent_session="parent")
        cache.share_with("child", ["k"], parent_session="parent")

        s = cache.stats()
        # Only the real file, not the symlink
        assert s["entry_count"] == 1


# ---------------------------------------------------------------------------
# list_keys
# ---------------------------------------------------------------------------


class TestListKeys:
    def test_list_keys(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        cache.put("alpha", "a", parent_session="s")
        cache.put("beta", "b", parent_session="s")

        keys = cache.list_keys("s")
        assert set(keys) == {"alpha", "beta"}

    def test_list_keys_empty(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        assert cache.list_keys("nonexistent") == []


# ---------------------------------------------------------------------------
# cache_key_for_file integration
# ---------------------------------------------------------------------------


class TestFileKeyIntegration:
    def test_file_cache_roundtrip(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        key = cache_key_for_file("src/main.py", "abc123")
        cache.put(key, "file contents here", parent_session="agent-1")
        assert cache.get(key, session="agent-1") == "file contents here"

    def test_different_git_hashes_different_keys(self, cache_dir: Path) -> None:
        cache = AgentCache(cache_dir)
        k1 = cache_key_for_file("src/main.py", "aaa")
        k2 = cache_key_for_file("src/main.py", "bbb")
        cache.put(k1, "old", parent_session="s")
        cache.put(k2, "new", parent_session="s")
        assert cache.get(k1, session="s") == "old"
        assert cache.get(k2, session="s") == "new"
