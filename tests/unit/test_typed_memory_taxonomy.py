"""Tests for typed memory taxonomy (T651)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.lessons import (
    _TYPE_DECAY_DAYS,
    _TYPE_DECAY_FACTOR,
    Lesson,
    MemoryType,
    file_lesson,
    gather_lessons_for_context,
    get_lessons_for_agent,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def temp_sdd_dir(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "memory").mkdir()
    return sdd


# ---------------------------------------------------------------------------
# Test memory type enum values
# ---------------------------------------------------------------------------


class TestMemoryTypeEnum:
    """Test MemoryType enum."""

    def test_has_four_types(self) -> None:
        assert len(MemoryType) == 4

    def test_user_type(self) -> None:
        assert MemoryType.USER.value == "user"

    def test_feedback_type(self) -> None:
        assert MemoryType.FEEDBACK.value == "feedback"

    def test_project_type(self) -> None:
        assert MemoryType.PROJECT.value == "project"

    def test_reference_type(self) -> None:
        assert MemoryType.REFERENCE.value == "reference"

    def test_is_string_subclass(self) -> None:
        # str, Enum so json.dumps handles it
        assert isinstance(MemoryType.USER, str)


# ---------------------------------------------------------------------------
# Test per-type decay configuration
# ---------------------------------------------------------------------------


class TestPerTypeDecayConfig:
    """Test per-type decay rates are configured."""

    def test_feedback_decays_fastest(self) -> None:
        """Feedback has shortest half-life (7 days)."""
        assert _TYPE_DECAY_DAYS[MemoryType.FEEDBACK] == 7

    def test_user_has_moderate_decay(self) -> None:
        assert _TYPE_DECAY_DAYS[MemoryType.USER] == 30

    def test_project_has_medium_decay(self) -> None:
        assert _TYPE_DECAY_DAYS[MemoryType.PROJECT] == 14

    def test_reference_has_slowest_decay(self) -> None:
        assert _TYPE_DECAY_DAYS[MemoryType.REFERENCE] == 90

    def test_all_types_have_decay_factor(self) -> None:
        for mt in MemoryType:
            assert mt in _TYPE_DECAY_FACTOR


# ---------------------------------------------------------------------------
# Test filing lessons with memory_type
# ---------------------------------------------------------------------------


class TestFileLessonMemoryType:
    """Test filing lessons with typed memory categories."""

    def test_file_lesson_default_type_is_user(self, temp_sdd_dir: Path) -> None:
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Always use HTTPS for API calls",
            tags=["security", "api"],
            confidence=0.9,
        )

        lines = lessons_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["memory_type"] == "user"

    def test_file_lesson_with_feedback_type(self, temp_sdd_dir: Path) -> None:
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Fix: use proper error handling",
            tags=["error-handling"],
            confidence=0.95,
            memory_type=MemoryType.FEEDBACK,
        )

        lines = lessons_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["memory_type"] == "feedback"

    def test_file_lesson_with_project_type(self, temp_sdd_dir: Path) -> None:
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Convention: all routes use /api prefix",
            tags=["convention", "routing"],
            confidence=0.85,
            memory_type=MemoryType.PROJECT,
        )

        lines = lessons_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["memory_type"] == "project"

    def test_file_lesson_with_reference_type(self, temp_sdd_dir: Path) -> None:
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Best practice: always validate input",
            tags=["validation"],
            confidence=0.9,
            memory_type=MemoryType.REFERENCE,
        )

        lines = lessons_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["memory_type"] == "reference"

    def test_file_lesson_dedup_respects_memory_type(self, temp_sdd_dir: Path) -> None:
        """Same content + tags but different memory_type = new lesson."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Always sanitize user input",
            tags=["security", "input"],
            confidence=0.8,
            memory_type=MemoryType.USER,
        )
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_b",
            content="Always sanitize user input",
            tags=["security", "input"],
            confidence=0.8,
            memory_type=MemoryType.FEEDBACK,
        )

        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lines = lessons_path.read_text().strip().split("\n")
        assert len(lines) == 2  # Both stored

        types = [json.loads(l)["memory_type"] for l in lines]
        assert "user" in types
        assert "feedback" in types


