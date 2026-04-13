"""Tests for LLM-as-judge evaluation framework.

Covers dataclass construction, prompt building, response parsing,
placeholder scoring, and Markdown report rendering.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.quality.llm_judge import (
    DEFAULT_DIMENSIONS,
    DimensionScore,
    JudgeDimension,
    JudgeResult,
    build_judge_prompt,
    parse_judge_response,
    render_judge_report,
    score_output,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    scores: dict[str, dict[str, object]],
) -> str:
    """Build a valid JSON judge response string."""
    return json.dumps({"scores": scores})


def _default_scores_payload(score: float = 0.8) -> dict[str, dict[str, object]]:
    """Return a scores dict matching DEFAULT_DIMENSIONS."""
    return {d.name: {"score": score, "reasoning": f"{d.name} looks good."} for d in DEFAULT_DIMENSIONS}


# ---------------------------------------------------------------------------
# JudgeDimension dataclass
# ---------------------------------------------------------------------------


class TestJudgeDimension:
    """Tests for the JudgeDimension frozen dataclass."""

    def test_construction(self) -> None:
        dim = JudgeDimension(name="accuracy", weight=0.5, description="How accurate")
        assert dim.name == "accuracy"
        assert dim.weight == pytest.approx(0.5)
        assert dim.description == "How accurate"

    def test_frozen(self) -> None:
        dim = JudgeDimension(name="x", weight=0.1, description="y")
        with pytest.raises(AttributeError):
            dim.name = "z"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = JudgeDimension(name="x", weight=0.1, description="y")
        b = JudgeDimension(name="x", weight=0.1, description="y")
        assert a == b


# ---------------------------------------------------------------------------
# DimensionScore dataclass
# ---------------------------------------------------------------------------


class TestDimensionScore:
    """Tests for the DimensionScore frozen dataclass."""

    def test_construction(self) -> None:
        dim = JudgeDimension(name="foo", weight=0.5, description="bar")
        ds = DimensionScore(dimension=dim, score=0.75, reasoning="good")
        assert ds.dimension is dim
        assert ds.score == pytest.approx(0.75)
        assert ds.reasoning == "good"

    def test_frozen(self) -> None:
        dim = JudgeDimension(name="foo", weight=0.5, description="bar")
        ds = DimensionScore(dimension=dim, score=0.5, reasoning="ok")
        with pytest.raises(AttributeError):
            ds.score = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# JudgeResult dataclass
# ---------------------------------------------------------------------------


class TestJudgeResult:
    """Tests for the JudgeResult frozen dataclass."""

    def test_construction(self) -> None:
        result = JudgeResult(
            task_id="t-1",
            overall_score=0.85,
            dimensions=(),
            model_used="test-model",
            cost_usd=0.01,
        )
        assert result.task_id == "t-1"
        assert result.overall_score == pytest.approx(0.85)
        assert result.dimensions == ()
        assert result.model_used == "test-model"
        assert result.cost_usd == pytest.approx(0.01)

    def test_frozen(self) -> None:
        result = JudgeResult(
            task_id="t-1",
            overall_score=0.5,
            dimensions=(),
            model_used="m",
            cost_usd=0.0,
        )
        with pytest.raises(AttributeError):
            result.overall_score = 0.9  # type: ignore[misc]

    def test_dimensions_is_tuple(self) -> None:
        """Dimensions are stored as a tuple (immutable)."""
        dim = JudgeDimension(name="a", weight=1.0, description="b")
        ds = DimensionScore(dimension=dim, score=0.5, reasoning="ok")
        result = JudgeResult(
            task_id="t-2",
            overall_score=0.5,
            dimensions=(ds,),
            model_used="m",
            cost_usd=0.0,
        )
        assert isinstance(result.dimensions, tuple)
        assert len(result.dimensions) == 1


# ---------------------------------------------------------------------------
# DEFAULT_DIMENSIONS
# ---------------------------------------------------------------------------


class TestDefaultDimensions:
    """Tests for the default dimension set."""

    def test_five_dimensions(self) -> None:
        assert len(DEFAULT_DIMENSIONS) == 5

    def test_weights_sum_to_one(self) -> None:
        total = sum(d.weight for d in DEFAULT_DIMENSIONS)
        assert abs(total - 1.0) < 1e-9

    def test_all_positive_weights(self) -> None:
        for d in DEFAULT_DIMENSIONS:
            assert d.weight > 0.0

    def test_expected_names(self) -> None:
        names = {d.name for d in DEFAULT_DIMENSIONS}
        assert names == {
            "task_completion",
            "code_correctness",
            "edge_cases",
            "maintainability",
            "style",
        }

    def test_all_have_descriptions(self) -> None:
        for d in DEFAULT_DIMENSIONS:
            assert len(d.description) > 10


# ---------------------------------------------------------------------------
# build_judge_prompt
# ---------------------------------------------------------------------------


class TestBuildJudgePrompt:
    """Tests for prompt construction."""

    def test_contains_task_description(self) -> None:
        prompt = build_judge_prompt("Fix the login bug", "def fix(): pass")
        assert "Fix the login bug" in prompt

    def test_contains_agent_output(self) -> None:
        prompt = build_judge_prompt("task", "def fix(): pass")
        assert "def fix(): pass" in prompt

    def test_contains_all_default_dimensions(self) -> None:
        prompt = build_judge_prompt("task", "output")
        for d in DEFAULT_DIMENSIONS:
            assert d.name in prompt

    def test_custom_dimensions(self) -> None:
        dims = (JudgeDimension(name="custom_dim", weight=1.0, description="Custom"),)
        prompt = build_judge_prompt("task", "output", dims)
        assert "custom_dim" in prompt
        assert "task_completion" not in prompt

    def test_raises_on_empty_dimensions(self) -> None:
        with pytest.raises(ValueError, match="At least one dimension"):
            build_judge_prompt("task", "output", dimensions=())

    def test_raises_on_negative_weight(self) -> None:
        dims = (JudgeDimension(name="bad", weight=-0.1, description="x"),)
        with pytest.raises(ValueError, match="negative weight"):
            build_judge_prompt("task", "output", dims)

    def test_raises_on_duplicate_name(self) -> None:
        dims = (
            JudgeDimension(name="dup", weight=0.5, description="a"),
            JudgeDimension(name="dup", weight=0.5, description="b"),
        )
        with pytest.raises(ValueError, match="Duplicate dimension"):
            build_judge_prompt("task", "output", dims)

    def test_raises_on_all_zero_weights(self) -> None:
        dims = (
            JudgeDimension(name="a", weight=0.0, description="x"),
            JudgeDimension(name="b", weight=0.0, description="y"),
        )
        with pytest.raises(ValueError, match="Total weight.*must be positive"):
            build_judge_prompt("task", "output", dims)

    def test_prompt_requests_json_output(self) -> None:
        prompt = build_judge_prompt("task", "output")
        assert "JSON" in prompt


# ---------------------------------------------------------------------------
# parse_judge_response
# ---------------------------------------------------------------------------


class TestParseJudgeResponse:
    """Tests for parsing LLM judge responses."""

    def test_valid_response(self) -> None:
        payload = _default_scores_payload(0.8)
        response = _make_response(payload)
        scores = parse_judge_response(response)
        assert len(scores) == 5
        for ds in scores:
            assert ds.score == pytest.approx(0.8)

    def test_response_with_markdown_fences(self) -> None:
        payload = _default_scores_payload(0.7)
        response = f"```json\n{_make_response(payload)}\n```"
        scores = parse_judge_response(response)
        assert len(scores) == 5

    def test_response_with_surrounding_text(self) -> None:
        payload = _default_scores_payload(0.6)
        response = f"Here is the result:\n{_make_response(payload)}\nDone."
        scores = parse_judge_response(response)
        assert len(scores) == 5

    def test_clamps_score_above_one(self) -> None:
        payload = _default_scores_payload()
        payload["task_completion"] = {"score": 1.5, "reasoning": "too high"}
        scores = parse_judge_response(_make_response(payload))
        tc = next(s for s in scores if s.dimension.name == "task_completion")
        assert tc.score == pytest.approx(1.0)

    def test_clamps_score_below_zero(self) -> None:
        payload = _default_scores_payload()
        payload["task_completion"] = {"score": -0.5, "reasoning": "too low"}
        scores = parse_judge_response(_make_response(payload))
        tc = next(s for s in scores if s.dimension.name == "task_completion")
        assert tc.score == pytest.approx(0.0)

    def test_raises_on_garbage(self) -> None:
        with pytest.raises(ValueError, match="No JSON"):
            parse_judge_response("This is not JSON at all.")

    def test_raises_on_missing_scores_key(self) -> None:
        with pytest.raises(ValueError, match="missing 'scores'"):
            parse_judge_response('{"other": 1}')

    def test_raises_on_missing_dimension(self) -> None:
        payload = _default_scores_payload()
        del payload["style"]
        with pytest.raises(ValueError, match="Missing.*style"):
            parse_judge_response(_make_response(payload))

    def test_raises_on_non_numeric_score(self) -> None:
        payload = _default_scores_payload()
        payload["style"] = {"score": "high", "reasoning": "oops"}
        with pytest.raises(ValueError, match="not a number"):
            parse_judge_response(_make_response(payload))

    def test_custom_dimensions(self) -> None:
        dims = (JudgeDimension(name="custom", weight=1.0, description="d"),)
        payload = {"custom": {"score": 0.9, "reasoning": "great"}}
        scores = parse_judge_response(_make_response(payload), dims)
        assert len(scores) == 1
        assert scores[0].dimension.name == "custom"
        assert scores[0].score == pytest.approx(0.9)

    def test_integer_score_accepted(self) -> None:
        payload = _default_scores_payload()
        payload["style"] = {"score": 1, "reasoning": "integer"}
        scores = parse_judge_response(_make_response(payload))
        style = next(s for s in scores if s.dimension.name == "style")
        assert style.score == pytest.approx(1.0)

    def test_missing_reasoning_defaults_to_empty(self) -> None:
        payload: dict[str, dict[str, object]] = {d.name: {"score": 0.5} for d in DEFAULT_DIMENSIONS}
        scores = parse_judge_response(_make_response(payload))
        for ds in scores:
            assert ds.reasoning == ""


# ---------------------------------------------------------------------------
# score_output
# ---------------------------------------------------------------------------


class TestScoreOutput:
    """Tests for the placeholder orchestration function."""

    def test_returns_judge_result(self) -> None:
        result = score_output("task desc", "agent output")
        assert isinstance(result, JudgeResult)

    def test_placeholder_scores_are_zero(self) -> None:
        result = score_output("task", "output")
        assert result.overall_score == pytest.approx(0.0)
        for ds in result.dimensions:
            assert ds.score == pytest.approx(0.0)

    def test_default_dimensions_used(self) -> None:
        result = score_output("task", "output")
        assert len(result.dimensions) == 5

    def test_custom_task_id(self) -> None:
        result = score_output("task", "output", task_id="my-task-42")
        assert result.task_id == "my-task-42"

    def test_auto_generated_task_id(self) -> None:
        result = score_output("task", "output")
        assert len(result.task_id) > 0  # UUID string

    def test_custom_model(self) -> None:
        result = score_output("task", "output", model="gpt-4o")
        assert result.model_used == "gpt-4o"

    def test_cost_is_zero(self) -> None:
        result = score_output("task", "output")
        assert result.cost_usd == pytest.approx(0.0)

    def test_custom_dimensions(self) -> None:
        dims = (JudgeDimension(name="only_dim", weight=1.0, description="d"),)
        result = score_output("task", "output", dims)
        assert len(result.dimensions) == 1
        assert result.dimensions[0].dimension.name == "only_dim"

    def test_raises_on_invalid_dimensions(self) -> None:
        with pytest.raises(ValueError):
            score_output("task", "output", dimensions=())


# ---------------------------------------------------------------------------
# render_judge_report
# ---------------------------------------------------------------------------


class TestRenderJudgeReport:
    """Tests for Markdown scorecard rendering."""

    def _make_result(
        self,
        overall: float = 0.85,
        scores: tuple[float, ...] | None = None,
    ) -> JudgeResult:
        if scores is None:
            scores = (0.9, 0.8, 0.7, 0.85, 0.95)
        dims = DEFAULT_DIMENSIONS
        dim_scores = tuple(
            DimensionScore(
                dimension=d,
                score=s,
                reasoning=f"Reasoning for {d.name}.",
            )
            for d, s in zip(dims, scores, strict=True)
        )
        return JudgeResult(
            task_id="t-report",
            overall_score=overall,
            dimensions=dim_scores,
            model_used="test-model",
            cost_usd=0.0042,
        )

    def test_contains_task_id(self) -> None:
        report = render_judge_report(self._make_result())
        assert "t-report" in report

    def test_contains_overall_score(self) -> None:
        report = render_judge_report(self._make_result(overall=0.85))
        assert "0.85" in report

    def test_contains_model(self) -> None:
        report = render_judge_report(self._make_result())
        assert "test-model" in report

    def test_contains_cost(self) -> None:
        report = render_judge_report(self._make_result())
        assert "0.0042" in report

    def test_contains_dimension_names(self) -> None:
        report = render_judge_report(self._make_result())
        for d in DEFAULT_DIMENSIONS:
            assert d.name in report

    def test_contains_reasoning(self) -> None:
        report = render_judge_report(self._make_result())
        assert "Reasoning for task_completion" in report

    def test_contains_grade(self) -> None:
        report = render_judge_report(self._make_result(overall=0.92))
        assert "(A)" in report

    def test_grade_f_for_low_score(self) -> None:
        report = render_judge_report(self._make_result(overall=0.3))
        assert "(F)" in report

    def test_empty_dimensions(self) -> None:
        result = JudgeResult(
            task_id="t-empty",
            overall_score=0.0,
            dimensions=(),
            model_used="m",
            cost_usd=0.0,
        )
        report = render_judge_report(result)
        assert "t-empty" in report
        assert "0.00" in report

    def test_table_headers(self) -> None:
        report = render_judge_report(self._make_result())
        assert "Dimension" in report
        assert "Weight" in report
        assert "Score" in report
        assert "Grade" in report
