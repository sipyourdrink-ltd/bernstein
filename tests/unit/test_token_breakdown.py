"""Tests for bernstein.core.tokens.token_breakdown."""

from __future__ import annotations

from bernstein.core.tokens.token_breakdown import (
    CategoryName,
    TokenBreakdown,
    TokenCategory,
    categorize_tokens,
    estimate_waste,
    get_optimization_recommendations,
)

# ---------------------------------------------------------------------------
# TokenCategory
# ---------------------------------------------------------------------------


class TestTokenCategory:
    """TokenCategory dataclass basics."""

    def test_frozen(self) -> None:
        cat = TokenCategory(category="system_prompt", tokens=500, percentage=25.0)
        assert cat.category == "system_prompt"
        assert cat.tokens == 500
        assert cat.percentage == 25.0

    def test_immutable(self) -> None:
        cat = TokenCategory(category="output", tokens=100, percentage=10.0)
        try:
            cat.tokens = 999  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass  # frozen dataclass — expected


# ---------------------------------------------------------------------------
# TokenBreakdown
# ---------------------------------------------------------------------------


class TestTokenBreakdown:
    """TokenBreakdown dataclass basics."""

    def test_defaults(self) -> None:
        bd = TokenBreakdown(agent_id="a1", task_id="t1", total_tokens=0)
        assert bd.categories == ()
        assert bd.waste_estimate == 0.0

    def test_frozen(self) -> None:
        bd = TokenBreakdown(agent_id="a1", task_id="t1", total_tokens=100)
        try:
            bd.total_tokens = 999  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# categorize_tokens
# ---------------------------------------------------------------------------


class TestCategorizeTokens:
    """Tests for the categorize_tokens function."""

    def test_empty_session(self) -> None:
        bd = categorize_tokens({"agent_id": "a1", "task_id": "t1"})
        assert bd.agent_id == "a1"
        assert bd.task_id == "t1"
        assert bd.total_tokens == 0
        assert len(bd.categories) == 5

    def test_string_content_estimated(self) -> None:
        bd = categorize_tokens(
            {
                "agent_id": "a1",
                "task_id": "t1",
                "system_prompt": "You are a helpful assistant." * 10,
                "task_description": "Fix the bug.",
            }
        )
        assert bd.total_tokens > 0
        cat_map = {c.category: c for c in bd.categories}
        assert cat_map["system_prompt"].tokens > 0
        assert cat_map["task_desc"].tokens > 0
        assert cat_map["context_files"].tokens == 0

    def test_int_content_used_directly(self) -> None:
        bd = categorize_tokens(
            {
                "agent_id": "a1",
                "task_id": "t1",
                "system_prompt": 500,
                "context_files": 2000,
                "task_description": 100,
                "output": 1000,
                "tool_results": 400,
            }
        )
        assert bd.total_tokens == 4000
        cat_map = {c.category: c for c in bd.categories}
        assert cat_map["system_prompt"].tokens == 500
        assert cat_map["context_files"].tokens == 2000
        assert cat_map["output"].tokens == 1000

    def test_percentages_sum_to_100(self) -> None:
        bd = categorize_tokens(
            {
                "agent_id": "a1",
                "task_id": "t1",
                "system_prompt": 100,
                "context_files": 200,
                "task_description": 50,
                "output": 100,
                "tool_results": 50,
            }
        )
        total_pct = sum(c.percentage for c in bd.categories)
        assert abs(total_pct - 100.0) < 0.1

    def test_categories_sorted_descending(self) -> None:
        bd = categorize_tokens(
            {
                "agent_id": "a1",
                "task_id": "t1",
                "system_prompt": 100,
                "context_files": 500,
                "output": 300,
            }
        )
        tokens = [c.tokens for c in bd.categories]
        assert tokens == sorted(tokens, reverse=True)

    def test_all_category_names_present(self) -> None:
        bd = categorize_tokens({"agent_id": "a1", "task_id": "t1", "system_prompt": 100})
        cat_names = {c.category for c in bd.categories}
        for name in CategoryName:
            assert name in cat_names

    def test_list_content_joined(self) -> None:
        bd = categorize_tokens(
            {
                "agent_id": "a1",
                "task_id": "t1",
                "context_files": ["file one content", "file two content"],
            }
        )
        cat_map = {c.category: c for c in bd.categories}
        assert cat_map["context_files"].tokens > 0

    def test_negative_int_clamped_to_zero(self) -> None:
        bd = categorize_tokens({"agent_id": "a1", "task_id": "t1", "system_prompt": -10})
        cat_map = {c.category: c for c in bd.categories}
        assert cat_map["system_prompt"].tokens == 0

    def test_missing_ids_default_to_empty(self) -> None:
        bd = categorize_tokens({})
        assert bd.agent_id == ""
        assert bd.task_id == ""


# ---------------------------------------------------------------------------
# estimate_waste
# ---------------------------------------------------------------------------


