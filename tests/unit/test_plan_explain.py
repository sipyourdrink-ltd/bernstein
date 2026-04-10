"""Tests for bernstein.cli.plan_explain -- plan analysis and explanation."""

from __future__ import annotations

from bernstein.cli.plan_explain import (
    PlanSummary,
    analyze_plan,
    format_plan_explanation,
    generate_explanation,
)

# ---------------------------------------------------------------------------
# Fixtures -- sample plan dicts
# ---------------------------------------------------------------------------

SIMPLE_PLAN: dict = {
    "name": "Simple",
    "description": "A simple one-stage plan.",
    "stages": [
        {
            "name": "Build",
            "steps": [
                {"goal": "Write code", "role": "backend", "scope": "small", "complexity": "low"},
            ],
        },
    ],
}

MULTI_STAGE_PLAN: dict = {
    "name": "Multi-Stage",
    "description": "A three-stage plan with dependencies.",
    "stages": [
        {
            "name": "Foundation",
            "steps": [
                {"title": "Create skeleton", "role": "backend", "scope": "small", "complexity": "low"},
                {"title": "Define models", "role": "backend", "scope": "small", "complexity": "medium"},
            ],
        },
        {
            "name": "Features",
            "depends_on": ["Foundation"],
            "steps": [
                {"title": "Auth endpoints", "role": "backend", "scope": "medium", "complexity": "medium"},
                {"title": "User CRUD", "role": "backend", "scope": "medium", "complexity": "medium"},
                {"title": "Admin panel", "role": "frontend", "scope": "large", "complexity": "high"},
            ],
        },
        {
            "name": "Quality",
            "depends_on": ["Features"],
            "steps": [
                {"title": "Integration tests", "role": "qa", "scope": "medium", "complexity": "medium"},
                {"title": "Security review", "role": "security", "scope": "small", "complexity": "low"},
            ],
        },
    ],
}

EMPTY_PLAN: dict = {
    "name": "Empty",
}

NO_STAGES_PLAN: dict = {
    "name": "No Stages",
    "stages": [],
}


# ---------------------------------------------------------------------------
# analyze_plan
# ---------------------------------------------------------------------------


class TestAnalyzePlan:
    """Tests for analyze_plan()."""

    def test_simple_plan_counts(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert summary.total_stages == 1
        assert summary.total_steps == 1

    def test_simple_plan_roles(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert summary.roles_used == ["backend"]

    def test_simple_plan_description(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert summary.description == "A simple one-stage plan."

    def test_simple_plan_critical_path(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert summary.critical_path_length == 1

    def test_simple_plan_agents(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert summary.estimated_agents == 1

    def test_simple_plan_cost_range(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        low, high = summary.estimated_cost_range
        assert low >= 0.0
        assert high >= low

    def test_multi_stage_counts(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        assert summary.total_stages == 3
        assert summary.total_steps == 7

    def test_multi_stage_roles(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        assert summary.roles_used == ["backend", "frontend", "qa", "security"]

    def test_multi_stage_critical_path(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        # Foundation -> Features -> Quality = 3
        assert summary.critical_path_length == 3

    def test_multi_stage_peak_agents(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        # Features stage has 3 steps -- widest
        assert summary.estimated_agents == 3

    def test_multi_stage_cost_range(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        low, high = summary.estimated_cost_range
        assert low > 0.0
        assert high > low

    def test_empty_plan(self) -> None:
        summary = analyze_plan(EMPTY_PLAN)
        assert summary.total_stages == 0
        assert summary.total_steps == 0
        assert summary.roles_used == []
        assert summary.estimated_agents == 0
        assert summary.critical_path_length == 0

    def test_no_stages_plan(self) -> None:
        summary = analyze_plan(NO_STAGES_PLAN)
        assert summary.total_stages == 0
        assert summary.total_steps == 0

    def test_summary_is_frozen(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        assert isinstance(summary, PlanSummary)


# ---------------------------------------------------------------------------
# generate_explanation
# ---------------------------------------------------------------------------


class TestGenerateExplanation:
    """Tests for generate_explanation()."""

    def test_includes_stage_count(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "3 stages" in text

    def test_includes_step_count(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "7 steps" in text

    def test_includes_roles(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "backend" in text
        assert "qa" in text

    def test_includes_critical_path(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "critical path" in text.lower()

    def test_includes_cost(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "Estimated cost:" in text
        assert "$" in text

    def test_includes_agents(self) -> None:
        summary = analyze_plan(MULTI_STAGE_PLAN)
        text = generate_explanation(summary)
        assert "agent" in text.lower()

    def test_singular_stage(self) -> None:
        summary = analyze_plan(SIMPLE_PLAN)
        text = generate_explanation(summary)
        assert "1 stage " in text

    def test_empty_plan_explanation(self) -> None:
        summary = analyze_plan(EMPTY_PLAN)
        text = generate_explanation(summary)
        assert "0 stages" in text
        assert "0 steps" in text


# ---------------------------------------------------------------------------
# format_plan_explanation
# ---------------------------------------------------------------------------


class TestFormatPlanExplanation:
    """Tests for format_plan_explanation()."""

    def test_includes_plan_name(self) -> None:
        output = format_plan_explanation(MULTI_STAGE_PLAN)
        assert "Multi-Stage" in output

    def test_includes_description(self) -> None:
        output = format_plan_explanation(SIMPLE_PLAN)
        assert "A simple one-stage plan." in output

    def test_includes_summary_section(self) -> None:
        output = format_plan_explanation(MULTI_STAGE_PLAN)
        assert "Summary" in output
        assert "Stages:" in output
        assert "Steps:" in output
        assert "Roles:" in output

    def test_includes_explanation_paragraph(self) -> None:
        output = format_plan_explanation(MULTI_STAGE_PLAN)
        assert "This plan has" in output

    def test_empty_plan_format(self) -> None:
        output = format_plan_explanation(EMPTY_PLAN)
        assert "Empty" in output
        assert "Stages:" in output

    def test_no_description_omitted(self) -> None:
        output = format_plan_explanation(NO_STAGES_PLAN)
        # No description key -- should not crash, should still have stats
        assert "No Stages" in output
        assert "Stages:" in output
