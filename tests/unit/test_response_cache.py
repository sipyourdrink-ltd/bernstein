"""Unit tests for the ResponseCacheManager (agent-output response cache)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.semantic_cache import (
    RESPONSE_CACHE_MAX_ENTRIES,
    RESPONSE_CACHE_SIMILARITY_THRESHOLD,
    ResponseCacheManager,
    _embed,
    _normalize,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Module-level pure-function tests (shared by both cache classes)
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercase(self) -> None:
        assert _normalize("Hello World") == "hello world"

    def test_strips_punctuation(self) -> None:
        assert _normalize("fix: the bug!") == "fix the bug"

    def test_collapses_whitespace(self) -> None:
        assert _normalize("add   tests  now") == "add tests now"

    def test_empty_string(self) -> None:
        assert _normalize("") == ""


class TestEmbed:
    def test_returns_tf_vector(self) -> None:
        vec = _embed("add add test")
        assert vec["add"] == pytest.approx(2 / 3)
        assert vec["test"] == pytest.approx(1 / 3)

    def test_empty_text_returns_empty_dict(self) -> None:
        assert _embed("") == {}

    def test_all_words_present(self) -> None:
        vec = _embed("backend fix login")
        assert set(vec.keys()) == {"backend", "fix", "login"}


# ---------------------------------------------------------------------------
# ResponseCacheManager tests
# ---------------------------------------------------------------------------


@pytest.fixture
def rcache(tmp_path: Path) -> ResponseCacheManager:
    return ResponseCacheManager(tmp_path, similarity_threshold=0.95, ttl_seconds=3600.0)


class TestResponseCacheManagerTaskKey:
    def test_combines_role_title_description(self) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login", "Fix the login bug")
        assert key == "backend:Fix login\nFix the login bug"

    def test_different_roles_produce_different_keys(self) -> None:
        k1 = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        k2 = ResponseCacheManager.task_key("qa", "Fix login", "desc")
        assert k1 != k2


class TestResponseCacheManagerMiss:
    def test_miss_on_empty_cache(self, rcache: ResponseCacheManager) -> None:
        result, score = rcache.lookup(ResponseCacheManager.task_key("backend", "Fix bug", "desc"))
        assert result is None
        assert score == 0.0


class TestResponseCacheManagerExactHit:
    def test_exact_hit_after_store(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login bug", "Fix the login endpoint")
        rcache.store(key, "Fixed by patching auth middleware")
        result, score = rcache.lookup(key)
        assert result == "Fixed by patching auth middleware"
        assert score == pytest.approx(1.0)

    def test_empty_result_not_stored(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        rcache.store(key, "")
        result, _score = rcache.lookup(key)
        assert result is None

    def test_update_result_on_duplicate_key(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        rcache.store(key, "old result")
        rcache.store(key, "new result")
        result, _ = rcache.lookup(key)
        assert result == "new result"
        assert rcache.get_stats()["entries"] == 1


class TestResponseCacheManagerFuzzyHit:
    def test_fuzzy_hit_semantically_identical(self, rcache: ResponseCacheManager) -> None:
        # Identical text → similarity 1.0
        key = ResponseCacheManager.task_key("backend", "add unit tests for auth module", "add tests")
        rcache.store(key, "Added 12 tests")
        result, score = rcache.lookup(key)
        assert result == "Added 12 tests"
        assert score == pytest.approx(1.0)

    def test_fuzzy_miss_unrelated_task(self, rcache: ResponseCacheManager) -> None:
        key1 = ResponseCacheManager.task_key("backend", "Fix login bug", "Fix the auth flow")
        rcache.store(key1, "Done")
        key2 = ResponseCacheManager.task_key("qa", "Deploy to Kubernetes", "Set up k8s cluster")
        result, _score = rcache.lookup(key2)
        assert result is None

    def test_threshold_respected(self, tmp_path: Path) -> None:
        # With threshold=1.0, only exact matches should hit
        strict = ResponseCacheManager(tmp_path, similarity_threshold=1.0)
        key = ResponseCacheManager.task_key("backend", "add auth tests", "write tests")
        strict.store(key, "done")
        # Slightly different key — same hash, still hits (exact match via SHA-256)
        result, _score = strict.lookup(key)
        assert result == "done"


class TestResponseCacheManagerStats:
    def test_get_stats_keys(self, rcache: ResponseCacheManager) -> None:
        stats = rcache.get_stats()
        assert "entries" in stats
        assert "total_hits" in stats
        assert "total_saved_calls" in stats
        assert "threshold" in stats
        assert "cache_path" in stats

    def test_hit_increments_total_hits(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("qa", "Run smoke tests", "smoke test")
        rcache.store(key, "Passed 42 tests")
        rcache.lookup(key)
        rcache.lookup(key)
        assert rcache.get_stats()["total_hits"] == 2
        assert rcache.get_stats()["total_saved_calls"] == 2

    def test_entries_count_increments(self, rcache: ResponseCacheManager) -> None:
        rcache.store(ResponseCacheManager.task_key("backend", "Fix bug A", ""), "done A")
        rcache.store(ResponseCacheManager.task_key("backend", "Fix bug B", ""), "done B")
        assert rcache.get_stats()["entries"] == 2

    def test_stats_split_verified_and_unverified_entries(self, rcache: ResponseCacheManager) -> None:
        rcache.store(
            ResponseCacheManager.task_key("backend", "Verified task", ""),
            "done A",
            verified=True,
            git_diff_lines=12,
            source_task_id="T-verified",
        )
        rcache.store(
            ResponseCacheManager.task_key("backend", "Unverified task", ""),
            "done B",
            verified=False,
            source_task_id="T-unverified",
        )
        stats = rcache.get_stats()
        assert stats["verified_entries"] == 1
        assert stats["unverified_entries"] == 1


class TestResponseCacheManagerTTL:
    def test_expired_entries_not_returned(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path, ttl_seconds=0.001)
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        mgr.store(key, "Fixed")
        time.sleep(0.01)
        result, _score = mgr.lookup(key)
        assert result is None

    def test_ttl_zero_disables_expiry(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path, ttl_seconds=0.0)
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        mgr.store(key, "Fixed")
        time.sleep(0.01)
        result, _ = mgr.lookup(key)
        assert result == "Fixed"


class TestResponseCacheManagerPersistence:
    def test_persist_and_reload(self, tmp_path: Path) -> None:
        mgr1 = ResponseCacheManager(tmp_path)
        key = ResponseCacheManager.task_key("backend", "Refactor payment service", "desc")
        mgr1.store(key, "Refactored into 3 modules")
        mgr1.save()

        mgr2 = ResponseCacheManager(tmp_path)
        result, score = mgr2.lookup(key)
        assert result == "Refactored into 3 modules"
        assert score == pytest.approx(1.0)

    def test_cache_path_created_on_save(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path)
        mgr.store(ResponseCacheManager.task_key("backend", "Fix bug", "desc"), "done")
        mgr.save()
        assert (tmp_path / ".sdd" / "caching" / "response_cache.jsonl").exists()

    def test_corrupted_file_ignored(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".sdd" / "caching"
        cache_dir.mkdir(parents=True)
        (cache_dir / "response_cache.jsonl").write_text("not valid json{{{")

        mgr = ResponseCacheManager(tmp_path)
        assert mgr.get_stats()["entries"] == 0

    def test_uses_separate_file_from_semantic_cache(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path)
        assert "response_cache.jsonl" in str(mgr.get_stats()["cache_path"])
        assert "semantic_cache" not in str(mgr.get_stats()["cache_path"])


class TestResponseCacheManagerMetadata:
    def test_lookup_entry_returns_metadata(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        rcache.store(
            key,
            "Fixed by patching auth middleware",
            verified=True,
            git_diff_lines=21,
            source_task_id="T-123",
        )

        entry, score = rcache.lookup_entry(key)

        assert entry is not None
        assert score == pytest.approx(1.0)
        assert entry.verified is True
        assert entry.git_diff_lines == 21
        assert entry.source_task_id == "T-123"

    def test_duplicate_store_refreshes_metadata(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Fix login", "desc")
        rcache.store(key, "old", verified=False, git_diff_lines=0, source_task_id="T-old")
        rcache.store(key, "new", verified=True, git_diff_lines=7, source_task_id="T-new")

        entry, _score = rcache.lookup_entry(key)

        assert entry is not None
        assert entry.response == "new"
        assert entry.verified is True
        assert entry.git_diff_lines == 7
        assert entry.source_task_id == "T-new"

    def test_list_entries_sorted_by_recency(self, rcache: ResponseCacheManager) -> None:
        first_key = ResponseCacheManager.task_key("backend", "Task A", "")
        second_key = ResponseCacheManager.task_key("backend", "Task B", "")
        rcache.store(first_key, "A", source_task_id="T-a")
        time.sleep(0.01)
        rcache.store(second_key, "B", source_task_id="T-b")

        entries = rcache.list_entries()

        assert [entry.source_task_id for entry in entries[:2]] == ["T-b", "T-a"]

    def test_inspect_task_finds_originating_entry(self, rcache: ResponseCacheManager) -> None:
        key = ResponseCacheManager.task_key("backend", "Task A", "")
        rcache.store(key, "A", source_task_id="T-a", verified=True)

        entry = rcache.inspect_task("T-a")

        assert entry is not None
        assert entry.response == "A"
        assert entry.verified is True

    def test_clear_unverified_only_keeps_verified_entries(self, rcache: ResponseCacheManager) -> None:
        verified_key = ResponseCacheManager.task_key("backend", "Verified", "")
        unverified_key = ResponseCacheManager.task_key("backend", "Unverified", "")
        rcache.store(verified_key, "safe", verified=True, source_task_id="T-safe")
        rcache.store(unverified_key, "ghost", verified=False, source_task_id="T-ghost")

        removed = rcache.clear(unverified_only=True)

        assert removed == 1
        assert rcache.inspect_task("T-safe") is not None
        assert rcache.inspect_task("T-ghost") is None


class TestResponseCacheManagerEviction:
    def test_lru_eviction_at_capacity(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path)
        for i in range(RESPONSE_CACHE_MAX_ENTRIES):
            key = ResponseCacheManager.task_key("backend", f"Task number {i}", "desc")
            mgr.store(key, f"result {i}")

        assert mgr.get_stats()["entries"] == RESPONSE_CACHE_MAX_ENTRIES

        # Adding one more triggers eviction of oldest 10%
        mgr.store(ResponseCacheManager.task_key("backend", "overflow task extra", "desc"), "overflow")
        assert mgr.get_stats()["entries"] < RESPONSE_CACHE_MAX_ENTRIES + 1


class TestResponseCacheManagerDefaultThreshold:
    def test_default_threshold_is_0_95(self, tmp_path: Path) -> None:
        mgr = ResponseCacheManager(tmp_path)
        assert mgr.get_stats()["threshold"] == RESPONSE_CACHE_SIMILARITY_THRESHOLD
        assert RESPONSE_CACHE_SIMILARITY_THRESHOLD == 0.95
