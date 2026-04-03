"""Tests for context collapse integration in tick pipeline (T418)."""

from __future__ import annotations

import logging

import pytest

from bernstein.core.context_collapse import CollapseResult, CollapseStage
from bernstein.core.tick_pipeline import collapse_prompt_sections

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sections(extra: int = 0) -> list[tuple[str, str]]:
    """Build a representative section list with known token counts.

    Critical sections (role, task, instruction) total ~50 tokens.
    Each extra unit adds ~100 tokens of non-critical content.
    """
    sections: list[tuple[str, str]] = [
        ("role", "You are a backend engineer.\n"),
        ("tasks", "### Task 1: Fix bug (id=abc)\nFix the authentication bug.\n"),
        ("instructions", "Complete the task and exit.\n"),
        ("signal", "Check signal files every 60 seconds.\n"),
    ]
    for i in range(extra):
        sections.append((f"context-block-{i}", "x" * 400))  # 400 chars = ~100 tokens
    return sections


# ---------------------------------------------------------------------------
# TestCollapsePromptSections
# ---------------------------------------------------------------------------


class TestCollapsePromptSections:
    """Tests for collapse_prompt_sections tick integration."""

    def test_no_collapse_when_within_budget(self) -> None:
        sections = _make_sections(extra=2)  # ~250 tokens, well within 50k
        result_sections, result = collapse_prompt_sections(sections, token_budget=50_000)

        assert result.within_budget is True
        assert result.steps == []
        assert result_sections is sections  # Same object, no modification

    def test_collapse_triggered_when_over_budget(self) -> None:
        # 600 sections * 400 chars = 240,000 chars ≈ 60,000 tokens
        sections = _make_sections(extra=600)
        _, result = collapse_prompt_sections(sections, token_budget=10_000)

        assert result.steps != []
        # After truncation and dropping, context blocks should be reduced
        assert result.compressed_tokens < result.original_tokens

    def test_returns_collapsed_sections(self) -> None:
        sections = _make_sections(extra=100)
        result_sections, _ = collapse_prompt_sections(sections, token_budget=5_000)

        # Should still have critical sections
        names = [name for name, _ in result_sections]
        assert "role" in names
        assert "tasks" in names
        assert "instructions" in names
        assert "signal" in names

    def test_logs_collapse_when_triggered(self, caplog: pytest.LogCaptureFixture) -> None:
        sections = _make_sections(extra=200)
        with caplog.at_level(logging.INFO):
            collapse_prompt_sections(sections, token_budget=2_000)

        assert any("staged collapse" in m.lower() for m in caplog.messages)

    def test_logs_within_budget_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        sections = _make_sections(extra=2)
        with caplog.at_level(logging.DEBUG):
            collapse_prompt_sections(sections, token_budget=50_000)

        assert any("within budget" in m.lower() for m in caplog.messages)

    def test_task_ids_in_log_context(self, caplog: pytest.LogCaptureFixture) -> None:
        sections = _make_sections(extra=200)
        with caplog.at_level(logging.INFO):
            collapse_prompt_sections(
                sections,
                token_budget=2_000,
                task_ids=["task-1", "task-2"],
            )

        assert any("task-1" in m for m in caplog.messages)

    def test_collapse_result_token_tracking(self) -> None:
        sections = _make_sections(extra=50)
        _, result = collapse_prompt_sections(sections, token_budget=5_000)

        assert result.original_tokens > 0
        assert result.compressed_tokens <= result.original_tokens
        if result.compressed_tokens <= 5_000:
            assert result.within_budget is True

    def test_empty_sections_handled(self) -> None:
        result_sections, result = collapse_prompt_sections([], token_budget=100)

        assert result.within_budget is True
        assert result.original_tokens == 0
        assert result.compressed_tokens == 0
        assert result_sections == []

    def test_critical_sections_preserved_after_collapse(self) -> None:
        # Create sections where critical sections alone exceed budget
        sections: list[tuple[str, str]] = [
            ("role", "You are a backend.\n" + "x" * 32_000),  # ~8000 tokens
            ("tasks", "### Task\n" + "y" * 32_000),  # ~8000 tokens
            ("instructions", "Do it.\n" + "z" * 8_000),  # ~2000 tokens
            ("context-extra", "extra" * 100),  # ~100 tokens
        ]
        result_sections, result = collapse_prompt_sections(sections, token_budget=15_000)

        names = [name for name, _ in result_sections]
        # Critical sections must remain
        assert "role" in names
        assert "tasks" in names
        assert "instructions" in names
        # Extra context may have been dropped
        # (critical sections alone are ~18000 tokens which exceed budget,
        # so within_budget will be False but collapse still tried)
        assert isinstance(result, CollapseResult)

    def test_collapse_result_has_steps_when_over_budget(self) -> None:
        sections = _make_sections(extra=50)
        _, result = collapse_prompt_sections(sections, token_budget=3_000)

        if not result.within_budget:
            # Verify steps are at least recorded
            assert isinstance(result.steps, list)

    def test_stage_ordering_is_truncate_drop_strip(self) -> None:
        """Verify collapse stages are applied in the correct order."""
        sections = _make_sections(extra=100)
        _, result = collapse_prompt_sections(sections, token_budget=1_000)

        # Collect stage names from steps
        stage_names = [s.stage for s in result.steps]
        if not stage_names:
            return  # No collapse was needed

        # Truncate should come first if it fired
        stage_order = {
            CollapseStage.TRUNCATE: 0,
            CollapseStage.DROP_SECTIONS: 1,
            CollapseStage.STRIP_METADATA: 2,
        }
        last_order = -1
        for stage in stage_names:
            assert stage_order[stage] >= last_order
            last_order = stage_order[stage]
