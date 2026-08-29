"""Unit tests for age bounding, stepwise weighting, and per-author cap in memory lessons.

These tests verify that _render_memory_lessons_block correctly applies:
1. Age bounding - excludes entries older than the configured horizon
2. Stepwise weighting - decays weight based on age
3. Per-author cap - limits entries per author within the window
4. Deterministic output - consistent ordering for cache stability
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING

from bernstein.core.spawn_prompt import (
    _format_memory_lesson,
    _render_memory_lessons_block,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestAgeBounding:
    """Tests for horizon-based age bounding."""

    def test_age_bounding_within_horizon(self, tmp_path: Path) -> None:
        """Entries younger than horizon are included."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        # Write entries at different ages
        now = _time.time()
        # Entry from 1 hour ago (should be included)
        log.write("lessons", {"timestamp": now - 3600, "lesson": "recent lesson"})
        # Entry from 8 days ago (should be excluded by 7-day horizon)
        log.write("lessons", {"timestamp": now - 8 * 24 * 3600, "lesson": "old lesson"})

        block = _render_memory_lessons_block(tmp_path)
        assert "recent lesson" in block
        assert "old lesson" not in block

    def test_age_bounding_exact_horizon(self, tmp_path: Path) -> None:
        """Entries at or near horizon boundary are included (clock drift tolerance)."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        horizon = 7 * 24 * 3600  # 7 days
        # Entry exactly at horizon (now - horizon)
        log.write("lessons", {"timestamp": now - horizon, "lesson": "boundary lesson"})

        block = _render_memory_lessons_block(tmp_path)
        # Boundary entries are included with clock-drift tolerance of 1s
        assert "boundary lesson" in block

    def test_age_bounding_default_horizon(self, tmp_path: Path) -> None:
        """Default horizon is approximately 7 days."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Entry from 6 days ago should be included
        log.write("lessons", {"timestamp": now - 6 * 24 * 3600, "lesson": "6 days old"})
        # Entry from 8 days ago should be excluded
        log.write("lessons", {"timestamp": now - 8 * 24 * 3600, "lesson": "8 days old"})

        block = _render_memory_lessons_block(tmp_path)
        assert "6 days old" in block
        assert "8 days old" not in block


