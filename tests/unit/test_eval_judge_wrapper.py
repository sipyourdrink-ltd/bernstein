"""Exercise the public judge wrapper with a fake in-process judge."""

from unittest.mock import AsyncMock, Mock

import pytest

from bernstein.eval import judge


@pytest.mark.asyncio
async def test_wrapper_forwards_model_and_review_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    verdict = judge.JudgeVerdict(verdict="PASS")
    review = AsyncMock(return_value=verdict)
    factory = Mock(return_value=Mock(review_git_diff=review))
    monkeypatch.setattr(judge, "EvalJudge", factory)

    assert (
        await judge.judge_code_change(
            task_description="Update a label",
            git_diff="+label",
            test_results="passed",
            model="test-model",
            provider="test",
        )
        is verdict
    )
    factory.assert_called_once_with(model="test-model", provider="test")
    review.assert_awaited_once_with(task_description="Update a label", git_diff="+label", test_results="passed")


@pytest.mark.asyncio
async def test_wrapper_returns_fail_when_breaker_is_tripped(monkeypatch: pytest.MonkeyPatch) -> None:
    review = AsyncMock(side_effect=judge.CircuitBreakerTripped("open"))
    monkeypatch.setattr(judge, "EvalJudge", Mock(return_value=Mock(review_git_diff=review)))

    verdict = await judge.judge_code_change(task_description="Update a label", git_diff="+label")

    assert verdict.verdict == "FAIL"
    assert verdict.issues == ["Judge circuit breaker tripped"]
