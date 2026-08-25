"""Tests for agent lesson propagation system."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.lessons import (
    _MAX_LESSON_CHARS,
    _STALENESS_DAYS,
    _TRUNCATION_WARNING,
    compute_lesson_staleness,
    file_lesson,
    gather_lessons_for_context,
    get_lessons_for_agent,
    is_lesson_stale,
)
from bernstein.core.spawner import _extract_tags_from_tasks

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def temp_sdd_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory with memory subdirectory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    memory_dir = sdd / "memory"
    memory_dir.mkdir()
    return sdd


class TestFileLessonBasic:
    """Test basic lesson filing."""

    def test_file_lesson_creates_file(self, temp_sdd_dir: Path) -> None:
        """Filing a lesson creates lessons.jsonl if it doesn't exist."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        assert not lessons_path.exists()

        lesson_id = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Always use HTTPS for API calls",
            tags=["security", "api"],
            confidence=0.9,
        )

        assert lessons_path.exists()
        assert len(lesson_id) > 0

    def test_file_lesson_creates_jsonl_entry(self, temp_sdd_dir: Path) -> None:
        """Filed lesson is stored as JSON in JSONL file."""
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"

        lesson_id = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Test content",
            tags=["testing"],
            confidence=0.8,
        )

        lines = lessons_path.read_text().strip().split("\n")
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["lesson_id"] == lesson_id
        assert data["task_id"] == "task_001"
        assert data["filed_by_agent"] == "agent_a"
        assert data["content"] == "Test content"
        assert data["tags"] == ["testing"]
        assert data["confidence"] == pytest.approx(0.8)
        assert data["version"] == 1

    def test_file_lesson_normalizes_tags(self, temp_sdd_dir: Path) -> None:
        """Tags are lowercased and sorted."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Test",
            tags=["API", "Security", "api"],  # duplicates and mixed case
            confidence=0.8,
        )

        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        data = json.loads(lessons_path.read_text().strip())
        # Duplicates removed, sorted, lowercased
        assert data["tags"] == ["api", "security"]

    def test_file_lesson_clamps_confidence(self, temp_sdd_dir: Path) -> None:
        """Confidence is clamped to 0-1 range."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Test",
            tags=["test"],
            confidence=1.5,  # Out of range
        )

        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        data = json.loads(lessons_path.read_text().strip())
        assert data["confidence"] == pytest.approx(1.0)

        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_a",
            content="Test 2",
            tags=["test"],
            confidence=-0.5,  # Out of range
        )

        lines = lessons_path.read_text().strip().split("\n")
        data = json.loads(lines[1])
        assert data["confidence"] == pytest.approx(0.0)


class TestFileLessonDeduplication:
    """Test lesson deduplication (preventing identical lessons)."""

    def test_identical_lesson_updates_confidence(self, temp_sdd_dir: Path) -> None:
        """Filing identical lesson updates confidence instead of creating duplicate."""
        lesson_id_1 = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Never trust user input",
            tags=["security", "validation"],
            confidence=0.7,
        )

        # File again with higher confidence
        lesson_id_2 = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_b",
            content="Never trust user input",  # Same content
            tags=["validation", "security"],  # Same tags (different order)
            confidence=0.95,
        )

        # Should return the same lesson_id
        assert lesson_id_1 == lesson_id_2

        # File should have only one entry, with updated confidence and version
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lines = lessons_path.read_text().strip().split("\n")
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["confidence"] == pytest.approx(0.95)
        assert data["version"] == 2

    def test_different_lessons_stored_separately(self, temp_sdd_dir: Path) -> None:
        """Different lessons are stored separately."""
        id1 = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Lesson 1",
            tags=["auth"],
            confidence=0.8,
        )

        id2 = file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_a",
            content="Lesson 2",  # Different content
            tags=["auth"],
            confidence=0.8,
        )

        assert id1 != id2

        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lines = lessons_path.read_text().strip().split("\n")
        assert len(lines) == 2


