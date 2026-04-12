"""Tests for the LLM judge — verdict parsing, circuit breaker, resilience."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.eval.judge import (
    CircuitBreakerTripped,
    EvalJudge,
    JudgeVerdict,
    _parse_verdict,
)

# ---------------------------------------------------------------------------
# JudgeVerdict dataclass
# ---------------------------------------------------------------------------


class TestJudgeVerdict:
    def test_defaults(self) -> None:
        v = JudgeVerdict()
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0
        assert v.verdict == "FAIL"
        assert v.issues == []

    def test_average_score_zero(self) -> None:
        v = JudgeVerdict()
        assert v.average_score == pytest.approx(0.0)

    def test_average_score_perfect(self) -> None:
        v = JudgeVerdict(correctness=5, style=5, test_coverage=5, safety=5, verdict="PASS")
        assert v.average_score == pytest.approx(1.0)

    def test_average_score_mixed(self) -> None:
        v = JudgeVerdict(correctness=4, style=3, test_coverage=4, safety=5)
        # (4+3+4+5) / 20 = 16/20 = 0.8
        assert v.average_score == pytest.approx(0.8)

    def test_issues_list(self) -> None:
        v = JudgeVerdict(issues=["bad naming", "missing test"])
        assert len(v.issues) == 2

    def test_frozen(self) -> None:
        v = JudgeVerdict()
        with pytest.raises(AttributeError):
            v.correctness = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _parse_verdict — clean JSON
# ---------------------------------------------------------------------------


class TestParseVerdictCleanJson:
    def test_valid_json(self) -> None:
        raw = json.dumps(
            {
                "correctness": 4,
                "style": 3,
                "test_coverage": 4,
                "safety": 5,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 4
        assert v.style == 3
        assert v.test_coverage == 4
        assert v.safety == 5
        assert v.verdict == "PASS"
        assert v.issues == []

    def test_fail_verdict(self) -> None:
        raw = json.dumps(
            {
                "correctness": 2,
                "style": 1,
                "test_coverage": 0,
                "safety": 4,
                "verdict": "FAIL",
                "issues": ["missing tests", "bad naming"],
            }
        )
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"
        assert len(v.issues) == 2

    def test_missing_fields_default_to_zero(self) -> None:
        raw = json.dumps({"verdict": "FAIL"})
        v = _parse_verdict(raw)
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0


# ---------------------------------------------------------------------------
# _parse_verdict — LLM response quirks
# ---------------------------------------------------------------------------


class TestParseVerdictQuirks:
    def test_markdown_code_fence(self) -> None:
        raw = '```json\n{"correctness": 3, "style": 3, "test_coverage": 3, "safety": 3, "verdict": "PASS", "issues": []}\n```'
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"
        assert v.correctness == 3

    def test_json_with_surrounding_text(self) -> None:
        raw = 'Here is my review:\n{"correctness": 4, "style": 4, "test_coverage": 3, "safety": 5, "verdict": "PASS", "issues": []}\nThat is my verdict.'
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"
        assert v.correctness == 4

    def test_whitespace_padding(self) -> None:
        raw = '   \n  {"correctness": 2, "style": 2, "test_coverage": 2, "safety": 2, "verdict": "FAIL", "issues": ["low quality"]}  \n  '
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"
        assert v.issues == ["low quality"]


# ---------------------------------------------------------------------------
# _parse_verdict — score clamping
# ---------------------------------------------------------------------------


class TestParseVerdictClamping:
    def test_scores_above_max_clamped(self) -> None:
        raw = json.dumps(
            {
                "correctness": 10,
                "style": 99,
                "test_coverage": 7,
                "safety": 6,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 5
        assert v.style == 5
        assert v.test_coverage == 5
        assert v.safety == 5

    def test_negative_scores_clamped(self) -> None:
        raw = json.dumps(
            {
                "correctness": -1,
                "style": -5,
                "test_coverage": -10,
                "safety": -3,
                "verdict": "FAIL",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 0
        assert v.style == 0
        assert v.test_coverage == 0
        assert v.safety == 0

    def test_float_scores_truncated(self) -> None:
        raw = json.dumps(
            {
                "correctness": 3.7,
                "style": 4.2,
                "test_coverage": 2.9,
                "safety": 4.5,
                "verdict": "PASS",
                "issues": [],
            }
        )
        v = _parse_verdict(raw)
        assert v.correctness == 3
        assert v.style == 4
        assert v.test_coverage == 2
        assert v.safety == 4


# ---------------------------------------------------------------------------
# _parse_verdict — verdict normalization
# ---------------------------------------------------------------------------


class TestParseVerdictNormalization:
    def test_lowercase_pass(self) -> None:
        raw = json.dumps({"verdict": "pass", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "PASS"

    def test_lowercase_fail(self) -> None:
        raw = json.dumps({"verdict": "fail", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"

    def test_unknown_verdict_defaults_fail(self) -> None:
        raw = json.dumps({"verdict": "MAYBE", "issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"

    def test_missing_verdict_defaults_fail(self) -> None:
        raw = json.dumps({"issues": []})
        v = _parse_verdict(raw)
        assert v.verdict == "FAIL"


# ---------------------------------------------------------------------------
# _parse_verdict — error cases
# ---------------------------------------------------------------------------


class TestParseVerdictErrors:
    def test_invalid_json_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_verdict("not json at all")

    def test_empty_string_raises(self) -> None:
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_verdict("")

    def test_non_dict_json_raises(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            _parse_verdict("[1, 2, 3]")

    def test_issues_non_list_handled(self) -> None:
        raw = json.dumps({"verdict": "FAIL", "issues": "single issue"})
        v = _parse_verdict(raw)
        # Non-list issues should result in empty list
        assert v.issues == []


# ---------------------------------------------------------------------------
# CircuitBreakerTripped exception
# ---------------------------------------------------------------------------


class TestCircuitBreakerTripped:
    def test_is_runtime_error(self) -> None:
        assert issubclass(CircuitBreakerTripped, RuntimeError)

    def test_message(self) -> None:
        exc = CircuitBreakerTripped("tripped")
        assert str(exc) == "tripped"


# ---------------------------------------------------------------------------
# EvalJudge — initialization and configuration
# ---------------------------------------------------------------------------

_GOOD_JSON = json.dumps(
    {"correctness": 4, "style": 4, "test_coverage": 3, "safety": 5, "verdict": "PASS", "issues": []}
)


class TestEvalJudgeInit:
    def test_defaults(self) -> None:
        judge = EvalJudge()
        assert judge.model == "anthropic/claude-sonnet-4"
        assert judge.provider == "openrouter_free"
        assert judge.circuit_breaker_threshold == 3
        assert judge.consecutive_failures == 0

    def test_custom_config(self) -> None:
        judge = EvalJudge(
            model="openai/gpt-5.4",
            provider="openai",
            backoff_schedule=(1.0, 2.0),
            circuit_breaker_threshold=5,
        )
        assert judge.model == "openai/gpt-5.4"
        assert judge.provider == "openai"
        assert judge.backoff_schedule == (1.0, 2.0)
        assert judge.circuit_breaker_threshold == 5

    def test_reset(self) -> None:
        judge = EvalJudge()
        judge._consecutive_failures = 5
        judge.reset()
        assert judge.consecutive_failures == 0


# ---------------------------------------------------------------------------
# EvalJudge — circuit breaker
# ---------------------------------------------------------------------------


class TestEvalJudgeCircuitBreaker:
    def test_no_trip_below_threshold(self) -> None:
        judge = EvalJudge(circuit_breaker_threshold=3)
        judge._consecutive_failures = 2
        judge.circuit_breaker()  # Should not raise

    def test_trips_at_threshold(self) -> None:
        judge = EvalJudge(circuit_breaker_threshold=3)
        judge._consecutive_failures = 3
        with pytest.raises(CircuitBreakerTripped, match="3 consecutive"):
            judge.circuit_breaker()

    def test_trips_above_threshold(self) -> None:
        judge = EvalJudge(circuit_breaker_threshold=3)
        judge._consecutive_failures = 10
        with pytest.raises(CircuitBreakerTripped):
            judge.circuit_breaker()


# ---------------------------------------------------------------------------
# EvalJudge — retry_with_backoff
# ---------------------------------------------------------------------------


class TestEvalJudgeRetryBackoff:
    def test_sleeps_correct_duration(self) -> None:
        judge = EvalJudge(backoff_schedule=(2.0, 4.0, 8.0))
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(judge.retry_with_backoff(0))
            mock_sleep.assert_awaited_once_with(2.0)

    def test_second_attempt_backoff(self) -> None:
        judge = EvalJudge(backoff_schedule=(2.0, 4.0, 8.0))
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(judge.retry_with_backoff(1))
            mock_sleep.assert_awaited_once_with(4.0)

    def test_beyond_schedule_no_sleep(self) -> None:
        judge = EvalJudge(backoff_schedule=(2.0,))
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(judge.retry_with_backoff(5))
            mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# EvalJudge — dual_attempt
# ---------------------------------------------------------------------------


class TestEvalJudgeDualAttempt:
    def test_first_attempt_succeeds(self) -> None:
        judge = EvalJudge()
        mock_llm = AsyncMock(return_value=_GOOD_JSON)
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.dual_attempt("test prompt"))
        assert verdict.verdict == "PASS"
        assert mock_llm.await_count == 1

    def test_falls_back_to_strict(self) -> None:
        judge = EvalJudge()
        # First call returns garbage, second returns valid JSON
        mock_llm = AsyncMock(side_effect=["not valid json {{{", _GOOD_JSON])
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.dual_attempt("test prompt"))
        assert verdict.verdict == "PASS"
        assert mock_llm.await_count == 2

    def test_both_attempts_fail_raises(self) -> None:
        judge = EvalJudge()
        mock_llm = AsyncMock(side_effect=["garbage", "still garbage"])
        with patch("bernstein.core.routing.llm.call_llm", mock_llm), pytest.raises((json.JSONDecodeError, ValueError)):
            asyncio.run(judge.dual_attempt("test prompt"))

    def test_llm_error_propagates(self) -> None:
        judge = EvalJudge()
        mock_llm = AsyncMock(side_effect=RuntimeError("API down"))
        with patch("bernstein.core.routing.llm.call_llm", mock_llm), pytest.raises(RuntimeError, match="API down"):
            asyncio.run(judge.dual_attempt("test prompt"))


# ---------------------------------------------------------------------------
# EvalJudge — review_git_diff (integration of all resilience)
# ---------------------------------------------------------------------------


class TestEvalJudgeReviewGitDiff:
    def test_success_on_first_try(self) -> None:
        judge = EvalJudge()
        mock_llm = AsyncMock(return_value=_GOOD_JSON)
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.review_git_diff(task_description="fix bug", git_diff="diff --git a/b"))
        assert verdict.verdict == "PASS"
        assert judge.consecutive_failures == 0

    def test_retries_on_runtime_error(self) -> None:
        judge = EvalJudge(backoff_schedule=(0.0, 0.0, 0.0, 0.0))
        mock_llm = AsyncMock(side_effect=[RuntimeError("timeout"), _GOOD_JSON])
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.review_git_diff(task_description="fix bug", git_diff="diff"))
        assert verdict.verdict == "PASS"
        assert judge.consecutive_failures == 0

    def test_circuit_breaker_trips(self) -> None:
        judge = EvalJudge(
            backoff_schedule=(0.0, 0.0, 0.0, 0.0),
            circuit_breaker_threshold=3,
        )
        mock_llm = AsyncMock(side_effect=RuntimeError("down"))
        with patch("bernstein.core.routing.llm.call_llm", mock_llm), pytest.raises(CircuitBreakerTripped):
            asyncio.run(judge.review_git_diff(task_description="fix bug", git_diff="diff"))
        assert judge.consecutive_failures >= 3

    def test_resets_failures_on_success(self) -> None:
        judge = EvalJudge(backoff_schedule=(0.0, 0.0, 0.0, 0.0))
        judge._consecutive_failures = 2
        mock_llm = AsyncMock(return_value=_GOOD_JSON)
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.review_git_diff(task_description="fix bug", git_diff="diff"))
        assert verdict.verdict == "PASS"
        assert judge.consecutive_failures == 0

    def test_truncates_large_diff(self) -> None:
        judge = EvalJudge()
        large_diff = "x" * 20000
        mock_llm = AsyncMock(return_value=_GOOD_JSON)
        with patch("bernstein.core.routing.llm.call_llm", mock_llm) as m:
            asyncio.run(judge.review_git_diff(task_description="task", git_diff=large_diff))
        # The prompt should have truncated the diff to 8000 chars
        call_prompt = m.call_args[0][0]
        assert "x" * 8000 in call_prompt
        assert "x" * 8001 not in call_prompt

    def test_exhausted_attempts_returns_fail(self) -> None:
        # With threshold=10 so circuit breaker doesn't trip, but all attempts parse-fail
        judge = EvalJudge(
            backoff_schedule=(0.0, 0.0),
            circuit_breaker_threshold=10,
        )
        mock_llm = AsyncMock(return_value="not json")
        with patch("bernstein.core.routing.llm.call_llm", mock_llm):
            verdict = asyncio.run(judge.review_git_diff(task_description="fix bug", git_diff="diff"))
        assert verdict.verdict == "FAIL"
        assert "exhausted" in verdict.issues[0].lower()