class TestEstimateWaste:
    """Tests for the estimate_waste function."""

    def test_zero_tokens_returns_zero(self) -> None:
        bd = TokenBreakdown(agent_id="a1", task_id="t1", total_tokens=0)
        assert estimate_waste(bd, files_used=[]) == 0.0

    def test_below_min_threshold_returns_zero(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=50,
            categories=(TokenCategory(category="system_prompt", tokens=50, percentage=100.0),),
        )
        assert estimate_waste(bd, files_used=[]) == 0.0

    def test_unused_files_penalty(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=400, percentage=40.0),
                TokenCategory(category="output", tokens=300, percentage=30.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=100, percentage=10.0),
                TokenCategory(category="tool_results", tokens=100, percentage=10.0),
            ),
        )
        waste = estimate_waste(bd, files_used=[])
        # context_files (400) counted as waste because files_used is empty.
        assert waste > 0.0

    def test_no_waste_when_files_used(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=300, percentage=30.0),
                TokenCategory(category="output", tokens=300, percentage=30.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="tool_results", tokens=250, percentage=25.0),
            ),
        )
        waste = estimate_waste(bd, files_used=["src/main.py"])
        # No category exceeds its bloat threshold, files are used,
        # and output >= 5%, so waste should be 0.
        assert waste == 0.0

    def test_bloated_category_penalty(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=700, percentage=70.0),
                TokenCategory(category="output", tokens=100, percentage=10.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="tool_results", tokens=50, percentage=5.0),
            ),
        )
        waste = estimate_waste(bd, files_used=["src/main.py"])
        # context_files at 70% exceeds the 50% bloat threshold.
        assert waste > 0.0

    def test_low_output_penalty(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=480, percentage=48.0),
                TokenCategory(category="system_prompt", tokens=140, percentage=14.0),
                TokenCategory(category="tool_results", tokens=350, percentage=35.0),
                TokenCategory(category="output", tokens=20, percentage=2.0),
                TokenCategory(category="task_desc", tokens=10, percentage=1.0),
            ),
        )
        waste = estimate_waste(bd, files_used=["src/main.py"])
        # Output is only 2%, below the 5% floor.
        assert waste > 0.0

    def test_waste_capped_at_100(self) -> None:
        # Extreme case: all tokens in context_files, no files used, no output.
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=950, percentage=95.0),
                TokenCategory(category="output", tokens=0, percentage=0.0),
                TokenCategory(category="system_prompt", tokens=50, percentage=5.0),
                TokenCategory(category="task_desc", tokens=0, percentage=0.0),
                TokenCategory(category="tool_results", tokens=0, percentage=0.0),
            ),
        )
        waste = estimate_waste(bd, files_used=[])
        assert waste <= 100.0


# ---------------------------------------------------------------------------
# get_optimization_recommendations
# ---------------------------------------------------------------------------


class TestGetOptimizationRecommendations:
    """Tests for the get_optimization_recommendations function."""

    def test_no_tips_when_balanced(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="output", tokens=350, percentage=35.0),
                TokenCategory(category="context_files", tokens=300, percentage=30.0),
                TokenCategory(category="tool_results", tokens=200, percentage=20.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert tips == []

    def test_system_prompt_tip(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="system_prompt", tokens=250, percentage=25.0),
                TokenCategory(category="output", tokens=250, percentage=25.0),
                TokenCategory(category="context_files", tokens=250, percentage=25.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="tool_results", tokens=200, percentage=20.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert any("System prompt" in t for t in tips)

    def test_context_files_tip(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="context_files", tokens=500, percentage=50.0),
                TokenCategory(category="output", tokens=200, percentage=20.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="tool_results", tokens=150, percentage=15.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert any("Context files" in t for t in tips)

    def test_tool_results_tip(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="tool_results", tokens=400, percentage=40.0),
                TokenCategory(category="output", tokens=300, percentage=30.0),
                TokenCategory(category="context_files", tokens=200, percentage=20.0),
                TokenCategory(category="system_prompt", tokens=50, percentage=5.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert any("Tool results" in t for t in tips)

    def test_below_min_tokens_no_tips(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=50,
            categories=(
                TokenCategory(category="system_prompt", tokens=50, percentage=100.0),
                TokenCategory(category="context_files", tokens=0, percentage=0.0),
                TokenCategory(category="task_desc", tokens=0, percentage=0.0),
                TokenCategory(category="output", tokens=0, percentage=0.0),
                TokenCategory(category="tool_results", tokens=0, percentage=0.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert tips == []

    def test_high_waste_triggers_tip(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="context_files", tokens=300, percentage=30.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="output", tokens=350, percentage=35.0),
                TokenCategory(category="tool_results", tokens=200, percentage=20.0),
            ),
            waste_estimate=35.0,
        )
        tips = get_optimization_recommendations(bd)
        assert any("waste" in t.lower() for t in tips)

    def test_output_dominance_tip(self) -> None:
        bd = TokenBreakdown(
            agent_id="a1",
            task_id="t1",
            total_tokens=1000,
            categories=(
                TokenCategory(category="output", tokens=600, percentage=60.0),
                TokenCategory(category="context_files", tokens=200, percentage=20.0),
                TokenCategory(category="system_prompt", tokens=100, percentage=10.0),
                TokenCategory(category="task_desc", tokens=50, percentage=5.0),
                TokenCategory(category="tool_results", tokens=50, percentage=5.0),
            ),
        )
        tips = get_optimization_recommendations(bd)
        assert any("Output" in t for t in tips)


# ---------------------------------------------------------------------------
# CategoryName enum
# ---------------------------------------------------------------------------


class TestCategoryName:
    """CategoryName StrEnum values."""

    def test_values(self) -> None:
        assert CategoryName.SYSTEM_PROMPT == "system_prompt"
        assert CategoryName.CONTEXT_FILES == "context_files"
        assert CategoryName.TASK_DESC == "task_desc"
        assert CategoryName.OUTPUT == "output"
        assert CategoryName.TOOL_RESULTS == "tool_results"

    def test_is_str(self) -> None:
        for name in CategoryName:
            assert isinstance(name, str)
