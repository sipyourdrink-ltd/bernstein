"""Self-healing orchestration with adjusted retries.

Diagnoses task failures, classifies them into ``FailureMode`` categories,
and produces a ``RetryConfig`` with adjusted model, effort, and compaction
settings so the orchestrator can re-attempt the task with a higher chance
of success.

Each failure mode has a pre-defined ``HealingAction`` that describes the
recovery strategy and the confidence the orchestrator should have in the
fix.  ``plan_healing`` combines the diagnosis with attempt-aware back-off
to produce a concrete ``RetryConfig`` (or ``None`` when retries are
exhausted).
"""

from __future__ import annotations

import logging
import re
import textwrap
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class FailureMode(StrEnum):
    """Categories of task failure the healer can diagnose."""

    CONTEXT_OVERFLOW = "context_overflow"
    QUALITY_GATE_FAILURE = "quality_gate_failure"
    MERGE_CONFLICT = "merge_conflict"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SPAWN_FAILURE = "spawn_failure"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealingAction:
    """Describes how to recover from a specific failure mode.

    Attributes:
        failure_mode: The failure category this action addresses.
        action: Human-readable description of the recovery strategy.
        adjustments: Key/value pairs describing parameter changes.
        confidence: Likelihood (0-1) that the action resolves the failure.
    """

    failure_mode: FailureMode
    action: str
    adjustments: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5


@dataclass(frozen=True)
class RetryConfig:
    """Concrete retry plan for a failed task.

    Attributes:
        task_id: The task to retry.
        original_model: Model used in the failed attempt.
        original_effort: Effort level used in the failed attempt.
        adjusted_model: Model to use for the retry.
        adjusted_effort: Effort level to use for the retry.
        compaction_level: How aggressively to compact context.
        additional_context: Extra instructions for the retried agent.
        max_retries: Maximum number of retry attempts allowed.
    """

    task_id: str
    original_model: str
    original_effort: str
    adjusted_model: str
    adjusted_effort: str
    compaction_level: Literal["none", "moderate", "aggressive"] = "none"
    additional_context: str = ""
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Healing strategies
# ---------------------------------------------------------------------------

#: Model downgrade order used by plan_healing.
_MODEL_DOWNGRADE: dict[str, str] = {
    "opus": "sonnet",
    "sonnet": "haiku",
}

#: Effort downgrade order used by plan_healing.
_EFFORT_DOWNGRADE: dict[str, str] = {
    "max": "high",
    "high": "medium",
    "medium": "low",
}

HEALING_STRATEGIES: dict[FailureMode, HealingAction] = {
    FailureMode.CONTEXT_OVERFLOW: HealingAction(
        failure_mode=FailureMode.CONTEXT_OVERFLOW,
        action="Compact context and retry with aggressive summarisation",
        adjustments={"compaction_level": "aggressive", "downgrade_model": True},
        confidence=0.8,
    ),
    FailureMode.QUALITY_GATE_FAILURE: HealingAction(
        failure_mode=FailureMode.QUALITY_GATE_FAILURE,
        action="Retry with higher-capability model and additional review hints",
        adjustments={"upgrade_model": True, "add_review_context": True},
        confidence=0.6,
    ),
    FailureMode.MERGE_CONFLICT: HealingAction(
        failure_mode=FailureMode.MERGE_CONFLICT,
        action="Re-pull base branch and retry with conflict resolution guidance",
        adjustments={"add_merge_guidance": True},
        confidence=0.7,
    ),
    FailureMode.RATE_LIMIT: HealingAction(
        failure_mode=FailureMode.RATE_LIMIT,
        action="Downgrade model to reduce rate-limit pressure",
        adjustments={"downgrade_model": True, "backoff": True},
        confidence=0.9,
    ),
    FailureMode.TIMEOUT: HealingAction(
        failure_mode=FailureMode.TIMEOUT,
        action="Reduce effort and compact context to finish within time budget",
        adjustments={"downgrade_effort": True, "compaction_level": "moderate"},
        confidence=0.7,
    ),
    FailureMode.SPAWN_FAILURE: HealingAction(
        failure_mode=FailureMode.SPAWN_FAILURE,
        action="Retry spawn with fallback model",
        adjustments={"downgrade_model": True},
        confidence=0.6,
    ),
    FailureMode.UNKNOWN: HealingAction(
        failure_mode=FailureMode.UNKNOWN,
        action="Retry with reduced effort and moderate compaction",
        adjustments={"downgrade_effort": True, "compaction_level": "moderate"},
        confidence=0.3,
    ),
}


