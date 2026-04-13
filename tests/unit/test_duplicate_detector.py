"""Tests for the duplicate task detector."""

from __future__ import annotations

import pytest
from bernstein.core.models import Task, TaskStatus

from bernstein.core.quality.duplicate_detector import (
    compute_word_overlap,
    detect_duplicates,
    merge_duplicate_tasks,
    normalize_text,
)

# --- Helpers ---


def _make_task(
    *,
    task_id: str = "T-1",
    title: str = "Implement feature",
    description: str = "Detailed description",
    role: str = "backend",
    priority: int = 2,
    status: TaskStatus = TaskStatus.OPEN,
    completion_signals: list[str] | None = None,
    owned_files: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role=role,
        priority=priority,
        status=status,
        completion_signals=completion_signals or [],
        owned_files=owned_files or [],
        depends_on=depends_on or [],
    )


# --- normalize_text ---


class TestNormalizeText:
    """Tests for normalize_text()."""

    def test_lowercases(self) -> None:
        assert normalize_text("Hello World") == "hello world"

    def test_removes_punctuation(self) -> None:
        assert normalize_text("Hello, World!") == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert normalize_text("  hello   world  ") == "hello world"

    def test_empty_string(self) -> None:
        assert normalize_text("") == ""

    def test_strips_special_chars(self) -> None:
        # \w includes underscores, so _ is preserved while - is stripped
        assert normalize_text("foo-bar_baz") == "foobar_baz"


# --- compute_word_overlap ---


class TestComputeWordOverlap:
    """Tests for compute_word_overlap()."""

    def test_identical_texts(self) -> None:
        assert compute_word_overlap("hello world", "hello world") == pytest.approx(1.0)

    def test_completely_different(self) -> None:
        assert compute_word_overlap("foo bar", "baz qux") == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        sim = compute_word_overlap("add auth module", "add auth tests")
        assert 0.0 < sim < 1.0

    def test_empty_string(self) -> None:
        assert compute_word_overlap("", "hello") == pytest.approx(0.0)
        assert compute_word_overlap("hello", "") == pytest.approx(0.0)

    def test_both_empty(self) -> None:
        assert compute_word_overlap("", "") == pytest.approx(0.0)


# --- detect_duplicates ---


class TestDetectDuplicates:
    """Tests for detect_duplicates()."""

    def test_no_duplicates(self) -> None:
        tasks = [
            _make_task(task_id="T-1", title="Add auth module", description="Build authentication"),
            _make_task(task_id="T-2", title="Fix database bug", description="Repair SQL queries"),
        ]
        dupes = detect_duplicates(tasks)
        assert dupes == []

    def test_detects_similar_tasks(self) -> None:
        tasks = [
            _make_task(task_id="T-1", title="Add authentication module", description="Build the auth system"),
            _make_task(task_id="T-2", title="Build authentication module", description="Add the auth system"),
        ]
        dupes = detect_duplicates(tasks, threshold=0.5)
        assert len(dupes) >= 1
        assert dupes[0][0] == "T-1"
        assert dupes[0][1] == "T-2"

    def test_only_compares_within_role(self) -> None:
        tasks = [
            _make_task(task_id="T-1", title="Add auth module", role="backend"),
            _make_task(task_id="T-2", title="Add auth module", role="frontend"),
        ]
        dupes = detect_duplicates(tasks)
        # Same title but different roles -> not compared
        assert dupes == []

    def test_ignores_non_open_tasks(self) -> None:
        tasks = [
            _make_task(task_id="T-1", title="Add auth module", status=TaskStatus.OPEN),
            _make_task(task_id="T-2", title="Add auth module", status=TaskStatus.DONE),
        ]
        dupes = detect_duplicates(tasks)
        assert dupes == []

    def test_sorted_by_similarity_descending(self) -> None:
        tasks = [
            _make_task(task_id="T-1", title="Add auth module", description="Build authentication"),
            _make_task(task_id="T-2", title="Add auth module", description="Build authentication system"),
            _make_task(task_id="T-3", title="Add authentication", description="Authentication module"),
        ]
        dupes = detect_duplicates(tasks, threshold=0.3)
        if len(dupes) > 1:
            assert dupes[0][2] >= dupes[1][2]


# --- merge_duplicate_tasks ---


class TestMergeDuplicateTasks:
    """Tests for merge_duplicate_tasks()."""

    def test_keeps_higher_priority(self) -> None:
        t1 = _make_task(task_id="T-1", title="Task A", priority=1)
        t2 = _make_task(task_id="T-2", title="Task B", priority=3)
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert merged.priority == 1

    def test_combines_descriptions(self) -> None:
        t1 = _make_task(task_id="T-1", description="First description")
        t2 = _make_task(task_id="T-2", description="Second description")
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert "First description" in merged.description
        assert "Second description" in merged.description

    def test_combines_completion_signals(self) -> None:
        t1 = _make_task(task_id="T-1", completion_signals=["tests pass"])
        t2 = _make_task(task_id="T-2", completion_signals=["lint clean", "tests pass"])
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert "tests pass" in merged.completion_signals
        assert "lint clean" in merged.completion_signals

    def test_combines_owned_files(self) -> None:
        t1 = _make_task(task_id="T-1", owned_files=["a.py", "b.py"])
        t2 = _make_task(task_id="T-2", owned_files=["b.py", "c.py"])
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert set(merged.owned_files) == {"a.py", "b.py", "c.py"}

    def test_combines_depends_on(self) -> None:
        t1 = _make_task(task_id="T-1", depends_on=["T-X"])
        t2 = _make_task(task_id="T-2", depends_on=["T-Y"])
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert set(merged.depends_on) == {"T-X", "T-Y"}

    def test_primary_id_preserved(self) -> None:
        t1 = _make_task(task_id="T-1", priority=1)
        t2 = _make_task(task_id="T-2", priority=2)
        merged = merge_duplicate_tasks(t1, t2, 0.8)
        assert merged.id == "T-1"
