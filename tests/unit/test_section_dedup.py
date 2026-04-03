"""Tests for section_dedup — SectionDeduplicator."""

from __future__ import annotations

import threading

from bernstein.core.section_dedup import (
    SectionDeduplicator,
    deduplicate_section,
    get_dedup_stats,
    get_deduplicator,
    reset_deduplicator,
)


class TestSectionDeduplicator:
    def test_empty_string_returns_empty(self) -> None:
        dedup = SectionDeduplicator()
        assert dedup.deduplicate("") == ""

    def test_first_insert_is_a_miss(self) -> None:
        dedup = SectionDeduplicator()
        text = "some prompt section"
        result = dedup.deduplicate(text)
        assert result == text
        assert dedup.stats()["misses"] == 1
        assert dedup.stats()["hits"] == 0

    def test_second_identical_insert_is_a_hit(self) -> None:
        dedup = SectionDeduplicator()
        text = "some prompt section"
        dedup.deduplicate(text)
        result = dedup.deduplicate(text)
        assert result == text
        assert dedup.stats()["hits"] == 1
        assert dedup.stats()["misses"] == 1

    def test_returns_strictly_same_object_reference(self) -> None:
        dedup = SectionDeduplicator()
        text = "unique text content"
        first = dedup.deduplicate(text)
        second = dedup.deduplicate(text)
        assert first is second

    def test_different_texts_are_different_misses(self) -> None:
        dedup = SectionDeduplicator()
        dedup.deduplicate("text A")
        dedup.deduplicate("text B")
        assert dedup.stats()["misses"] == 2
        assert dedup.stats()["hits"] == 0

    def test_clear_resets_cache_but_not_stats(self) -> None:
        dedup = SectionDeduplicator()
        dedup.deduplicate("hello")
        dedup.deduplicate("hello")
        dedup.clear()
        stats = dedup.stats()
        assert stats["hits"] == 1  # stats are lifetime, not reset on clear
        assert stats["size"] == 0

    def test_eviction_when_max_entries_exceeded(self) -> None:
        dedup = SectionDeduplicator(max_entries=3)
        dedup.deduplicate("a")
        dedup.deduplicate("b")
        dedup.deduplicate("c")
        # Access "a" again to make it MRU
        dedup.deduplicate("a")
        # Insert "d" — should evict "b" (LRU)
        dedup.deduplicate("d")
        stats = dedup.stats()
        assert stats["size"] == 3
        # "b" was evicted, so next access should be a miss
        dedup.deduplicate("b")
        assert dedup.stats()["misses"] == stats["misses"] + 1

    def test_max_entries_increases_after_eviction(self) -> None:
        dedup = SectionDeduplicator(max_entries=2)
        dedup.deduplicate("a")
        dedup.deduplicate("b")
        dedup.deduplicate("c")  # evicts "a"
        dedup.deduplicate("d")  # evicts "b"
        assert dedup.stats()["size"] == 2

    def test_thread_safety(self) -> None:
        dedup = SectionDeduplicator()
        barrier = threading.Barrier(4)
        results: list[str] = []
        lock = threading.Lock()

        def worker(text: str) -> None:
            barrier.wait(timeout=5)
            for _ in range(50):
                result = dedup.deduplicate(text)
                with lock:
                    results.append(result)

        threads = [threading.Thread(target=worker, args=("shared text",)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert all(r == "shared text" for r in results)

    def test_stats_return_correct_size(self) -> None:
        dedup = SectionDeduplicator()
        dedup.deduplicate("one")
        dedup.deduplicate("two")
        dedup.deduplicate("three")
        assert dedup.stats()["size"] == 3


class TestGlobalDeduplicator:
    def setup_method(self) -> None:
        reset_deduplicator()

    def teardown_method(self) -> None:
        reset_deduplicator()

    def test_get_deduplicator_returns_singleton(self) -> None:
        assert get_deduplicator() is get_deduplicator()

    def test_global_dedup_shares_state(self) -> None:
        deduplicate_section("test content")
        stats = get_dedup_stats()
        assert stats["misses"] == 1

        deduplicate_section("test content")
        stats = get_dedup_stats()
        assert stats["hits"] == 1

    def test_reset_clears_global(self) -> None:
        deduplicate_section("persist me")
        reset_deduplicator()
        assert get_dedup_stats()["size"] == 0