# ---------------------------------------------------------------------------
# Diagnosis patterns
# ---------------------------------------------------------------------------

_CONTEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"context.*(length|window|limit)\s*(exceeded|overflow)", re.IGNORECASE),
    re.compile(r"max.?tokens?\s*(exceeded|reached)", re.IGNORECASE),
    re.compile(r"token\s*(budget|limit)\s*exceeded", re.IGNORECASE),
    re.compile(r"prompt\s+is\s+too\s+long", re.IGNORECASE),
    re.compile(r"context\s+overflow", re.IGNORECASE),
]

_QUALITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"quality.?gate\s*(fail|reject)", re.IGNORECASE),
    re.compile(r"lint(er|ing)?\s*(fail|error)", re.IGNORECASE),
    re.compile(r"test(s)?\s+(fail|error)", re.IGNORECASE),
    re.compile(r"type.?check\s*(fail|error)", re.IGNORECASE),
    re.compile(r"coverage\s+(below|under|insufficient)", re.IGNORECASE),
]

_MERGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"merge\s+conflict", re.IGNORECASE),
    re.compile(r"conflict(s)?\s+(in|with|during)", re.IGNORECASE),
    re.compile(r"CONFLICT\s*\(", re.IGNORECASE),
    re.compile(r"cannot\s+merge", re.IGNORECASE),
]

_RATE_LIMIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"quota\s+exceeded", re.IGNORECASE),
]

_TIMEOUT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"timed?\s*out", re.IGNORECASE),
    re.compile(r"deadline\s+exceeded", re.IGNORECASE),
]

_SPAWN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"spawn\s*(fail|error)", re.IGNORECASE),
    re.compile(r"failed\s+to\s+start", re.IGNORECASE),
    re.compile(r"agent\s+(not\s+found|unavailable)", re.IGNORECASE),
    re.compile(r"adapter\s+(not\s+found|missing)", re.IGNORECASE),
    re.compile(r"command\s+not\s+found", re.IGNORECASE),
]