class TestGetLessonsForAgent:
    """Test retrieving lessons by tag."""

    def test_get_lessons_empty_when_no_lessons(self, temp_sdd_dir: Path) -> None:
        """Getting lessons when none exist returns empty list."""
        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["auth"],
        )
        assert result == []

    def test_get_lessons_empty_when_no_tags(self, temp_sdd_dir: Path) -> None:
        """Getting lessons with empty tags returns empty list."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Test",
            tags=["auth"],
            confidence=0.8,
        )

        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=[],  # No tags
        )
        assert result == []

    def test_get_lessons_by_tag_overlap(self, temp_sdd_dir: Path) -> None:
        """Get lessons matches by tag overlap."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Auth lesson",
            tags=["auth", "security"],
            confidence=0.8,
        )

        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_a",
            content="Database lesson",
            tags=["database", "sql"],
            confidence=0.8,
        )

        # Query with tags matching the first lesson
        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["auth", "validation"],
        )

        assert len(result) == 1
        assert result[0].content == "Auth lesson"

    def test_get_lessons_respects_limit(self, temp_sdd_dir: Path) -> None:
        """Get lessons respects the limit parameter."""
        for i in range(5):
            file_lesson(
                sdd_dir=temp_sdd_dir,
                task_id=f"task_{i:03d}",
                agent_id="agent_a",
                content=f"Lesson {i}",
                tags=["common"],
                confidence=0.8,
            )

        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["common"],
            limit=2,
        )

        assert len(result) == 2

    def test_get_lessons_ranked_by_confidence(self, temp_sdd_dir: Path) -> None:
        """Retrieved lessons are ranked by confidence."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Low confidence",
            tags=["test"],
            confidence=0.5,
        )

        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_a",
            content="High confidence",
            tags=["test"],
            confidence=0.95,
        )

        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["test"],
        )

        assert len(result) == 2
        assert result[0].content == "High confidence"
        assert result[0].confidence == pytest.approx(0.95)
        assert result[1].content == "Low confidence"

    def test_get_lessons_handles_multiple_tag_overlap(self, temp_sdd_dir: Path) -> None:
        """Lessons with multiple matching tags rank higher."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Single tag",
            tags=["auth"],
            confidence=0.8,
        )

        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_002",
            agent_id="agent_a",
            content="Multiple tags",
            tags=["auth", "security", "validation"],
            confidence=0.8,
        )

        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["auth", "security"],
        )

        # Both should match, but "Multiple tags" has more overlap
        assert len(result) == 2
        assert result[0].content == "Multiple tags"


class TestLessonDecay:
    """Test lesson confidence decay over time."""

    def test_old_lesson_has_reduced_confidence(self, temp_sdd_dir: Path) -> None:
        """Lessons older than DECAY_DAYS have reduced confidence."""
        # File a lesson with a timestamp in the past (30+ days ago)
        lessons_path = temp_sdd_dir / "memory" / "lessons.jsonl"
        lessons_path.parent.mkdir(parents=True, exist_ok=True)

        old_time = time.time() - (35 * 24 * 3600)  # 35 days ago
        lesson = {
            "lesson_id": "test_id",
            "tags": ["test"],
            "content": "Old lesson",
            "confidence": 0.9,
            "created_timestamp": old_time,
            "filed_by_agent": "agent_a",
            "task_id": "task_001",
            "version": 1,
        }

        lessons_path.write_text(json.dumps(lesson) + "\n")

        result = get_lessons_for_agent(
            sdd_dir=temp_sdd_dir,
            task_tags=["test"],
        )

        assert len(result) == 1
        # Confidence should be decayed
        assert result[0].confidence < 0.9


