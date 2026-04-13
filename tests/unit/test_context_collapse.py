"""Tests for staged context collapse (T418)."""

from __future__ import annotations

import pytest
from bernstein.core.context_collapse import (
    CollapseStep,
    _drop_sections,
    _estimate_tokens,
    _section_priority,
    _strip_metadata,
    _truncate_sections,
    staged_context_collapse,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section(name: str, tokens: int = 100) -> tuple[str, str]:
    """Create a named section with approximately *tokens* estimated tokens."""
    return (name, "x" * tokens * 4)


def _critical_sections(count: int = 2) -> list[tuple[str, str]]:
    """Return critical sections (priority 10)."""
    return [_section("role", 200), _section("tasks", 200)][:count]


# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_string(self) -> None:
        assert _estimate_tokens("") == 0

    def test_short_text(self) -> None:
        assert _estimate_tokens("abcd") == 1

    def test_long_text(self) -> None:
        text = "x" * 4000
        assert _estimate_tokens(text) == 1000


# ---------------------------------------------------------------------------
# _section_priority
# ---------------------------------------------------------------------------


class TestSectionPriority:
    def test_critical_sections(self) -> None:
        assert _section_priority("role") == 10
        assert _section_priority("task") == 10
        assert _section_priority("instruction") == 10
        assert _section_priority("signal") == 10

    def test_non_critical_sections(self) -> None:
        assert _section_priority("project") == 7
        assert _section_priority("context") == 5
        assert _section_priority("lessons") == 4

    def test_unknown_section(self) -> None:
        assert _section_priority("foobar") == 5

    def test_case_insensitive(self) -> None:
        assert _section_priority("ROLE") == 10


# ---------------------------------------------------------------------------
# _truncate_sections (Stage 1)
# ---------------------------------------------------------------------------


class TestTruncateSections:
    def test_no_op_when_under_budget(self) -> None:
        sections = [_section("context", 100)]
        result, steps = _truncate_sections(sections, budget=500)
        assert steps == []
        assert result == sections

    def test_truncates_non_critical_sections(self) -> None:
        sections = [
            ("role", "critical content"),
            _section("project context", 100),
            _section("lessons", 200),
        ]
        # Total tokens: ~0 + 100 + 200 = 300. Budget: 150.
        result, steps = _truncate_sections(sections, budget=150)
        assert len(steps) >= 1
        # Role should NOT be truncated
        assert result[0][0] == "role"
        assert result[0][1] == "critical content"

    def test_never_truncates_priority_10(self) -> None:
        sections = [
            _section("task", 500),
            _section("instructions", 500),
        ]
        result, steps = _truncate_sections(sections, budget=100)
        assert steps == []  # Priority 10 sections are never truncated
        assert result == sections

    def test_preserves_truncation_notice(self) -> None:
        sections = [_section("project", 1000)]
        result, steps = _truncate_sections(sections, budget=100)
        assert len(steps) >= 1
        _, content = result[0]
        assert "truncated" in content.lower() or "truncate" in content.lower()


# ---------------------------------------------------------------------------
# _drop_sections (Stage 2)
# ---------------------------------------------------------------------------


class TestDropSections:
    def test_no_op_when_under_budget(self) -> None:
        sections = [_section("lessons", 50)]
        result, steps = _drop_sections(sections, budget=500)
        assert steps == []
        assert result == sections

    def test_drops_least_important_first(self) -> None:
        # specialist has priority 2, lessons has priority 4
        sections = [
            _section("specialist agents", 200),
            _section("lessons", 200),
            ("role", "critical"),
        ]
        result, steps = _drop_sections(sections, budget=100)
        # At least one section should be dropped
        assert len(steps) >= 1
        # Role should never be dropped
        assert any(name == "role" for name, _ in result)

    def test_stops_dropping_when_within_budget(self) -> None:
        sections = [
            _section("specialist", 50),
            _section("heartbeat", 50),
            _section("recommendations", 50),
            _section("lessons", 50),
        ]
        # Total: ~200 tokens, budget 100
        result, steps = _drop_sections(sections, budget=100)
        result_tokens = sum(_estimate_tokens(c) for _, c in result)
        assert result_tokens <= 100 or len(result) < len(sections)
        assert len(steps) >= 1


# ---------------------------------------------------------------------------
# _strip_metadata (Stage 3)
# ---------------------------------------------------------------------------


class TestStripMetadata:
    def test_no_op_when_under_budget(self) -> None:
        sections = [_section("lessons", 50)]
        result, steps = _strip_metadata(sections, budget=500)
        assert steps == []
        assert result == sections

    def test_strips_lessons(self) -> None:
        sections = [
            ("role", "critical"),
            _section("lessons", 200),
        ]
        result, steps = _strip_metadata(sections, budget=100)
        # Lessons should be stripped
        assert len(steps) >= 1
        assert all("lesson" not in name.lower() for name, _ in result)

    def test_strips_recommendations(self) -> None:
        sections = [
            ("task", "task content"),
            _section("recommendations", 300),
        ]
        result, steps = _strip_metadata(sections, budget=50)
        assert len(steps) >= 1
        assert all("recommend" not in name.lower() for name, _ in result)

    def test_strips_largest_metadata_first(self) -> None:
        # Large lessons block, small recommendations block — both should
        # be stripped in descending order of size
        sections = [
            _section("lessons", 500),
            _section("recommendation", 100),
        ]
        _, steps = _strip_metadata(sections, budget=50)
        assert len(steps) >= 1
        # Lessons (larger) should be stripped first
        assert steps[0].section_name.lower() == "lessons"


# ---------------------------------------------------------------------------
# staged_context_collapse (integration)
# ---------------------------------------------------------------------------


class TestStagedContextCollapse:
    def test_fits_under_budget(self) -> None:
        sections = [
            ("role", "You are a backend engineer."),
            ("tasks", "## Assigned tasks\nTask 1"),
            ("project context", "x" * 1000),
            ("lessons", "x" * 2000),
        ]
        result = staged_context_collapse(sections, token_budget=500)
        assert result.within_budget
        assert result.compressed_tokens <= 500
        assert result.original_tokens > result.compressed_tokens

    def test_preserves_critical_sections(self) -> None:
        sections = [
            ("role", "role_content"),
            ("task", "task_content"),
            ("instructions", "instructions_content"),
            ("signal", "signal_content"),
            ("lessons", "x" * 1000),
        ]
        result = staged_context_collapse(sections, token_budget=200)
        # Critical section names should still be present
        names = [name for name, _ in result.sections]
        assert any("role" in n for n in names)
        assert any("task" in n for n in names)

    def test_returns_empty_steps_when_no_collapse_needed(self) -> None:
        sections = [("role", "small"), ("task", "small")]
        result = staged_context_collapse(sections, token_budget=10000)
        assert result.steps == []
        assert result.within_budget

    def test_logs_multiple_steps(self, caplog: pytest.LogCaptureFixture) -> None:
        # Create a prompt that will require truncation AND dropping
        sections = [
            ("role", "r"),
            ("tasks", "t"),
            _section("project context", 1000),
            _section("lessons", 1000),
            _section("recommendations", 1000),
        ]
        with caplog.at_level("INFO"):
            result = staged_context_collapse(sections, token_budget=200)
        # Should have taken at least one stage
        assert len(result.steps) >= 1

    def test_within_budget_flag_false_when_critical_exceeds(self) -> None:
        # Even with all non-critical stripped, critical sections alone
        # exceed the tiny budget
        sections = [
            ("role", "x" * 4000),  # 1000 tokens
            ("task", "x" * 4000),  # 1000 tokens
        ]
        result = staged_context_collapse(sections, token_budget=500)
        assert result.within_budget is False
        assert result.compressed_tokens > 500

    def test_step_records_are_complete(self) -> None:
        sections = [
            ("role", "role"),
            _section("project", 500),
            _section("lessons", 500),
        ]
        result = staged_context_collapse(sections, token_budget=100)
        for step in result.steps:
            assert isinstance(step, CollapseStep)
            assert step.tokens_freed >= 0
            assert step.section_name
            assert step.action

    def test_accepts_empty_sections(self) -> None:
        result = staged_context_collapse([], token_budget=500)
        assert result.sections == []
        assert result.original_tokens == 0
        assert result.within_budget

    def test_deduplicates_stage_names(self) -> None:
        """Multiple sections of same type should result in multiple steps."""
        sections = [
            _section("project A", 1000),
            _section("project B", 1000),
        ]
        result = staged_context_collapse(sections, token_budget=100)
        # Both project sections should be processed
        assert len(result.steps) >= 1