#: Exit codes that hint at specific failure modes.
_EXIT_CODE_HINTS: dict[int, FailureMode] = {
    124: FailureMode.TIMEOUT,  # GNU timeout exit code
    137: FailureMode.TIMEOUT,  # SIGKILL (often OOM or timeout)
    143: FailureMode.TIMEOUT,  # SIGTERM
    127: FailureMode.SPAWN_FAILURE,  # command not found
    126: FailureMode.SPAWN_FAILURE,  # permission denied on exec
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diagnose_failure(error_message: str, exit_code: int | None = None) -> FailureMode:
    """Classify an error message into a failure mode.

    Checks regex patterns in priority order against the error message,
    then falls back to exit-code heuristics.

    Args:
        error_message: The error text from the failed agent/task.
        exit_code: Optional process exit code.

    Returns:
        The diagnosed ``FailureMode``.
    """
    pattern_groups: list[tuple[list[re.Pattern[str]], FailureMode]] = [
        (_CONTEXT_PATTERNS, FailureMode.CONTEXT_OVERFLOW),
        (_MERGE_PATTERNS, FailureMode.MERGE_CONFLICT),
        (_RATE_LIMIT_PATTERNS, FailureMode.RATE_LIMIT),
        (_TIMEOUT_PATTERNS, FailureMode.TIMEOUT),
        (_SPAWN_PATTERNS, FailureMode.SPAWN_FAILURE),
        (_QUALITY_PATTERNS, FailureMode.QUALITY_GATE_FAILURE),
    ]

    for patterns, mode in pattern_groups:
        for pattern in patterns:
            if pattern.search(error_message):
                logger.debug("Diagnosed failure mode %s via pattern %r", mode, pattern.pattern)
                return mode

    if exit_code is not None and exit_code in _EXIT_CODE_HINTS:
        mode = _EXIT_CODE_HINTS[exit_code]
        logger.debug("Diagnosed failure mode %s via exit code %d", mode, exit_code)
        return mode

    logger.debug("Could not diagnose failure, returning UNKNOWN")
    return FailureMode.UNKNOWN


def _resolve_compaction_level(
    adjustments: dict[str, Any],
    attempt: int,
) -> Literal["none", "moderate", "aggressive"]:
    """Determine compaction level from adjustments and attempt number."""
    compaction_raw = adjustments.get("compaction_level", "none")
    level: Literal["none", "moderate", "aggressive"] = (
        compaction_raw if compaction_raw in ("none", "moderate", "aggressive") else "none"
    )
    if attempt >= 3 and level != "aggressive":
        return "aggressive"
    if attempt >= 2 and level == "none":
        return "moderate"
    return level


def plan_healing(
    task_id: str,
    failure: FailureMode,
    current_model: str,
    current_effort: str,
    attempt: int,
) -> RetryConfig | None:
    """Produce a retry plan for a failed task.

    Returns ``None`` when the maximum number of retries (3) has been
    reached, or when the failure mode has no healing strategy.

    Args:
        task_id: Identifier of the failed task.
        failure: The diagnosed failure mode.
        current_model: Model used in the failed attempt.
        current_effort: Effort used in the failed attempt.
        attempt: The attempt number (1-based; 1 = first failure).

    Returns:
        A ``RetryConfig`` with adjusted parameters, or ``None``.
    """
    strategy = HEALING_STRATEGIES.get(failure)
    if strategy is None:
        logger.warning("No healing strategy for failure mode %s", failure)
        return None

    max_retries = 3
    if attempt > max_retries:
        logger.info(
            "Task %s exceeded max retries (%d), giving up",
            task_id,
            max_retries,
        )
        return None

    adjustments = strategy.adjustments

    # -- model adjustment ------------------------------------------------
    adjusted_model = current_model
    if adjustments.get("downgrade_model"):
        adjusted_model = _MODEL_DOWNGRADE.get(current_model, current_model)
    elif adjustments.get("upgrade_model"):
        # Reverse lookup: find a model that downgrades *to* current_model
        for higher, lower in _MODEL_DOWNGRADE.items():
            if lower == current_model:
                adjusted_model = higher
                break

    # -- effort adjustment -----------------------------------------------
    adjusted_effort = current_effort
    if adjustments.get("downgrade_effort"):
        adjusted_effort = _EFFORT_DOWNGRADE.get(current_effort, current_effort)

    # -- compaction ------------------------------------------------------
    compaction_level = _resolve_compaction_level(adjustments, attempt)

    # -- additional context ---------------------------------------------
    context_parts: list[str] = []
    if adjustments.get("add_review_context"):
        context_parts.append(
            "Previous attempt failed quality gates. Pay extra attention to linting, type-checking, and test coverage."
        )
    if adjustments.get("add_merge_guidance"):
        context_parts.append(
            "Previous attempt hit merge conflicts. Pull latest changes from the base branch before making edits."
        )
    additional_context = " ".join(context_parts)

    config = RetryConfig(
        task_id=task_id,
        original_model=current_model,
        original_effort=current_effort,
        adjusted_model=adjusted_model,
        adjusted_effort=adjusted_effort,
        compaction_level=compaction_level,
        additional_context=additional_context,
        max_retries=max_retries,
    )

    logger.info(
        "Healing plan for task %s (attempt %d): %s -> %s/%s, compaction=%s",
        task_id,
        attempt,
        failure,
        adjusted_model,
        adjusted_effort,
        compaction_level,
    )

    return config


def format_healing_plan(config: RetryConfig) -> str:
    """Format a ``RetryConfig`` as a human-readable summary.

    Args:
        config: The retry configuration to format.

    Returns:
        A multi-line string describing the healing plan.
    """
    lines = [
        f"Healing plan for task {config.task_id}",
        f"  Model:      {config.original_model} -> {config.adjusted_model}",
        f"  Effort:     {config.original_effort} -> {config.adjusted_effort}",
        f"  Compaction: {config.compaction_level}",
        f"  Max retries: {config.max_retries}",
    ]
    if config.additional_context:
        wrapped = textwrap.fill(config.additional_context, width=60)
        lines.append(f"  Context:    {wrapped}")
    return "\n".join(lines)