class TestGatherLessonsForContext:
    """Test formatting lessons for agent context injection."""

    def test_gather_returns_empty_when_no_lessons(self, temp_sdd_dir: Path) -> None:
        """Gather returns empty string when no lessons match."""
        result = gather_lessons_for_context(
            sdd_dir=temp_sdd_dir,
            task_tags=["nonexistent"],
        )
        assert result == ""

    def test_gather_formats_lessons_for_context(self, temp_sdd_dir: Path) -> None:
        """Gather formats lessons into markdown for context injection."""
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="task_001",
            agent_id="agent_a",
            content="Use environment variables for secrets",
            tags=["security", "config"],
            confidence=0.9,
        )

        result = gather_lessons_for_context(
            sdd_dir=temp_sdd_dir,
            task_tags=["security"],
        )

        assert "Prior Agent Lessons" in result
        assert "security" in result
        assert "Use environment variables for secrets" in result
        assert "task_001" in result
        assert "0.90" in result or "0.9" in result

    def test_gather_includes_multiple_lessons(self, temp_sdd_dir: Path) -> None:
        """Gather includes all matching lessons."""
        for i in range(3):
            file_lesson(
                sdd_dir=temp_sdd_dir,
                task_id=f"task_{i:03d}",
                agent_id="agent_a",
                content=f"Lesson content {i}",
                tags=["testing"],
                confidence=0.8,
            )

        result = gather_lessons_for_context(
            sdd_dir=temp_sdd_dir,
            task_tags=["testing"],
        )

        # Should include all 3 lessons
        assert result.count("Lesson content") == 3
        assert result.count("task_") == 3


class TestExtractTagsFromTasks:
    """Test tag extraction from tasks for lesson lookup."""

    def test_extracts_role_as_tag(self) -> None:
        """Role is always included as a tag."""
        from bernstein.core.models import Task

        task = Task(id="t1", title="Fix bug", description="Fix it", role="backend")
        tags = _extract_tags_from_tasks([task])
        assert "backend" in tags

    def test_extracts_title_words(self) -> None:
        """Significant title words become tags."""
        from bernstein.core.models import Task

        task = Task(
            id="t1",
            title="Auth middleware rewrite",
            description="Rewrite auth",
            role="backend",
        )
        tags = _extract_tags_from_tasks([task])
        assert "auth" in tags
        assert "middleware" in tags
        assert "rewrite" in tags

    def test_filters_stop_words(self) -> None:
        """Common stop words are excluded."""
        from bernstein.core.models import Task

        task = Task(
            id="t1",
            title="Fix the broken API for users",
            description="desc",
            role="qa",
        )
        tags = _extract_tags_from_tasks([task])
        assert "the" not in tags
        assert "for" not in tags
        assert "fix" in tags
        assert "api" in tags

    def test_filters_short_words(self) -> None:
        """Words with 2 or fewer characters are excluded."""
        from bernstein.core.models import Task

        task = Task(id="t1", title="A DB fix", description="desc", role="backend")
        tags = _extract_tags_from_tasks([task])
        assert "db" not in tags
        assert "fix" in tags

    def test_multiple_tasks_merge_tags(self) -> None:
        """Tags from multiple tasks are merged."""
        from bernstein.core.models import Task

        tasks = [
            Task(id="t1", title="Auth system", description="desc", role="backend"),
            Task(id="t2", title="Database migration", description="desc", role="backend"),
        ]
        tags = _extract_tags_from_tasks(tasks)
        assert "auth" in tags
        assert "database" in tags
        assert "system" in tags
        assert "migration" in tags


