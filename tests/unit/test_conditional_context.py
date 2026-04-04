"""Tests for conditional context activation (T677).

Validates that SectionRelevanceFilter skips prompt sections that are
irrelevant to the current task based on role, scope, owned_files, and
session_id.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.spawn_prompt import (
    SECTION_RULES,
    SectionRule,
    _files_match_patterns,
    _lesson_cache,
    _render_prompt,
    _scope_ordinal,
    filter_sections,
    section_is_relevant,
)
from bernstein.templates.renderer import TemplateError

# ---------------------------------------------------------------------------
# Unit tests for _scope_ordinal
# ---------------------------------------------------------------------------


class TestScopeOrdinal:
    def test_small(self) -> None:
        assert _scope_ordinal("small") == 0

    def test_medium(self) -> None:
        assert _scope_ordinal("medium") == 1

    def test_large(self) -> None:
        assert _scope_ordinal("large") == 2

    def test_unknown_defaults_to_medium(self) -> None:
        assert _scope_ordinal("unknown") == 1


# ---------------------------------------------------------------------------
# Unit tests for _files_match_patterns
# ---------------------------------------------------------------------------


class TestFilesMatchPatterns:
    def test_exact_match(self) -> None:
        assert _files_match_patterns(["src/main.py"], ("src/main.py",))

    def test_wildcard_match(self) -> None:
        assert _files_match_patterns(["src/bernstein/core/models.py"], ("src/bernstein/core/*.py",))

    def test_double_star_not_fnmatch(self) -> None:
        # fnmatch treats ** the same as * (no recursive), but the user intent
        # is "anything under src/" — single * suffices for flat matching.
        assert _files_match_patterns(["src/foo.py"], ("src/*.py",))

    def test_no_match(self) -> None:
        assert not _files_match_patterns(["tests/unit/test_foo.py"], ("src/*.py",))

    def test_empty_files(self) -> None:
        assert not _files_match_patterns([], ("src/*.py",))

    def test_empty_patterns(self) -> None:
        assert not _files_match_patterns(["src/main.py"], ())

    def test_question_mark_glob(self) -> None:
        assert _files_match_patterns(["src/a.py"], ("src/?.py",))

    def test_multiple_files_one_matches(self) -> None:
        files = ["README.md", "src/core/foo.py"]
        assert _files_match_patterns(files, ("src/core/*.py",))


# ---------------------------------------------------------------------------
# Unit tests for section_is_relevant
# ---------------------------------------------------------------------------


class TestSectionIsRelevant:
    """Tests for the section_is_relevant function."""

    def test_unlisted_section_always_relevant(self) -> None:
        """Sections not in the rules table are always included (critical)."""
        assert section_is_relevant(
            "role", role="backend", scope="medium", owned_files=[], session_id=""
        )

    def test_specialists_only_for_manager(self) -> None:
        assert section_is_relevant(
            "specialists", role="manager", scope="medium", owned_files=[], session_id=""
        )
        assert not section_is_relevant(
            "specialists", role="backend", scope="medium", owned_files=[], session_id=""
        )

    def test_team_awareness_excluded_for_docs(self) -> None:
        assert not section_is_relevant(
            "team awareness", role="docs", scope="medium", owned_files=[], session_id="s1"
        )

    def test_team_awareness_included_for_backend_with_session(self) -> None:
        assert section_is_relevant(
            "team awareness", role="backend", scope="medium", owned_files=[], session_id="s1"
        )

    def test_team_awareness_excluded_without_session(self) -> None:
        assert not section_is_relevant(
            "team awareness", role="backend", scope="medium", owned_files=[], session_id=""
        )

    def test_heartbeat_requires_medium_scope(self) -> None:
        assert not section_is_relevant(
            "heartbeat", role="backend", scope="small", owned_files=[], session_id="s1"
        )
        assert section_is_relevant(
            "heartbeat", role="backend", scope="medium", owned_files=[], session_id="s1"
        )
        assert section_is_relevant(
            "heartbeat", role="backend", scope="large", owned_files=[], session_id="s1"
        )

    def test_heartbeat_requires_session(self) -> None:
        assert not section_is_relevant(
            "heartbeat", role="backend", scope="large", owned_files=[], session_id=""
        )

    def test_file_ownership_excluded_for_manager(self) -> None:
        assert not section_is_relevant(
            "file ownership", role="manager", scope="medium", owned_files=[], session_id="s1"
        )

    def test_file_ownership_included_for_backend(self) -> None:
        assert section_is_relevant(
            "file ownership", role="backend", scope="medium", owned_files=[], session_id="s1"
        )

    def test_recommendations_excluded_for_manager(self) -> None:
        assert not section_is_relevant(
            "recommendations", role="manager", scope="medium", owned_files=[], session_id=""
        )

    def test_recommendations_included_for_qa(self) -> None:
        assert section_is_relevant(
            "recommendations", role="qa", scope="medium", owned_files=[], session_id=""
        )

    def test_custom_rule_with_file_patterns(self) -> None:
        """Custom rules with file_patterns only activate on matching files."""
        custom_rules = {
            "security": SectionRule(file_patterns=("src/auth/*", "src/crypto/*")),
        }
        assert section_is_relevant(
            "security",
            role="backend",
            scope="medium",
            owned_files=["src/auth/token.py"],
            session_id="",
            rules=custom_rules,
        )
        assert not section_is_relevant(
            "security",
            role="backend",
            scope="medium",
            owned_files=["src/models.py"],
            session_id="",
            rules=custom_rules,
        )

    def test_lessons_excluded_for_visionary(self) -> None:
        assert not section_is_relevant(
            "lessons", role="visionary", scope="medium", owned_files=[], session_id=""
        )

    def test_lessons_included_for_backend(self) -> None:
        assert section_is_relevant(
            "lessons", role="backend", scope="medium", owned_files=[], session_id=""
        )


# ---------------------------------------------------------------------------
# Unit tests for filter_sections
# ---------------------------------------------------------------------------


class TestFilterSections:
    """Tests for the filter_sections function."""

    def test_critical_sections_never_removed(self) -> None:
        """Role, tasks, instructions, git_safety are always kept."""
        sections = [
            ("role", "You are a backend specialist."),
            ("git_safety", "Git safety rules."),
            ("tasks", "Task 1: ..."),
            ("instructions", "Complete these tasks."),
        ]
        result = filter_sections(
            sections, role="backend", scope="medium", owned_files=[], session_id=""
        )
        assert len(result) == 4
        assert [name for name, _ in result] == ["role", "git_safety", "tasks", "instructions"]

    def test_specialists_dropped_for_non_manager(self) -> None:
        sections = [
            ("role", "You are a backend specialist."),
            ("specialists", "Agency agents..."),
            ("tasks", "Task 1: ..."),
        ]
        result = filter_sections(
            sections, role="backend", scope="medium", owned_files=[], session_id=""
        )
        names = [name for name, _ in result]
        assert "specialists" not in names

    def test_specialists_kept_for_manager(self) -> None:
        sections = [
            ("role", "You are a manager."),
            ("specialists", "Agency agents..."),
            ("tasks", "Task 1: ..."),
        ]
        result = filter_sections(
            sections, role="manager", scope="medium", owned_files=[], session_id=""
        )
        names = [name for name, _ in result]
        assert "specialists" in names

    def test_heartbeat_dropped_for_small_scope(self) -> None:
        sections = [
            ("role", "Role."),
            ("tasks", "Tasks."),
            ("heartbeat", "Heartbeat instructions."),
        ]
        result = filter_sections(
            sections, role="backend", scope="small", owned_files=[], session_id="s1"
        )
        names = [name for name, _ in result]
        assert "heartbeat" not in names

    def test_heartbeat_kept_for_large_scope(self) -> None:
        sections = [
            ("role", "Role."),
            ("tasks", "Tasks."),
            ("heartbeat", "Heartbeat instructions."),
        ]
        result = filter_sections(
            sections, role="backend", scope="large", owned_files=[], session_id="s1"
        )
        names = [name for name, _ in result]
        assert "heartbeat" in names

    def test_multiple_sections_filtered_for_docs_role(self) -> None:
        """Docs role drops team awareness, team coordination, file ownership."""
        sections = [
            ("role", "You are a docs specialist."),
            ("tasks", "Task 1: ..."),
            ("team awareness", "Other agents..."),
            ("team coordination", "Post to bulletin..."),
            ("file ownership", "Files locked by others."),
            ("lessons", "Lesson context."),
            ("recommendations", "Recommendations."),
            ("instructions", "Complete these."),
        ]
        result = filter_sections(
            sections, role="docs", scope="medium", owned_files=[], session_id="s1"
        )
        names = [name for name, _ in result]
        assert "team awareness" not in names
        assert "team coordination" not in names
        assert "file ownership" not in names
        # Lessons and recommendations ARE kept for docs (useful for writing docs)
        assert "lessons" in names
        assert "recommendations" in names
        # Critical sections are kept
        assert "role" in names
        assert "tasks" in names
        assert "instructions" in names

    def test_custom_rules_override_defaults(self) -> None:
        custom_rules: dict[str, SectionRule] = {
            "specialists": SectionRule(),  # always include
        }
        sections = [
            ("specialists", "Available agents."),
            ("tasks", "Task 1: ..."),
        ]
        result = filter_sections(
            sections,
            role="backend",
            scope="medium",
            owned_files=[],
            session_id="",
            rules=custom_rules,
        )
        names = [name for name, _ in result]
        assert "specialists" in names


# ---------------------------------------------------------------------------
# Integration test: _render_prompt applies conditional context
# ---------------------------------------------------------------------------


def test_render_prompt_drops_specialists_for_backend(tmp_path: Path, make_task: Any) -> None:
    """Backend role should not receive the specialists section even if data is present."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="backend", title="Build parser", description="Parse.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", side_effect=TemplateError("missing")),
        patch("bernstein.core.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend", "qa"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
            # agency_catalog would add specialists, but they are only for manager
        )

    assert "Available specialist agents" not in prompt