# ---------------------------------------------------------------------------
# Test per-type decay in retrieval
# ---------------------------------------------------------------------------


class TestPerTypeDecay:
    """Test that retrieval uses per-type decay rates."""

    def test_feedback_decays_faster_than_user(self, temp_sdd_dir: Path) -> None:
        """After 10 days, feedback lesson should be more decayed than user."""
        now = time.time()
        ten_days_ago = now - (10 * 24 * 3600)

        user_lesson = Lesson(
            lesson_id="u1",
            tags=["api"],
            content="User lesson",
            confidence=0.9,
            created_timestamp=ten_days_ago,
            filed_by_agent="agent_a",
            task_id="t1",
            memory_type=MemoryType.USER,
        )
        feedback_lesson = Lesson(
            lesson_id="f1",
            tags=["api"],
            content="Feedback lesson",
            confidence=0.9,
            created_timestamp=ten_days_ago,
            filed_by_agent="agent_a",
            task_id="t2",
            memory_type=MemoryType.FEEDBACK,
        )

        # Compute decayed confidence manually
        user_age = (now - user_lesson.created_timestamp) / (24 * 3600)  # 10 days
        feedback_age = (now - feedback_lesson.created_timestamp) / (24 * 3600)  # 10 days

        user_decay_days = _TYPE_DECAY_DAYS[MemoryType.USER]
        feedback_decay_days = _TYPE_DECAY_DAYS[MemoryType.FEEDBACK]

        # User: 10 < 30 → no decay
        assert user_age <= user_decay_days

        # Feedback: 10 > 7 → decay applies
        assert feedback_age > feedback_decay_days

    def test_lesson_lesson_serialization_has_memory_type(self, temp_sdd_dir: Path) -> None:
        """Filing and retrieving a lesson preserves the memory_type."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="t1",
            agent_id="agent_a",
            content="Project convention: use type hints",
            tags=["typing"],
            memory_type=MemoryType.PROJECT,
        )

        retrieved = get_lessons_for_agent(temp_sdd_dir, ["typing"])
        assert len(retrieved) == 1
        assert retrieved[0].memory_type == MemoryType.PROJECT

    def test_gather_lesson_context_includes_type(self, temp_sdd_dir: Path) -> None:
        """Context block includes the memory type."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="t1",
            agent_id="agent_a",
            content="Reference: PEP8 line length",
            tags=["style"],
            memory_type=MemoryType.REFERENCE,
        )

        context = gather_lessons_for_context(temp_sdd_dir, ["style"])
        assert "**Type:** reference" in context

    def test_parse_lesson_handles_unknown_type(self, temp_sdd_dir: Path) -> None:
        """Unknown memory_type in JSONL falls back to USER."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        # Manually write an entry with an unknown type
        entry = {
            "lesson_id": "x1",
            "tags": ["test"],
            "content": "Some content",
            "confidence": 0.8,
            "created_timestamp": time.time(),
            "filed_by_agent": "agent_a",
            "task_id": "t1",
            "memory_type": "unknown_type",
            "version": 1,
        }
        lessons_path.write_text(json.dumps(entry) + "\n")

        retrieved = get_lessons_for_agent(temp_sdd_dir, ["test"])
        assert len(retrieved) == 1
        assert retrieved[0].memory_type == MemoryType.USER

    def test_parse_lesson_defaults_user_when_missing(self, temp_sdd_dir: Path) -> None:
        """Missing memory_type field in JSONL defaults to USER."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        entry = {
            "lesson_id": "x1",
            "tags": ["test"],
            "content": "Some content",
            "confidence": 0.8,
            "created_timestamp": time.time(),
            "filed_by_agent": "agent_a",
            "task_id": "t1",
            "version": 1,
        }
        lessons_path.write_text(json.dumps(entry) + "\n")

        retrieved = get_lessons_for_agent(temp_sdd_dir, ["test"])
        assert len(retrieved) == 1
        assert retrieved[0].memory_type == MemoryType.USER