class TestLessonSpawnerIntegration:
    """Test that lessons are injected into agent prompts."""

    def test_render_prompt_includes_lessons(self, temp_sdd_dir: Path) -> None:
        """Lessons matching task tags appear in the rendered prompt."""
        from unittest.mock import MagicMock

        from bernstein.core.spawner import _render_prompt

        from bernstein.core.tasks.artifacts import ArtifactSpec

        # File a lesson with a tag that will match
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="prior_task",
            agent_id="prior_agent",
            content="Always validate auth tokens before processing",
            tags=["auth", "security"],
            confidence=0.9,
        )

        # Create a mock task with title containing "auth"
        task = MagicMock()
        task.role = "backend"
        task.title = "Auth token validation"
        task.description = "Validate auth tokens"
        task.owned_files = []
        task.id = "task_001"
        task.mcp_servers = []
        task.parent_context = None
        task.depends_on = []
        # The renderer reads the artifact contract to decide whether the task
        # completes on a receipt or on a commit. A bare MagicMock answers that
        # question with a Mock, which is not `code_diff`, so the prompt would
        # try to render a contract for a task that has none.
        task.artifact_spec = ArtifactSpec()

        workdir = temp_sdd_dir.parent
        templates_dir = workdir / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        prompt = _render_prompt(
            [task],
            templates_dir,
            workdir,
        )

        assert "Prior Agent Lessons" in prompt
        assert "Always validate auth tokens before processing" in prompt

    def test_render_prompt_no_lessons_when_no_match(self, temp_sdd_dir: Path) -> None:
        """No lesson section when no tags match."""
        from unittest.mock import MagicMock

        from bernstein.core.spawner import _render_prompt

        from bernstein.core.tasks.artifacts import ArtifactSpec

        # File a lesson with unrelated tags
        file_lesson(
            sdd_dir=temp_sdd_dir,
            task_id="prior_task",
            agent_id="prior_agent",
            content="Database indexing tip",
            tags=["database", "performance"],
            confidence=0.9,
        )

        # Create a task with no matching tags
        task = MagicMock()
        task.role = "frontend"
        task.title = "CSS styling fix"
        task.description = "Fix CSS"
        task.owned_files = []
        task.id = "task_002"
        task.mcp_servers = []
        task.parent_context = None
        task.depends_on = []
        # The renderer reads the artifact contract to decide whether the task
        # completes on a receipt or on a commit. A bare MagicMock answers that
        # question with a Mock, which is not `code_diff`, so the prompt would
        # try to render a contract for a task that has none.
        task.artifact_spec = ArtifactSpec()

        workdir = temp_sdd_dir.parent
        templates_dir = workdir / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        prompt = _render_prompt(
            [task],
            templates_dir,
            workdir,
        )

        assert "Prior Agent Lessons" not in prompt


# ---------------------------------------------------------------------------
# Lesson staleness (T652)
# ---------------------------------------------------------------------------


class TestLessonStaleness:
    def test_compute_lesson_staleness(self) -> None:
        now = 1_000_000.0
        created = now - (2 * 86400)  # 2 days ago
        assert compute_lesson_staleness(created, now) == pytest.approx(2.0)

    def test_is_lesson_stale_when_fresh(self) -> None:
        now = 1_000_000.0
        created = now - (0.5 * 86400)  # 12 hours ago
        assert is_lesson_stale(created, now) is False

    def test_is_lesson_stale_when_old(self) -> None:
        now = 1_000_000.0
        created = now - (2 * 86400)  # 2 days ago
        assert is_lesson_stale(created, now) is True

    def test_staleness_constant(self) -> None:
        assert _STALENESS_DAYS == 1


class TestGatherLessonsWithStaleness:
    def test_fresh_lesson_no_staleness_warning(self, tmp_path: Path) -> None:
        """Fresh lessons (<1d old) should not have staleness warnings."""
        sdd = tmp_path / ".sdd"
        now = 1_000_000.0
        # File lesson with timestamp 12 hours ago
        lessons_path = sdd / "memory"
        lessons_path.mkdir(parents=True)
        created_ts = now - (0.5 * 86400)
        lesson_data = {
            "lesson_id": "lesson-1",
            "tags": ["auth"],
            "content": "Always check JWT tokens.",
            "confidence": 0.9,
            "created_timestamp": created_ts,
            "filed_by_agent": "agent-1",
            "task_id": "task-1",
            "version": 1,
        }
        (lessons_path / "lessons.jsonl").write_text(json.dumps(lesson_data))

        result = gather_lessons_for_context(sdd, ["auth"], now=now)
        assert "Always check JWT tokens" in result
        assert "may be outdated" not in result

    def test_stale_lesson_has_staleness_warning(self, tmp_path: Path) -> None:
        """Lessions >1d old should include staleness caveat."""
        sdd = tmp_path / ".sdd"
        now = 1_000_000.0
        # File lesson with timestamp 3 days ago
        lessons_path = sdd / "memory"
        lessons_path.mkdir(parents=True)
        created_ts = now - (3 * 86400)
        lesson_data = {
            "lesson_id": "lesson-1",
            "tags": ["auth"],
            "content": "Always use bcrypt for passwords.",
            "confidence": 0.9,
            "created_timestamp": created_ts,
            "filed_by_agent": "agent-1",
            "task_id": "task-1",
            "version": 1,
        }
        (lessons_path / "lessons.jsonl").write_text(json.dumps(lesson_data))

        result = gather_lessons_for_context(sdd, ["auth"], now=now)
        assert "Always use bcrypt" in result
        assert "may be outdated" in result
        assert "3" in result  # age in days