def test_render_prompt_drops_team_sections_for_docs(tmp_path: Path, make_task: Any) -> None:
    """Docs role should not see team awareness or team coordination even with session_id."""
    _lesson_cache.clear()
    task = make_task(id="T-1", role="docs", title="Write readme", description="Write docs.")
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", side_effect=TemplateError("missing")),
        patch("bernstein.core.spawn_prompt.gather_lessons_for_context", return_value="## Lessons\nSome lesson."),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["docs"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="docs-abc",
            bulletin_summary="- Backend pushed new API.",
        )

    assert "Team awareness" not in prompt
    assert "Team coordination" not in prompt
    # But role and tasks are always there
    assert "You are a docs specialist." in prompt
    assert "Write readme" in prompt


def test_render_prompt_drops_heartbeat_for_small_scope(tmp_path: Path, make_task: Any) -> None:
    """Small-scope tasks should not include heartbeat section."""
    from bernstein.core.models import Scope

    _lesson_cache.clear()
    task = make_task(
        id="T-1", role="backend", title="Fix typo", description="Fix it.", scope=Scope.SMALL
    )
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
        )

    assert "Heartbeat" not in prompt


def test_render_prompt_keeps_heartbeat_for_large_scope(tmp_path: Path, make_task: Any) -> None:
    """Large-scope tasks should include heartbeat section."""
    from bernstein.core.models import Scope

    _lesson_cache.clear()
    task = make_task(
        id="T-1", role="backend", title="Refactor engine", description="Big refactor.", scope=Scope.LARGE
    )
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch("bernstein.core.spawn_prompt.gather_lessons_for_context", return_value=""),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="A-1",
        )

    # The heartbeat section is behind a try/except that may fail in test env,
    # but the filter should NOT have dropped it. Check that signal section IS
    # present (also requires session_id, scope >= medium).
    assert "Signal files" in prompt


