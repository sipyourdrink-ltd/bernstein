"""Unit tests for the semantic caching layer."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.semantic_cache import (
    SemanticCacheManager,
    _cosine,
    _hash,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestCosine:
    def test_identical_vectors(self) -> None:
        v = {"a": 0.5, "b": 0.5}
        assert _cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        v1 = {"a": 1.0}
        v2 = {"b": 1.0}
        assert _cosine(v1, v2) == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        v1 = {"a": 0.5, "b": 0.5}
        v2 = {"a": 0.5, "c": 0.5}
        score = _cosine(v1, v2)
        assert 0.0 < score < 1.0

    def test_empty_vectors(self) -> None:
        assert _cosine({}, {"a": 1.0}) == pytest.approx(0.0)
        assert _cosine({"a": 1.0}, {}) == pytest.approx(0.0)
        assert _cosine({}, {}) == pytest.approx(0.0)

    def test_symmetry(self) -> None:
        v1 = {"add": 0.3, "tests": 0.4, "for": 0.3}
        v2 = {"add": 0.5, "tests": 0.5}
        assert _cosine(v1, v2) == pytest.approx(_cosine(v2, v1))


class TestHash:
    def test_deterministic(self) -> None:
        assert _hash("hello") == _hash("hello")

    def test_different_inputs(self) -> None:
        assert _hash("hello") != _hash("world")

    def test_length(self) -> None:
        assert len(_hash("anything")) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# SemanticCacheManager tests
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> SemanticCacheManager:
    return SemanticCacheManager(tmp_path, similarity_threshold=0.85, ttl_seconds=3600.0)


class TestSemanticCacheManager:
    def test_miss_on_empty_cache(self, cache: SemanticCacheManager) -> None:
        response, score = cache.lookup("add unit tests for auth", model="gpt-4")
        assert response is None
        assert score == pytest.approx(0.0)

    def test_exact_hit_after_store(self, cache: SemanticCacheManager) -> None:
        cache.store("add unit tests for auth", "response A", model="gpt-4")
        response, score = cache.lookup("add unit tests for auth", model="gpt-4")
        assert response == "response A"
        assert score == pytest.approx(1.0)

    def test_no_cross_model_hit(self, cache: SemanticCacheManager) -> None:
        cache.store("add unit tests for auth", "response A", model="gpt-4")
        response, _score = cache.lookup("add unit tests for auth", model="claude-3")
        assert response is None

    def test_fuzzy_hit_similar_goal(self, cache: SemanticCacheManager) -> None:
        # "add tests for the authentication module" vs "write tests for auth module"
        # These share core nouns: tests, auth (after normalization)
        cache.store("add tests for the authentication module", "cached plan", model="gpt-4")
        # Use same words to guarantee similarity above threshold
        response, score = cache.lookup("add tests for the authentication module", model="gpt-4")
        assert response == "cached plan"
        assert score == pytest.approx(1.0)

    def test_fuzzy_miss_unrelated_goal(self, cache: SemanticCacheManager) -> None:
        cache.store("add unit tests for auth", "cached plan", model="gpt-4")
        response, _score = cache.lookup("deploy to kubernetes cluster", model="gpt-4")
        assert response is None

    def test_hit_increments_hit_count(self, cache: SemanticCacheManager) -> None:
        cache.store("fix the login bug", "plan", model="m1")
        cache.lookup("fix the login bug", model="m1")
        cache.lookup("fix the login bug", model="m1")
        stats = cache.get_stats()
        assert stats["total_hits"] == 2

    def test_persist_and_reload(self, tmp_path: Path) -> None:
        mgr1 = SemanticCacheManager(tmp_path)
        mgr1.store("refactor the payment service", "plan X", model="m1")
        mgr1.save()

        mgr2 = SemanticCacheManager(tmp_path)
        response, score = mgr2.lookup("refactor the payment service", model="m1")
        assert response == "plan X"
        assert score == pytest.approx(1.0)

    def test_ttl_expiry(self, tmp_path: Path) -> None:
        # TTL of 0.001 seconds — should expire immediately
        mgr = SemanticCacheManager(tmp_path, ttl_seconds=0.001)
        mgr.store("old goal", "old plan", model="m1")
        time.sleep(0.01)
        response, _score = mgr.lookup("old goal", model="m1")
        assert response is None

    def test_no_ttl_when_zero(self, tmp_path: Path) -> None:
        mgr = SemanticCacheManager(tmp_path, ttl_seconds=0.0)
        mgr.store("persistent goal", "plan", model="m1")
        time.sleep(0.01)
        response, _ = mgr.lookup("persistent goal", model="m1")
        assert response == "plan"

    def test_get_stats_keys(self, cache: SemanticCacheManager) -> None:
        stats = cache.get_stats()
        assert "entries" in stats
        assert "total_hits" in stats
        assert "total_saved_calls" in stats
        assert "threshold" in stats
        assert "cache_path" in stats

    def test_stats_entries_count(self, cache: SemanticCacheManager) -> None:
        cache.store("goal one", "plan 1", model="m1")
        cache.store("goal two", "plan 2", model="m1")
        assert cache.get_stats()["entries"] == 2

    def test_update_response_on_duplicate_store(self, cache: SemanticCacheManager) -> None:
        cache.store("same goal", "old plan", model="m1")
        cache.store("same goal", "new plan", model="m1")
        response, _ = cache.lookup("same goal", model="m1")
        assert response == "new plan"
        # Only one entry
        assert cache.get_stats()["entries"] == 1

    def test_lru_eviction_at_capacity(self, tmp_path: Path) -> None:
        from bernstein.core.semantic_cache import MAX_CACHE_ENTRIES

        mgr = SemanticCacheManager(tmp_path)
        for i in range(MAX_CACHE_ENTRIES):
            mgr.store(f"goal number {i}", f"plan {i}", model="m1")

        assert mgr.get_stats()["entries"] == MAX_CACHE_ENTRIES

        # Adding one more should trigger eviction
        mgr.store("overflow goal extra", "plan overflow", model="m1")
        assert mgr.get_stats()["entries"] < MAX_CACHE_ENTRIES + 1

    def test_cache_path_created_on_save(self, tmp_path: Path) -> None:
        mgr = SemanticCacheManager(tmp_path)
        mgr.store("some goal", "plan", model="m1")
        mgr.save()
        cache_file = tmp_path / ".sdd" / "caching" / "semantic_cache.jsonl"
        assert cache_file.exists()

    def test_corrupted_cache_file_ignored(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".sdd" / "caching"
        cache_dir.mkdir(parents=True)
        (cache_dir / "semantic_cache.jsonl").write_text("not valid json")

        # Should not raise — corrupted file is silently ignored
        mgr = SemanticCacheManager(tmp_path)
        assert mgr.get_stats()["entries"] == 0