class TestMemoryTruncation:
    """T654 - truncation warnings when memory exceeds budget."""

    def test_truncation_warning_when_exceeds_budget(self, tmp_path: Path) -> None:
        """When lessons overflow, a truncation notice should be appended."""
        sdd = tmp_path / ".sdd"
        lessons_path = sdd / "memory"
        lessons_path.mkdir(parents=True)

        # Write a single very long lesson
        content = "A" * 5000  # Exceeds default budget of 4000
        now = 1_000_000.0
        lesson_data = {
            "lesson_id": "lesson-1",
            "tags": ["auth"],
            "content": content,
            "confidence": 0.9,
            "created_timestamp": now,
            "filed_by_agent": "agent-1",
            "task_id": "task-1",
            "version": 1,
        }
        (lessons_path / "lessons.jsonl").write_text(json.dumps(lesson_data))

        result = gather_lessons_for_context(sdd, ["auth"], now=now)
        # Should be truncated with warning
        assert len(result) < len(content) + 50  # some overhead
        assert "context window limits" in result

    def test_no_truncation_when_under_budget(self, tmp_path: Path) -> None:
        """Short lessons should not trigger truncation."""
        sdd = tmp_path / ".sdd"
        lessons_path = sdd / "memory"
        lessons_path.mkdir(parents=True)

        now = 1_000_000.0
        lesson_data = {
            "lesson_id": "lesson-1",
            "tags": ["auth"],
            "content": "Keep passwords short.",
            "confidence": 0.9,
            "created_timestamp": now,
            "filed_by_agent": "agent-1",
            "task_id": "task-1",
            "version": 1,
        }
        (lessons_path / "lessons.jsonl").write_text(json.dumps(lesson_data))

        result = gather_lessons_for_context(sdd, ["auth"], now=now)
        assert _TRUNCATION_WARNING not in result
        assert "Keep passwords short" in result

    def test_truncation_respects_custom_limit(self, tmp_path: Path) -> None:
        """Custom max_chars should be honoured."""
        sdd = tmp_path / ".sdd"
        lessons_path = sdd / "memory"
        lessons_path.mkdir(parents=True)

        now = 1_000_000.0
        lesson_data = {
            "lesson_id": "lesson-1",
            "tags": ["auth"],
            "content": "Test lesson for truncation.",
            "confidence": 0.9,
            "created_timestamp": now,
            "filed_by_agent": "agent-1",
            "task_id": "task-1",
            "version": 1,
        }
        (lessons_path / "lessons.jsonl").write_text(json.dumps(lesson_data))

        result = gather_lessons_for_context(sdd, ["auth"], now=now, max_chars=50)
        assert "context window limits" in result

    def test_truncation_constants(self) -> None:
        """Verify the constants are positive and non-empty."""
        assert _MAX_LESSON_CHARS > 0
        assert len(_TRUNCATION_WARNING) > 0


# ---------------------------------------------------------------------------
# Convention receipts: review corrections & audit chain (Issue #3750)
# ---------------------------------------------------------------------------


