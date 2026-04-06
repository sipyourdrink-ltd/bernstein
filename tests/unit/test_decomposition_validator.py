"""Tests for task decomposition validation (TASK-009)."""

from __future__ import annotations

from bernstein.core.decomposition_validator import (
    _extract_keywords,
    validate_decomposition,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus


def _t(
    id: str,
    title: str = "Task",
    description: str = "desc",
    owned_files: list[str] | None = None,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        owned_files=owned_files or [],
        estimated_minutes=estimated_minutes,
    )


class TestExtractKeywords:
    def test_basic_extraction(self) -> None:
        words = _extract_keywords("Implement the authentication module")
        assert "authentication" in words
        assert "module" in words

    def test_stop_words_removed(self) -> None:
        words = _extract_keywords("the a is are and or")
        assert len(words) == 0

    def test_short_words_removed(self) -> None:
        words = _extract_keywords("go be do it")
        assert len(words) == 0

    def test_case_insensitive(self) -> None:
        words = _extract_keywords("Authentication MODULE")
        assert "authentication" in words
        assert "module" in words


class TestValidateDecomposition:
    def test_valid_decomposition(self) -> None:
        parent = _t(
            "p1",
            title="Build authentication system",
            description="Implement login and signup flows",
            owned_files=["src/auth.py", "src/login.py"],
            estimated_minutes=60,
        )
        subtasks = [
            _t(
                "s1",
                title="Implement login flow",
                description="Build login endpoint with authentication",
                owned_files=["src/auth.py"],
                estimated_minutes=30,
            ),
            _t(
                "s2",
                title="Implement signup flow",
                description="Build signup endpoint with login redirect",
                owned_files=["src/login.py"],
                estimated_minutes=30,
            ),
        ]
        report = validate_decomposition(parent, subtasks)
        assert report.is_valid
        assert report.parent_id == "p1"
        assert set(report.subtask_ids) == {"s1", "s2"}

    def test_missing_file_coverage(self) -> None:
        parent = _t(
            "p1",
            owned_files=["src/a.py", "src/b.py", "src/c.py"],
            estimated_minutes=60,
        )
        subtasks = [_t("s1", owned_files=["src/a.py"], estimated_minutes=60)]
        report = validate_decomposition(parent, subtasks, min_file_coverage=0.8)
        has_file_issue = any(i.category == "file_coverage" for i in report.issues)
        assert has_file_issue
        assert not report.is_valid  # Below 80% threshold

    def test_partial_file_coverage_warning(self) -> None:
        parent = _t(
            "p1",
            owned_files=["src/a.py", "src/b.py"],
            estimated_minutes=60,
        )
        subtasks = [_t("s1", owned_files=["src/a.py"], estimated_minutes=60)]
        report = validate_decomposition(parent, subtasks, min_file_coverage=0.4)
        # 50% coverage is above 40% min, but there are uncovered files
        file_issues = [i for i in report.issues if i.category == "file_coverage"]
        assert any(i.severity == "warning" for i in file_issues)

    def test_under_scoped(self) -> None:
        parent = _t("p1", estimated_minutes=120)
        subtasks = [_t("s1", estimated_minutes=10)]
        report = validate_decomposition(parent, subtasks, min_scope_ratio=0.5)
        scope_issues = [i for i in report.issues if i.category == "scope_ratio"]
        assert len(scope_issues) >= 1

    def test_over_scoped(self) -> None:
        parent = _t("p1", estimated_minutes=30)
        subtasks = [_t("s1", estimated_minutes=200)]
        report = validate_decomposition(parent, subtasks, max_scope_ratio=3.0)
        scope_issues = [i for i in report.issues if i.category == "scope_ratio"]
        assert len(scope_issues) >= 1

    def test_empty_subtasks_error(self) -> None:
        parent = _t("p1")
        report = validate_decomposition(parent, [])
        assert not report.is_valid
        empty_issues = [i for i in report.issues if i.category == "empty_decomposition"]
        assert len(empty_issues) == 1

    def test_no_owned_files_full_coverage(self) -> None:
        parent = _t("p1")  # No owned files
        subtasks = [_t("s1")]
        report = validate_decomposition(parent, subtasks)
        file_errors = [i for i in report.issues if i.category == "file_coverage" and i.severity == "error"]
        assert len(file_errors) == 0

    def test_keyword_coverage(self) -> None:
        parent = _t(
            "p1",
            title="Implement authentication with OAuth2 and JWT tokens",
            description="Build a complete authentication system using OAuth2 provider",
            estimated_minutes=60,
        )
        subtasks = [
            _t(
                "s1",
                title="Something completely different about database",
                description="Migrate the database schema",
                estimated_minutes=60,
            ),
        ]
        report = validate_decomposition(parent, subtasks, min_keyword_coverage=0.5)
        kw_issues = [i for i in report.issues if i.category == "keyword_coverage"]
        assert len(kw_issues) >= 1