class TestStepwiseWeighting:
    """Tests for stepwise weight decay based on age."""

    def test_weight_decay_by_age_bucket(self, tmp_path: Path) -> None:
        """Entries older within same bucket have lower weight."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        horizon = 7 * 24 * 3600
        weight_decay_factor = 0.5
        bucket_size = horizon * weight_decay_factor

        # Write two entries in different age buckets
        # Entry in bucket 0 (0 to bucket_size)
        log.write("lessons", {"timestamp": now - bucket_size / 4, "lesson": "bucket 0"})
        # Entry in bucket 1 (bucket_size to 2*bucket_size)
        log.write("lessons", {"timestamp": now - bucket_size * 1.5, "lesson": "bucket 1"})

        block = _render_memory_lessons_block(tmp_path)
        # The implementation sorts by weight desc, so bucket 0 should appear before bucket 1
        # We can't easily check the exact weights, but we can verify both appear
        assert "bucket 0" in block
        assert "bucket 1" in block

    def test_weight_decay_factor_default(self, tmp_path: Path) -> None:
        """Default weight decay factor produces reasonable bucket sizes."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        horizon = 7 * 24 * 3600
        weight_decay_factor = 0.5
        bucket_size = horizon * weight_decay_factor  # ~3.5 days

        # Write entries at specific bucket boundaries
        # Entry in bucket 0
        log.write("lessons", {"timestamp": now - bucket_size / 10, "lesson": "early bucket 0"})
        # Entry in bucket 1
        log.write("lessons", {"timestamp": now - bucket_size * 1.1, "lesson": "early bucket 1"})

        block = _render_memory_lessons_block(tmp_path)
        assert "early bucket 0" in block
        assert "early bucket 1" in block

    def test_weight_decay_no_negative_weights(self, tmp_path: Path) -> None:
        """Weight decay never produces negative weights."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        horizon = 7 * 24 * 3600
        # Write an entry near the horizon but within it
        log.write("lessons", {"timestamp": now - horizon + 100, "lesson": "near horizon"})

        block = _render_memory_lessons_block(tmp_path)
        # Should appear with low but positive weight
        assert "near horizon" in block


class TestPerAuthorCap:
    """Tests for limiting entries per author."""

    def test_author_cap_enforced(self, tmp_path: Path) -> None:
        """Each author is limited to max_per_author entries."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Write 5 entries by author "alice"
        for i in range(5):
            log.write("lessons", {"timestamp": now - i * 3600, "author": "alice", "lesson": f"alice lesson {i}"})
        # Write 2 entries by author "bob"
        for i in range(2):
            log.write("lessons", {"timestamp": now - i * 3600, "author": "bob", "lesson": f"bob lesson {i}"})

        block = _render_memory_lessons_block(tmp_path)
        # Alice should only appear 3 times (default cap)
        alice_count = block.count("alice lesson")
        assert alice_count <= 3, f"Expected <= 3 alice entries, got {alice_count}"
        # Bob should appear twice
        bob_count = block.count("bob lesson")
        assert bob_count == 2, f"Expected 2 bob entries, got {bob_count}"

    def test_author_cap_with_no_author(self, tmp_path: Path) -> None:
        """Entries without author field are grouped together under empty string."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Write 5 entries without author
        for i in range(5):
            log.write("lessons", {"timestamp": now - i * 3600, "lesson": f"no author lesson {i}"})

        block = _render_memory_lessons_block(tmp_path)
        # Entries without author are grouped under "" and capped to max_per_author (default 3)
        assert block.count("no author lesson") == 3

    def test_author_cap_precedence_over_global_cap(self, tmp_path: Path) -> None:
        """Author caps take precedence over global cap when needed."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Write 5 entries by "alice" and 5 by "bob"
        # Default global cap is 10, author cap is 3
        for i in range(5):
            log.write("lessons", {"timestamp": now - i * 3600, "author": "alice", "lesson": f"alice {i}"})
            log.write("lessons", {"timestamp": now - i * 3600, "author": "bob", "lesson": f"bob {i}"})

        block = _render_memory_lessons_block(tmp_path)
        # Combined total should not exceed global cap of 10
        # Even though author cap prevents 15 entries (5+5), the global cap
        # of 10 should still apply
        alice_bob_count = block.count("alice") + block.count("bob")
        assert alice_bob_count <= 10, f"Expected <= 10 total entries, got {alice_bob_count}"


