"""Task completion confidence scoring from janitor verification results.

Computes a confidence score (0.0-1.0) for each completed task based on
the janitor's verification signals, judge verdicts, and guardrail results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import JanitorResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfidenceWeights:
    """Weights for different verification signal types.

    Attributes:
        signal_pass: Weight for each passing signal check.
        signal_fail: Penalty for each failing signal check.
        judge_accept: Weight for a judge "accept" verdict.
        judge_confidence_mult: Multiply judge weight by judge's own confidence.
        guardrail_pass: Weight for each passing guardrail.
        guardrail_block: Penalty for a blocking guardrail failure.
        no_signals_penalty: Penalty when no signals were defined.
    """

    signal_pass: float = 0.2
    signal_fail: float = -0.3
    judge_accept: float = 0.3
    judge_confidence_mult: bool = True
    guardrail_pass: float = 0.1
    guardrail_block: float = -0.5
    no_signals_penalty: float = -0.2


@dataclass(frozen=True)
class ConfidenceScore:
    """Confidence score for a task's completion.

    Attributes:
        task_id: The task identifier.
        score: Overall confidence score, clamped to [0.0, 1.0].
        signals_passed: Number of signals that passed.
        signals_failed: Number of signals that failed.
        judge_accepted: Whether the LLM judge accepted the result.
        judge_confidence: The judge's own confidence value (if evaluated).
        guardrails_passed: Number of guardrails that passed.
        guardrails_blocked: Number of guardrails that blocked.
        breakdown: Dict mapping component names to their score contribution.
    """

    task_id: str
    score: float
    signals_passed: int = 0
    signals_failed: int = 0
    judge_accepted: bool | None = None
    judge_confidence: float | None = None
    guardrails_passed: int = 0
    guardrails_blocked: int = 0
    breakdown: dict[str, float] = field(default_factory=dict[str, float])


def compute_confidence(
    result: JanitorResult,
    *,
    weights: ConfidenceWeights | None = None,
) -> ConfidenceScore:
    """Compute a completion confidence score from janitor verification results.

    The score is the sum of weighted components, clamped to [0.0, 1.0]:
    - Each passing signal adds signal_pass weight.
    - Each failing signal adds signal_fail weight (negative).
    - A judge "accept" verdict adds judge_accept weight (optionally scaled
      by the judge's own confidence).
    - Each passing guardrail adds guardrail_pass weight.
    - Each blocking guardrail failure adds guardrail_block penalty.
    - A base score of 0.5 is used as the starting point.

    Args:
        result: JanitorResult from janitor verification.
        weights: Weight configuration. Defaults to ConfidenceWeights().

    Returns:
        ConfidenceScore with the computed score and breakdown.
    """
    if weights is None:
        weights = ConfidenceWeights()

    breakdown: dict[str, float] = {}
    base = 0.5
    breakdown["base"] = base

    # Signal results
    signals_passed = sum(1 for _, passed, _ in result.signal_results if passed)
    signals_failed = sum(1 for _, passed, _ in result.signal_results if not passed)

    if result.signal_results:
        signal_score = signals_passed * weights.signal_pass + signals_failed * weights.signal_fail
        breakdown["signals"] = signal_score
    elif not result.signal_results:
        breakdown["no_signals"] = weights.no_signals_penalty

    # Judge verdict
    judge_accepted: bool | None = None
    judge_confidence_val: float | None = None
    if result.judge_verdict is not None:
        judge_accepted = result.judge_verdict.verdict == "accept"
        judge_confidence_val = result.judge_verdict.confidence
        if judge_accepted:
            jw = weights.judge_accept
            if weights.judge_confidence_mult:
                jw *= result.judge_verdict.confidence
            breakdown["judge"] = jw
        else:
            breakdown["judge"] = -weights.judge_accept

    # Guardrail results
    guardrails_passed = sum(1 for g in result.guardrail_results if g.passed)
    guardrails_blocked = sum(1 for g in result.guardrail_results if g.blocked)

    if result.guardrail_results:
        guardrail_score = guardrails_passed * weights.guardrail_pass + guardrails_blocked * weights.guardrail_block
        breakdown["guardrails"] = guardrail_score

    # Compute total
    total = sum(breakdown.values())
    score = max(0.0, min(1.0, total))

    confidence = ConfidenceScore(
        task_id=result.task_id,
        score=score,
        signals_passed=signals_passed,
        signals_failed=signals_failed,
        judge_accepted=judge_accepted,
        judge_confidence=judge_confidence_val,
        guardrails_passed=guardrails_passed,
        guardrails_blocked=guardrails_blocked,
        breakdown=breakdown,
    )

    logger.debug(
        "Confidence for %s: %.2f (signals=%d/%d, judge=%s, guardrails=%d/%d)",
        result.task_id,
        score,
        signals_passed,
        signals_passed + signals_failed,
        judge_accepted,
        guardrails_passed,
        guardrails_passed + guardrails_blocked,
    )
    return confidence


def compute_batch_confidence(
    results: list[JanitorResult],
    *,
    weights: ConfidenceWeights | None = None,
) -> list[ConfidenceScore]:
    """Compute confidence scores for a batch of janitor results.

    Args:
        results: List of JanitorResult from janitor verification.
        weights: Weight configuration. Defaults to ConfidenceWeights().

    Returns:
        List of ConfidenceScore for each result.
    """
    return [compute_confidence(r, weights=weights) for r in results]