class TestConventionReceipts:
    """Convention receipt lifecycle, deduplication, conflict detection, and expiration."""

    def test_file_review_correction_calls_file_lesson_and_persists_receipt(self, tmp_path: Path) -> None:
        """A review correction calls file_lesson() and persists an attested convention receipt."""
        from bernstein.core.knowledge.conventions import file_review_correction

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)
        src_file = tmp_path / "src" / "api.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("def handle_request(): pass\n", encoding="utf-8")

        receipt = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Always validate request payloads before dispatching",
            subject_path="src/api.py",
            base_commit_sha="1111222233334444555566667777888899990000",
            subject_symbol="handle_request",
            filing_finding_id="review-pr-42",
            decided_by="reviewer-bob",
        )

        # Assert lesson lands where file_lesson() writes
        lessons_path = sdd_dir / "memory" / "lessons.jsonl"
        assert lessons_path.is_file()
        lesson_line = json.loads(lessons_path.read_text(encoding="utf-8").strip())
        assert lesson_line["content"] == "Always validate request payloads before dispatching"
        assert lesson_line["lesson_id"] == receipt.lesson_id

        # Assert convention receipt persisted
        receipt_file = sdd_dir / "conventions" / "receipts" / f"{receipt.receipt_id}.json"
        assert receipt_file.is_file()
        stored = json.loads(receipt_file.read_text(encoding="utf-8"))
        assert stored["receipt_id"] == receipt.receipt_id
        assert stored["version"] == 1
        assert stored["status"] == "active"
        assert stored["signature"] != ""

    def test_file_review_correction_thrice_yields_version_three(self, tmp_path: Path) -> None:
        """Filing the same correction three times yields one receipt at version: 3."""
        from bernstein.core.knowledge.conventions import file_review_correction

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        r1 = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Sanitize log sink inputs",
            subject_path="src/logger.py",
            base_commit_sha="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            decided_by="reviewer-1",
        )
        assert r1.version == 1

        r2 = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Sanitize log sink inputs",
            subject_path="src/logger.py",
            base_commit_sha="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            decided_by="reviewer-2",
        )
        assert r2.receipt_id == r1.receipt_id
        assert r2.version == 2

        r3 = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Sanitize log sink inputs",
            subject_path="src/logger.py",
            base_commit_sha="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            decided_by="reviewer-3",
        )
        assert r3.receipt_id == r1.receipt_id
        assert r3.version == 3

        # Exactly one receipt file on disk
        receipt_files = list((sdd_dir / "conventions" / "receipts").glob("*.json"))
        assert len(receipt_files) == 1

        # Lessons store also has 1 deduplicated entry
        lessons_path = sdd_dir / "memory" / "lessons.jsonl"
        lines = [line for line in lessons_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["version"] == 3

    def test_conflicting_assertions_rejected_at_file_time(self, tmp_path: Path) -> None:
        """Overlapping path with conflicting assertion is rejected naming both receipts."""
        from bernstein.core.knowledge.conventions import file_review_correction

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        r1 = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Always use snake_case for handlers",
            subject_path="src/handlers/*.py",
            base_commit_sha="commit-1",
            assertion_ref={"kind": "regex_in_file", "target": "src/handlers/auth.py::def handle_"},
        )

        with pytest.raises(ValueError) as exc_info:
            file_review_correction(
                sdd_dir=sdd_dir,
                workdir=tmp_path,
                rule_text="Never use snake_case for handlers",
                subject_path="src/handlers/auth.py",
                base_commit_sha="commit-2",
                assertion_ref={"kind": "regex_in_file", "target": "src/handlers/auth.py::def handleAuth"},
            )

        err_msg = str(exc_info.value)
        assert "Convention conflict" in err_msg
        assert r1.receipt_id in err_msg

    def test_symbol_expiration_removes_from_active_rules(self, tmp_path: Path) -> None:
        """A rule whose subject_symbol is deleted reports expired and stops being active."""
        from bernstein.core.knowledge.conventions import (
            file_review_correction,
            get_active_conventions,
        )

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)
        py_file = tmp_path / "src" / "worker.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text("class OldWorker:\n    pass\n", encoding="utf-8")

        r = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="OldWorker must subclass BaseWorker",
            subject_path="src/worker.py",
            subject_symbol="OldWorker",
            base_commit_sha="commit-worker",
        )

        # When symbol exists at HEAD, rule is active
        active_before, _ = get_active_conventions(sdd_dir, workdir=tmp_path)
        assert any(x.receipt_id == r.receipt_id for x in active_before)

        # Delete symbol from worker.py (e.g. refactored/removed)
        py_file.write_text("class NewWorker:\n    pass\n", encoding="utf-8")

        # After symbol removal, rule is omitted from active conventions
        active_after, _ = get_active_conventions(sdd_dir, workdir=tmp_path)
        assert not any(x.receipt_id == r.receipt_id for x in active_after)

    def test_active_ruleset_hash_deterministic(self, tmp_path: Path) -> None:
        """Two calls with identical active conventions render identical ruleset_hash."""
        from bernstein.core.knowledge.conventions import (
            file_review_correction,
            get_active_conventions,
        )

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Rule A",
            subject_path="src/a.py",
            base_commit_sha="commit-a",
        )
        file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Rule B",
            subject_path="src/b.py",
            base_commit_sha="commit-b",
        )

        active1, hash1 = get_active_conventions(sdd_dir)
        _active2, hash2 = get_active_conventions(sdd_dir)

        assert len(active1) == 2
        assert hash1 == hash2
        assert hash1.startswith("sha256:")

    def test_retire_convention_receipt_records_chain_event(self, tmp_path: Path) -> None:
        """Retiring a convention receipt updates status and appends convention.retired audit event."""
        from bernstein.core.knowledge.conventions import (
            file_review_correction,
            get_active_conventions,
            retire_convention_receipt,
            verify_conventions_audit,
        )

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        r = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Temporary convention to be retired",
            subject_path="src/temp.py",
            base_commit_sha="commit-temp",
        )

        active_before, _ = get_active_conventions(sdd_dir)
        assert len(active_before) == 1

        retired_r = retire_convention_receipt(
            sdd_dir=sdd_dir,
            receipt_id=r.receipt_id,
            retired_by="maintainer-carol",
            reason="Superseded by new linter rule",
        )

        assert retired_r.status == "retired"
        active_after, _ = get_active_conventions(sdd_dir)
        assert len(active_after) == 0

        # Audit verification must pass for clean retirement
        audit_res = verify_conventions_audit(sdd_dir)
        assert audit_res.valid
        assert len(audit_res.errors) == 0

    def test_unrelated_assertions_on_one_file_are_not_a_conflict(self, tmp_path: Path) -> None:
        """Two rules that merely touch the same file both file cleanly.

        "Cannot both hold" is the conflict test, not "names the same file".
        Rejecting two independent assertions over one file refuses legitimate
        filings, which is worse than accepting them: the operator is told a
        contradiction exists where none does.
        """
        from bernstein.core.knowledge.conventions import file_review_correction

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        first = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="Log sinks in the auth module take sanitised values",
            subject_path="src/handlers/auth.py",
            base_commit_sha="commit-1",
            assertion_ref={"kind": "regex_in_file", "target": "src/handlers/auth.py::sanitize_log"},
        )
        second = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="The auth module keeps an explicit timeout on every outbound call",
            subject_path="src/handlers/auth.py",
            base_commit_sha="commit-2",
            assertion_ref={"kind": "regex_in_file", "target": "src/handlers/auth.py::timeout="},
        )

        assert first.receipt_id != second.receipt_id
        receipts_dir = sdd_dir / "conventions" / "receipts"
        assert len(sorted(receipts_dir.glob("*.json"))) == 2

    def test_negated_assertion_on_the_same_target_is_a_conflict(self, tmp_path: Path) -> None:
        """Same target asserted with opposite polarity is rejected, naming both ids."""
        from bernstein.core.knowledge.conventions import file_review_correction

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)

        first = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="The auth module imports the shared sanitiser",
            subject_path="src/handlers/auth.py",
            base_commit_sha="commit-1",
            assertion_ref={"kind": "import_resolves", "target": "bernstein.core.security.sanitize"},
        )
        with pytest.raises(ValueError) as exc_info:
            file_review_correction(
                sdd_dir=sdd_dir,
                workdir=tmp_path,
                rule_text="The auth module keeps the shared sanitiser out of its imports",
                subject_path="src/handlers/auth.py",
                base_commit_sha="commit-2",
                assertion_ref={"kind": "import_resolves", "target": "!bernstein.core.security.sanitize"},
            )

        assert first.receipt_id in str(exc_info.value)

    def test_ruleset_hash_ignores_receipt_identity_and_filing_time(self, tmp_path: Path) -> None:
        """Two installs with the same rules in force agree on ruleset_hash.

        Receipt ids are minted per install and filing timestamps are wall
        clock, so folding either into the digest makes it a fingerprint of one
        install's filing history rather than of the rules in force.
        """
        from bernstein.core.knowledge.conventions import (
            ConventionReceipt,
            compute_ruleset_hash,
        )

        base = {
            "rule_text": "Sanitise caller-derived values before a log sink",
            "rule_text_hash": "a" * 64,
            "subject_path": "src/handlers/auth.py",
            "base_commit_sha": "commit-1",
        }
        operator_a = ConventionReceipt(
            receipt_id="11111111-1111-1111-1111-111111111111",
            created_timestamp=1_700_000_000.0,
            lesson_id="lesson-a",
            filing_finding_id="finding-a",
            decided_by="maintainer-a",
            **base,
        )
        operator_b = ConventionReceipt(
            receipt_id="22222222-2222-2222-2222-222222222222",
            created_timestamp=1_800_000_000.0,
            lesson_id="lesson-b",
            filing_finding_id="finding-b",
            decided_by="maintainer-b",
            **base,
        )

        assert operator_a.to_canonical_bytes() != operator_b.to_canonical_bytes()
        assert compute_ruleset_hash([operator_a]) == compute_ruleset_hash([operator_b])

    def test_ruleset_hash_changes_when_a_rule_changes(self, tmp_path: Path) -> None:
        """The digest still moves on anything that changes what a rule demands."""
        from bernstein.core.knowledge.conventions import (
            ConventionReceipt,
            compute_ruleset_hash,
        )

        receipt = ConventionReceipt(
            receipt_id="r-1",
            rule_text="Sanitise caller-derived values before a log sink",
            rule_text_hash="a" * 64,
            subject_path="src/handlers/auth.py",
            base_commit_sha="commit-1",
        )
        moved_commit = ConventionReceipt(
            receipt_id="r-1",
            rule_text=receipt.rule_text,
            rule_text_hash=receipt.rule_text_hash,
            subject_path=receipt.subject_path,
            base_commit_sha="commit-2",
        )
        other_text = ConventionReceipt(
            receipt_id="r-1",
            rule_text="Something else entirely",
            rule_text_hash="b" * 64,
            subject_path=receipt.subject_path,
            base_commit_sha=receipt.base_commit_sha,
        )

        assert compute_ruleset_hash([receipt]) != compute_ruleset_hash([moved_commit])
        assert compute_ruleset_hash([receipt]) != compute_ruleset_hash([other_text])

    def test_expired_symbol_is_recorded_in_the_chain(self, tmp_path: Path) -> None:
        """An expired rule reports 'expired' and says so in the chain.

        Dropping it from the active projection alone leaves an operator unable
        to tell a rule that expired from a rule that was never filed -- the
        exact ambiguity the receipt exists to close.
        """
        import json as _json

        from bernstein.core.knowledge.conventions import (
            file_review_correction,
            get_active_conventions,
            verify_conventions_audit,
        )
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import (
            EVENT_CONVENTION_RETIRED,
            AuditChainStore,
        )

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)
        py_file = tmp_path / "src" / "worker.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text("class OldWorker:\n    pass\n", encoding="utf-8")

        r = file_review_correction(
            sdd_dir=sdd_dir,
            workdir=tmp_path,
            rule_text="OldWorker must subclass BaseWorker",
            subject_path="src/worker.py",
            subject_symbol="OldWorker",
            base_commit_sha="commit-worker",
        )

        py_file.write_text("class NewWorker:\n    pass\n", encoding="utf-8")
        active_after, _ = get_active_conventions(sdd_dir, workdir=tmp_path)
        assert not any(x.receipt_id == r.receipt_id for x in active_after)

        stored = _json.loads((sdd_dir / "conventions" / "receipts" / f"{r.receipt_id}.json").read_text())
        assert stored["status"] == "expired"

        chain = AuditChainStore(sdd_dir / "audit", key=load_or_create_audit_key())
        events = chain.query(event_type=EVENT_CONVENTION_RETIRED)
        assert [e for e in events if e.details.get("receipt_id") == r.receipt_id]

        # The expiry is a clean chain transition, not a tamper signal.
        audit_res = verify_conventions_audit(sdd_dir)
        assert audit_res.valid, audit_res.errors
