"""Tests for task completion confidence scoring (TASK-012)."""

from __future__ import annotations

import pytest
from bernstein.core.completion_confidence import (
    ConfidenceWeights,
    compute_batch_confidence,
    compute_confidence,
)

from bernstein.core.models import GuardrailResult, JanitorResult, JudgeVerdict


def _janitor_result(
    task_id: str = "t1",
    signal_results: list[tuple[str, bool, str]] | None = None,
    judge_verdict: JudgeVerdict | None = None,
    guardrail_results: list[GuardrailResult] | None = None,
) -> JanitorResult:
    return JanitorResult(
        task_id=task_id,
        passed=True,
        signal_results=signal_results or [],
        judge_verdict=judge_verdict,
        guardrail_results=guardrail_results or [],
    )


class TestComputeConfidence:
    def test_base_score_with_no_data(self) -> None:
        result = _janitor_result()
        score = compute_confidence(result)
        # Base 0.5 + no_signals_penalty (-0.2) = 0.3
        assert score.score == pytest.approx(0.3, abs=0.01)
        assert score.task_id == "t1"

    def test_all_signals_pass(self) -> None:
        result = _janitor_result(
            signal_results=[
                ("test_passes: pytest", True, "exit 0"),
                ("path_exists: src/foo.py", True, "exists"),
            ]
        )
        score = compute_confidence(result)
        # Base 0.5 + 2 * 0.2 = 0.9
        assert score.score == pytest.approx(0.9, abs=0.01)
        assert score.signals_passed == 2
        assert score.signals_failed == 0

    def test_mixed_signals(self) -> None:
        result = _janitor_result(
            signal_results=[
                ("test_passes: pytest", True, "exit 0"),
                ("path_exists: missing.py", False, "not found"),
            ]
        )
        score = compute_confidence(result)
        # Base 0.5 + 0.2 + (-0.3) = 0.4
        assert score.score == pytest.approx(0.4, abs=0.01)
        assert score.signals_passed == 1
        assert score.signals_failed == 1

    def test_all_signals_fail(self) -> None:
        result = _janitor_result(
            signal_results=[
                ("test_passes: pytest", False, "non-zero exit"),
                ("path_exists: missing.py", False, "not found"),
            ]
        )
        score = compute_confidence(result)
        # Base 0.5 + 2 * (-0.3) = -0.1, clamped to 0.0
        assert score.score == 0.0

    def test_judge_accept(self) -> None:
        verdict = JudgeVerdict(verdict="accept", confidence=0.9, feedback="looks good")
        result = _janitor_result(judge_verdict=verdict)
        score = compute_confidence(result)
        assert score.judge_accepted is True
        assert score.judge_confidence == 0.9
        # Base 0.5 + no_signals(-0.2) + judge(0.3 * 0.9 = 0.27) = 0.57
        assert score.score == pytest.approx(0.57, abs=0.01)

    def test_judge_retry(self) -> None:
        verdict = JudgeVerdict(verdict="retry", confidence=0.8, feedback="needs work")
        result = _janitor_result(judge_verdict=verdict)
        score = compute_confidence(result)
        assert score.judge_accepted is False
        # Base 0.5 + no_signals(-0.2) + judge(-0.3) = 0.0
        assert score.score == 0.0

    def test_guardrails_pass(self) -> None:
        guardrails = [
            GuardrailResult(check="lint", passed=True, blocked=False, detail="ok"),
            GuardrailResult(check="security", passed=True, blocked=False, detail="ok"),
        ]
        result = _janitor_result(guardrail_results=guardrails)
        score = compute_confidence(result)
        assert score.guardrails_passed == 2
        assert score.guardrails_blocked == 0
        # Base 0.5 + no_signals(-0.2) + 2*0.1 = 0.5
        assert score.score == pytest.approx(0.5, abs=0.01)

    def test_guardrail_block(self) -> None:
        guardrails = [
            GuardrailResult(check="secret_detection", passed=False, blocked=True, detail="secrets found"),
        ]
        result = _janitor_result(guardrail_results=guardrails)
        score = compute_confidence(result)
        assert score.guardrails_blocked == 1
        # Base 0.5 + no_signals(-0.2) + (-0.5) = -0.2, clamped to 0.0
        assert score.score == 0.0

    def test_custom_weights(self) -> None:
        weights = ConfidenceWeights(signal_pass=0.5, no_signals_penalty=0.0)
        result = _janitor_result(signal_results=[("test", True, "ok")])
        score = compute_confidence(result, weights=weights)
        # Base 0.5 + 0.5 = 1.0
        assert score.score == pytest.approx(1.0, abs=0.01)

    def test_score_clamped_to_one(self) -> None:
        weights = ConfidenceWeights(signal_pass=1.0)
        result = _janitor_result(
            signal_results=[
                ("t1", True, "ok"),
                ("t2", True, "ok"),
            ]
        )
        score = compute_confidence(result, weights=weights)
        assert score.score <= 1.0

    def test_score_clamped_to_zero(self) -> None:
        weights = ConfidenceWeights(signal_fail=-5.0)
        result = _janitor_result(signal_results=[("t1", False, "fail")])
        score = compute_confidence(result, weights=weights)
        assert score.score >= 0.0

    def test_breakdown_keys(self) -> None:
        result = _janitor_result(signal_results=[("t1", True, "ok")])
        score = compute_confidence(result)
        assert "base" in score.breakdown
        assert "signals" in score.breakdown


class TestComputeBatchConfidence:
    def test_empty_batch(self) -> None:
        results = compute_batch_confidence([])
        assert results == []

    def test_batch_of_two(self) -> None:
        r1 = _janitor_result("t1", signal_results=[("test", True, "ok")])
        r2 = _janitor_result("t2", signal_results=[("test", False, "fail")])
        scores = compute_batch_confidence([r1, r2])
        assert len(scores) == 2
        assert scores[0].task_id == "t1"
        assert scores[1].task_id == "t2"
        assert scores[0].score > scores[1].score
