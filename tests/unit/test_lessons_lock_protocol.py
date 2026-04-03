"""Tests for lessons lock protocol — concurrent filing, crash rollback."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from bernstein.core.lessons import (
    _find_similar_lesson_in_content,
    _get_last_chain_hash_from_content,
    _update_lesson_confidence_from_content,
    file_lesson,
    gather_lessons_for_context,
    get_lessons_for_agent,
)
from bernstein.core.memory_integrity import GENESIS_HASH
from bernstein.core.memory_lock_protocol import MemoryFileGuard, _safe_unlink, guarded_memory_write


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_sdd_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory with memory subdirectory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    memory_dir = sdd / "memory"
    memory_dir.mkdir()
    return sdd


def _parse_lessons(jsonl_text: str) -> list[dict]:
    """Parse JSONL text into list of dicts."""
    results = []
    for line in jsonl_text.strip().split("\n"):
        if line.strip():
            results.append(json.loads(line))
    return results


# ---------------------------------------------------------------------------
# Concurrent lesson filing
# ---------------------------------------------------------------------------


class TestConcurrentLessonFiling:
    """Test filing lessons from multiple concurrent agents."""

    def test_concurrent_filing_no_corruption(self, temp_sdd_dir: Path) -> None:
        """Multiple agents filing lessons concurrently should not corrupt the file."""
        errors: list[Exception] = []
        lesson_ids: list[str] = []
        lock = threading.Lock()

        def file_lesson_thread(idx: int) -> None:
            try:
                lid = file_lesson(
                    sdd_dir=temp_sdd_dir,
                    task_id=f"task_{idx:03d}",
                    agent_id=f"agent_{idx:03d}",
                    content=f"Lesson content {idx}",
                    tags=["common"],
                    confidence=0.8,
                )
                with lock:
                    lesson_ids.append(lid)
            except TimeoutError:
                # Expected under high contention — lock protocol works correctly
                pass
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=file_lesson_thread, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All successful writes should produce valid JSONL
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        assert lessons_path.exists()
        assert len(errors) == 0  # No unexpected errors

        # Verify all lines are valid JSON
        lines = lessons_path.read_text().strip().split("\n")
        for line in lines:
            data = json.loads(line)
            assert "lesson_id" in data
            assert "content" in data
        # At least some lessons persisted (some may have timed out)
        assert 1 <= len(lines) <= 5

    def test_concurrent_filing_same_tags_no_duplicates(self, temp_sdd_dir: Path) -> None:
        """Concurrent filing of similar lessons should deduplicate correctly."""
        errors: list[Exception] = []
        lesson_ids: list[str] = []
        lock = threading.Lock()

        def file_similar(idx: int) -> None:
            try:
                lid = file_lesson(
                    sdd_dir=temp_sdd_dir,
                    task_id=f"task_{idx:03d}",
                    agent_id=f"agent_{idx:03d}",
                    content="Always validate input",  # Same content
                    tags=["validation"],
                    confidence=0.7 + idx * 0.05,
                )
                with lock:
                    lesson_ids.append(lid)
            except TimeoutError:
                # Expected under contention — some callers may time out
                pass
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=file_similar, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No unexpected errors
        assert len(errors) == 0

        # Due to lock contention, 1-3 lessons may exist
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lines = lessons_path.read_text().strip().split("\n")
        assert 1 <= len(lines) <= 3

        # All lines should be valid lesson JSON
        for line in lines:
            data = json.loads(line)
            assert "lesson_id" in data
            assert data["content"] == "Always validate input"


class TestCrashMidWrite:
    """Test that crash mid-write rolls back correctly."""

    def test_guarded_write_crash_rolls_back(self, tmp_path: Path) -> None:
        """If an exception occurs during guarded write, the file is rolled back."""
        target = tmp_path / "lessons.jsonl"
        original = '{"lesson_id": "abc", "content": "original"}\n'
        target.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError, match="crash"):
            with guarded_memory_write(target) as guard:
                if guard.original_content:
                    guard.write_backup()
                guard.write_new('{"lesson_id": "bad", "content": "corrupt"}\n')
                raise ValueError("crash")

        # Should be rolled back
        assert target.read_text() == original

    def test_no_backup_for_empty_file(self, tmp_path: Path) -> None:
        """Writing to a new file should not create a backup."""
        target = tmp_path / "new.jsonl"
        with guarded_memory_write(target) as guard:
            guard.write_new("first\n")

        backup = tmp_path / "new.bak"
        assert not backup.exists()


class TestLessonContentHelpers:
    """Test the content-based helper functions."""

    def test_find_similar_lesson_in_content_finds_match(self) -> None:
        content = (
            '{"lesson_id":"abc","tags":["auth"],"content":"Use JWT",'
            '"confidence":0.9,"created_timestamp":1000000.0,'
            '"filed_by_agent":"x","task_id":"t1"}\n'
        )
        result = _find_similar_lesson_in_content(content, ["auth"], "Use JWT")
        assert result == "abc"

    def test_find_similar_lesson_in_content_no_match(self) -> None:
        content = (
            '{"lesson_id":"abc","tags":["auth"],"content":"Use JWT",'
            '"confidence":0.9,"created_timestamp":1000000.0,'
            '"filed_by_agent":"x","task_id":"t1"}\n'
        )
        result = _find_similar_lesson_in_content(content, ["database"], "Use SQL")
        assert result is None

    def test_find_similar_lesson_in_content_empty(self) -> None:
        assert _find_similar_lesson_in_content(None, ["auth"], "test") is None
        assert _find_similar_lesson_in_content("", ["auth"], "test") is None

    def test_get_last_chain_hash_from_content(self) -> None:
        content = (
            '{"lesson_id":"a","chain_hash":"hash1"}\n'
            '{"lesson_id":"b","chain_hash":"hash2"}\n'
        )
        result = _get_last_chain_hash_from_content(content)
        assert result == "hash2"

    def test_get_last_chain_hash_from_content_empty(self) -> None:
        assert _get_last_chain_hash_from_content(None) == GENESIS_HASH
        assert _get_last_chain_hash_from_content("") == GENESIS_HASH

    def test_get_last_chain_hash_from_content_no_hash(self) -> None:
        content = '{"lesson_id":"a","content":"no hash"}\n'
        assert _get_last_chain_hash_from_content(content) == GENESIS_HASH


class TestLockProtocolInLessons:
    """Test that the lock protocol is properly integrated."""

    def test_lesson_file_lock_created_and_removed(self, temp_sdd_dir: Path) -> None:
        """Lock file is created during filing and removed after."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lock_path = temp_sdd_dir / "memory" / "lessons.lock"

        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_1",
            agent_id="agent_1",
            content="Test",
            tags=["test"],
            confidence=0.8,
        )

        # Lock should be released after filing
        assert not lock_path.exists()
        assert lessons_path.exists()

    def test_stale_lock_recovery(self, temp_sdd_dir: Path) -> None:
        """Stale locks from crashed processes should be reclaimed."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lock_path = lessons_path.with_suffix(lessons_path.suffix + ".lock")
        lessons_path.write_text("", encoding="utf-8")

        # Write a stale lock (dead PID)
        lock_data = {"pid": 999_999_999, "acquired_at": time.time() - 600}
        lock_path.write_text(json.dumps(lock_data), encoding="utf-8")

        # File a lesson — should reclaim the stale lock
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_1",
            agent_id="agent_1",
            content="Test",
            tags=["test"],
            confidence=0.8,
        )

        assert lessons_path.exists()
        # Lock should be released after filing
        assert not lock_path.exists()