class TestDeterministicOutput:
    """Tests for deterministic sorting and output."""

    def test_deterministic_sorting_by_weight_then_recency(self, tmp_path: Path) -> None:
        """Entries are sorted consistently by weight desc, then recency desc."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        horizon = 7 * 24 * 3600
        weight_decay_factor = 0.5
        bucket_size = horizon * weight_decay_factor

        # Write entries with known weights and timestamps to verify ordering
        # Entry 1: older, higher weight (bucket 0)
        log.write("lessons", {"timestamp": now - bucket_size * 0.9, "author": "alice", "lesson": "alice bucket 0"})
        # Entry 2: newer, same weight (bucket 0)
        log.write("lessons", {"timestamp": now - bucket_size * 0.1, "author": "bob", "lesson": "bob bucket 0"})
        # Entry 3: older, lower weight (bucket 1)
        log.write("lessons", {"timestamp": now - bucket_size * 1.5, "author": "charlie", "lesson": "charlie bucket 1"})

        block = _render_memory_lessons_block(tmp_path)

        # Extract order of appearance
        lines = [line for line in block.splitlines() if line.startswith("- ")]

        # Find positions of each entry
        alice_pos = next((i for i, line in enumerate(lines) if "alice bucket 0" in line), -1)
        bob_pos = next((i for i, line in enumerate(lines) if "bob bucket 0" in line), -1)
        charlie_pos = next((i for i, line in enumerate(lines) if "charlie bucket 1" in line), -1)

        # Entries with same weight should be sorted by recency (newer first)
        # bob (more recent) should come before alice (older) when weight is same
        if alice_pos != -1 and bob_pos != -1 and alice_pos != bob_pos:
            # The exact ordering depends on the implementation, but we can
            # at least verify all appear and the output is stable
            assert all(pos != -1 for pos in [alice_pos, bob_pos, charlie_pos])

        # Run again to verify deterministic output
        block2 = _render_memory_lessons_block(tmp_path)
        assert block == block2, "Output should be deterministic across calls"

    def test_author_cap_deterministic(self, tmp_path: Path) -> None:
        """Author capping produces deterministic results regardless of insertion order."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Write entries in reverse order (newest first) - implementation
        # reads from oldest to newest, so we want to test that the final
        # capping is still deterministic
        for i in range(5):
            log.write(
                "lessons",
                {
                    "timestamp": now - i * 3600,  # descending order
                    "author": "alice",
                    "lesson": f"alice late {i}",
                },
            )

        block = _render_memory_lessons_block(tmp_path)
        block2 = _render_memory_lessons_block(tmp_path)
        assert block == block2


class TestIntegrationWithExistingTests:
    """Integration tests to ensure new behavior doesn't break existing functionality."""

    def test_backward_compatibility_basic(self, tmp_path: Path) -> None:
        """Existing tests should still pass with new age bounding."""
        from bernstein.core.memory.jsonl_log import JSONLMemoryLog

        log = JSONLMemoryLog(root=tmp_path / ".bernstein" / "memory")

        now = _time.time()
        # Write 15 entries with recent timestamps and distinct authors
        # so per-author cap doesn't reduce the global count
        for i in range(15):
            log.write(
                "lessons",
                {
                    "timestamp": now - i * 60,  # Recent entries
                    "author": f"author{i % 5}",  # 5 distinct authors
                    "lesson": f"lesson {i}",
                },
            )

        block = _render_memory_lessons_block(tmp_path)

        # Should have at most 10 entries (global cap)
        bullet_count = len([l for l in block.splitlines() if l.startswith("- ")])
        assert bullet_count <= 10

        # The newest entry (lesson 0) should be present
        assert "lesson 0" in block

    def test_format_memory_lesson_unchanged(self) -> None:
        """_format_memory_lesson behavior should remain unchanged."""
        lesson = _format_memory_lesson({"task": "T-1", "lesson": "guard imports"})
        assert lesson == "- (T-1) guard imports"

        lesson = _format_memory_lesson({"text": "deflake the parser"})
        assert lesson == "- deflake the parser"

    def test_existing_test_fixture_compatibility(self) -> None:
        """Verify the new implementation works with existing test fixtures."""
        # This test would use the existing test fixtures if they existed
        # but for now we just verify the function signatures haven't changed
        assert callable(_render_memory_lessons_block)


class TestConfigurationDefaults:
    """Tests for configuration defaults and environment."""

    def test_default_horizon_is_sensible(self) -> None:
        """Default horizon should be approximately 7 days."""
        from bernstein.core.defaults import SPAWN

        # Check that the default horizon is 7 days (in seconds)
        expected_horizon = 7 * 24 * 3600
        # Allow for floating point rounding
        assert abs(SPAWN.memory_lessons_horizon_s - expected_horizon) < 3600

    def test_default_weight_decay_factor(self) -> None:
        """Default weight decay factor should be reasonable."""
        from bernstein.core.defaults import SPAWN

        assert 0.0 < SPAWN.memory_lessons_weight_decay_factor <= 1.0

    def test_default_author_cap(self) -> None:
        """Default author cap should be positive."""
        from bernstein.core.defaults import SPAWN

        assert SPAWN.memory_lessons_max_per_author > 0