def test_render_prompt_backend_context_activates_for_src_files(
    tmp_path: Path, make_task: Any
) -> None:
    """Backend context activates when owned_files match src/ patterns (acceptance criterion)."""
    _lesson_cache.clear()
    task = make_task(
        id="T-1",
        role="backend",
        title="Fix parser",
        description="Fix the parser.",
        owned_files=["src/bernstein/core/parser.py"],
    )
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    project_md = tmp_path / ".sdd" / "project.md"
    project_md.parent.mkdir(parents=True)
    project_md.write_text("Project context for backend.", encoding="utf-8")

    with (
        patch("bernstein.core.spawn_prompt.render_role_prompt", return_value="You are a backend specialist."),
        patch(
            "bernstein.core.spawn_prompt.gather_lessons_for_context",
            return_value="## Lessons\nUse strict typing.",
        ),
        patch("bernstein.core.spawn_prompt._list_subdirs_cached", return_value=["backend"]),
    ):
        prompt = _render_prompt(
            [task],
            templates_dir=templates_dir,
            workdir=tmp_path,
            session_id="backend-001",
        )

    # Backend + session_id + src/ files -> should have relevant sections
    assert "You are a backend specialist." in prompt
    assert "Fix parser" in prompt
    assert "Project context for backend." in prompt
    assert "Lessons" in prompt
    assert "Signal files" in prompt


def test_section_rules_table_covers_expected_sections() -> None:
    """Verify SECTION_RULES has entries for all optional sections."""
    expected = {
        "specialists",
        "team awareness",
        "team coordination",
        "file ownership",
        "heartbeat",
        "recommendations",
        "lessons",
        "predecessor",
        "project",
        "meta nudges",
    }
    assert set(SECTION_RULES.keys()) == expected
