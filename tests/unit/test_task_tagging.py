"""Tests for task tagging and filtering (TASK-013)."""

from __future__ import annotations

import pytest

from bernstein.core.models import Complexity, Scope, Task, TaskStatus
from bernstein.core.task_tagging import InvalidTagError, TaskTagger, validate_tag


def _t(id: str) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
    )


class TestValidateTag:
    def test_valid_simple(self) -> None:
        assert validate_tag("backend") == "backend"

    def test_normalised_lowercase(self) -> None:
        assert validate_tag("Backend") == "backend"

    def test_valid_with_hyphens(self) -> None:
        assert validate_tag("api-v2") == "api-v2"

    def test_valid_with_underscores(self) -> None:
        assert validate_tag("hot_fix") == "hot_fix"

    def test_strips_whitespace(self) -> None:
        assert validate_tag("  backend  ") == "backend"

    def test_empty_raises(self) -> None:
        with pytest.raises(InvalidTagError):
            validate_tag("")

    def test_spaces_only_raises(self) -> None:
        with pytest.raises(InvalidTagError):
            validate_tag("   ")

    def test_special_chars_raises(self) -> None:
        with pytest.raises(InvalidTagError):
            validate_tag("tag!@#")

    def test_starts_with_hyphen_raises(self) -> None:
        with pytest.raises(InvalidTagError):
            validate_tag("-invalid")


class TestTaskTagger:
    def test_add_and_get_tags(self) -> None:
        tagger = TaskTagger()
        result = tagger.add_tags("t1", ["backend", "api"])
        assert result == {"backend", "api"}
        assert tagger.get_tags("t1") == {"backend", "api"}

    def test_add_tags_idempotent(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend"])
        tagger.add_tags("t1", ["backend"])
        assert tagger.get_tags("t1") == {"backend"}

    def test_remove_tags(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend", "api", "v2"])
        remaining = tagger.remove_tags("t1", ["api"])
        assert remaining == {"backend", "v2"}

    def test_remove_from_unknown_task(self) -> None:
        tagger = TaskTagger()
        result = tagger.remove_tags("nonexistent", ["tag"])
        assert result == set()

    def test_has_tag(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend"])
        assert tagger.has_tag("t1", "backend")
        assert not tagger.has_tag("t1", "frontend")
        assert not tagger.has_tag("t2", "backend")

    def test_filter_by_tag(self) -> None:
        tagger = TaskTagger()
        tasks = [_t("t1"), _t("t2"), _t("t3")]
        tagger.add_tags("t1", ["backend"])
        tagger.add_tags("t2", ["backend", "api"])
        tagger.add_tags("t3", ["frontend"])

        result = tagger.filter_by_tag(tasks, "backend")
        assert [t.id for t in result] == ["t1", "t2"]

    def test_filter_by_any_tag(self) -> None:
        tagger = TaskTagger()
        tasks = [_t("t1"), _t("t2"), _t("t3")]
        tagger.add_tags("t1", ["backend"])
        tagger.add_tags("t2", ["frontend"])
        tagger.add_tags("t3", ["security"])

        result = tagger.filter_by_any_tag(tasks, ["backend", "security"])
        assert {t.id for t in result} == {"t1", "t3"}

    def test_filter_by_all_tags(self) -> None:
        tagger = TaskTagger()
        tasks = [_t("t1"), _t("t2"), _t("t3")]
        tagger.add_tags("t1", ["backend", "api"])
        tagger.add_tags("t2", ["backend"])
        tagger.add_tags("t3", ["api"])

        result = tagger.filter_by_all_tags(tasks, ["backend", "api"])
        assert [t.id for t in result] == ["t1"]

    def test_all_tags(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend", "api"])
        tagger.add_tags("t2", ["frontend"])
        assert tagger.all_tags() == {"backend", "api", "frontend"}

    def test_tasks_with_tag(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend"])
        tagger.add_tags("t2", ["backend"])
        tagger.add_tags("t3", ["frontend"])
        assert set(tagger.tasks_with_tag("backend")) == {"t1", "t2"}

    def test_tag_counts(self) -> None:
        tagger = TaskTagger()
        tagger.add_tags("t1", ["backend", "api"])
        tagger.add_tags("t2", ["backend"])
        counts = tagger.tag_counts()
        assert counts["backend"] == 2
        assert counts["api"] == 1

    def test_invalid_tag_in_add(self) -> None:
        tagger = TaskTagger()
        with pytest.raises(InvalidTagError):
            tagger.add_tags("t1", ["valid", "!invalid"])

    def test_get_tags_unknown_task(self) -> None:
        tagger = TaskTagger()
        assert tagger.get_tags("nonexistent") == set()

    def test_filter_empty_tasks(self) -> None:
        tagger = TaskTagger()
        assert tagger.filter_by_tag([], "backend") == []

    def test_case_insensitive_filtering(self) -> None:
        tagger = TaskTagger()
        tasks = [_t("t1")]
        tagger.add_tags("t1", ["Backend"])
        result = tagger.filter_by_tag(tasks, "BACKEND")
        assert len(result) == 1
