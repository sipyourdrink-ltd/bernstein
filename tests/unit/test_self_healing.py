"""Tests for self-healing orchestration."""

from __future__ import annotations

import pytest
from bernstein.core.self_healing import (
    HEALING_STRATEGIES,
    FailureMode,
    HealingAction,
    RetryConfig,
    diagnose_failure,
    format_healing_plan,
    plan_healing,
)

# ---------------------------------------------------------------------------
# FailureMode enum
# ---------------------------------------------------------------------------


class TestFailureMode:
    """FailureMode enum membership."""

    def test_all_modes_present(self) -> None:
        """Every expected mode exists."""
        expected = {
            "context_overflow",
            "quality_gate_failure",
            "merge_conflict",
            "rate_limit",
            "timeout",
            "spawn_failure",
            "unknown",
        }
        assert {m.value for m in FailureMode} == expected

    def test_string_value(self) -> None:
        """StrEnum values are plain strings."""
        assert FailureMode.RATE_LIMIT == "rate_limit"


# ---------------------------------------------------------------------------
# HealingAction dataclass
# ---------------------------------------------------------------------------


class TestHealingAction:
    """HealingAction frozen dataclass."""

    def test_frozen(self) -> None:
        """HealingAction instances are immutable."""
        action = HealingAction(
            failure_mode=FailureMode.TIMEOUT,
            action="reduce effort",
        )
        try:
            action.confidence = 0.99  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass  # expected

    def test_defaults(self) -> None:
        """Default adjustments and confidence."""
        action = HealingAction(
            failure_mode=FailureMode.UNKNOWN,
            action="retry",
        )
        assert action.adjustments == {}
        assert action.confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# RetryConfig dataclass
# ---------------------------------------------------------------------------


class TestRetryConfig:
    """RetryConfig frozen dataclass."""

    def test_frozen(self) -> None:
        """RetryConfig instances are immutable."""
        cfg = RetryConfig(
            task_id="t1",
            original_model="opus",
            original_effort="max",
            adjusted_model="sonnet",
            adjusted_effort="high",
        )
        try:
            cfg.max_retries = 10  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass  # expected

    def test_defaults(self) -> None:
        """Default compaction, context, and max_retries."""
        cfg = RetryConfig(
            task_id="t1",
            original_model="opus",
            original_effort="max",
            adjusted_model="sonnet",
            adjusted_effort="high",
        )
        assert cfg.compaction_level == "none"
        assert cfg.additional_context == ""
        assert cfg.max_retries == 3


# ---------------------------------------------------------------------------
# HEALING_STRATEGIES
# ---------------------------------------------------------------------------


class TestHealingStrategies:
    """HEALING_STRATEGIES dict coverage."""

    def test_one_strategy_per_mode(self) -> None:
        """Every FailureMode has a strategy."""
        for mode in FailureMode:
            assert mode in HEALING_STRATEGIES, f"Missing strategy for {mode}"

    def test_strategy_types(self) -> None:
        """All values are HealingAction instances."""
        for mode, action in HEALING_STRATEGIES.items():
            assert isinstance(action, HealingAction)
            assert action.failure_mode == mode

    def test_confidence_range(self) -> None:
        """All confidence values are between 0 and 1."""
        for action in HEALING_STRATEGIES.values():
            assert 0.0 <= action.confidence <= 1.0


# ---------------------------------------------------------------------------
# diagnose_failure
# ---------------------------------------------------------------------------


class TestDiagnoseFailure:
    """diagnose_failure pattern matching."""

    def test_context_overflow(self) -> None:
        """Detects context overflow errors."""
        assert diagnose_failure("context length exceeded") == FailureMode.CONTEXT_OVERFLOW

    def test_context_window_exceeded(self) -> None:
        """Detects context window limit."""
        assert diagnose_failure("context window limit exceeded") == FailureMode.CONTEXT_OVERFLOW

    def test_max_tokens(self) -> None:
        """Detects max tokens exceeded."""
        assert diagnose_failure("max_tokens exceeded") == FailureMode.CONTEXT_OVERFLOW

    def test_prompt_too_long(self) -> None:
        """Detects prompt too long."""
        assert diagnose_failure("prompt is too long") == FailureMode.CONTEXT_OVERFLOW

    def test_quality_gate_failure(self) -> None:
        """Detects quality gate failures."""
        assert diagnose_failure("quality_gate failed") == FailureMode.QUALITY_GATE_FAILURE

    def test_lint_failure(self) -> None:
        """Detects linting errors."""
        assert diagnose_failure("linter failed on 3 files") == FailureMode.QUALITY_GATE_FAILURE

    def test_test_failure(self) -> None:
        """Detects test failures."""
        assert diagnose_failure("tests failed: 2 errors") == FailureMode.QUALITY_GATE_FAILURE

    def test_merge_conflict(self) -> None:
        """Detects merge conflicts."""
        assert diagnose_failure("CONFLICT (content): merge conflict in foo.py") == FailureMode.MERGE_CONFLICT

    def test_cannot_merge(self) -> None:
        """Detects cannot merge."""
        assert diagnose_failure("cannot merge: divergent branches") == FailureMode.MERGE_CONFLICT

    def test_rate_limit(self) -> None:
        """Detects rate limits."""
        assert diagnose_failure("rate limit exceeded") == FailureMode.RATE_LIMIT

    def test_429(self) -> None:
        """Detects HTTP 429."""
        assert diagnose_failure("HTTP 429 Too Many Requests") == FailureMode.RATE_LIMIT

    def test_overloaded(self) -> None:
        """Detects overloaded errors."""
        assert diagnose_failure("API is overloaded") == FailureMode.RATE_LIMIT

    def test_timeout(self) -> None:
        """Detects timeout errors."""
        assert diagnose_failure("connection timed out") == FailureMode.TIMEOUT

    def test_deadline_exceeded(self) -> None:
        """Detects deadline exceeded."""
        assert diagnose_failure("deadline exceeded") == FailureMode.TIMEOUT

    def test_spawn_failure(self) -> None:
        """Detects spawn failures."""
        assert diagnose_failure("spawn failed: adapter not available") == FailureMode.SPAWN_FAILURE

    def test_command_not_found(self) -> None:
        """Detects command not found."""
        assert diagnose_failure("command not found: claude") == FailureMode.SPAWN_FAILURE

    def test_exit_code_124_timeout(self) -> None:
        """Exit code 124 maps to TIMEOUT."""
        assert diagnose_failure("process exited", exit_code=124) == FailureMode.TIMEOUT

    def test_exit_code_127_spawn(self) -> None:
        """Exit code 127 maps to SPAWN_FAILURE."""
        assert diagnose_failure("process exited", exit_code=127) == FailureMode.SPAWN_FAILURE

    def test_unknown_message(self) -> None:
        """Unrecognized error returns UNKNOWN."""
        assert diagnose_failure("something went horribly wrong") == FailureMode.UNKNOWN

    def test_unknown_exit_code(self) -> None:
        """Unrecognized exit code returns UNKNOWN."""
        assert diagnose_failure("process exited", exit_code=42) == FailureMode.UNKNOWN


# ---------------------------------------------------------------------------
# plan_healing
# ---------------------------------------------------------------------------


class TestPlanHealing:
    """plan_healing retry plan generation."""

    def test_returns_retry_config(self) -> None:
        """Returns a RetryConfig for valid input."""
        result = plan_healing("t1", FailureMode.RATE_LIMIT, "opus", "max", attempt=1)

        assert result is not None
        assert isinstance(result, RetryConfig)
        assert result.task_id == "t1"

    def test_rate_limit_downgrades_model(self) -> None:
        """Rate limit healing downgrades model."""
        result = plan_healing("t1", FailureMode.RATE_LIMIT, "opus", "max", attempt=1)

        assert result is not None
        assert result.adjusted_model == "sonnet"

    def test_context_overflow_aggressive_compaction(self) -> None:
        """Context overflow uses aggressive compaction."""
        result = plan_healing("t1", FailureMode.CONTEXT_OVERFLOW, "opus", "high", attempt=1)

        assert result is not None
        assert result.compaction_level == "aggressive"

    def test_timeout_downgrades_effort(self) -> None:
        """Timeout healing downgrades effort."""
        result = plan_healing("t1", FailureMode.TIMEOUT, "sonnet", "high", attempt=1)

        assert result is not None
        assert result.adjusted_effort == "medium"

    def test_quality_gate_upgrades_model(self) -> None:
        """Quality gate failure upgrades model."""
        result = plan_healing("t1", FailureMode.QUALITY_GATE_FAILURE, "sonnet", "high", attempt=1)

        assert result is not None
        assert result.adjusted_model == "opus"

    def test_quality_gate_adds_review_context(self) -> None:
        """Quality gate adds review hints."""
        result = plan_healing("t1", FailureMode.QUALITY_GATE_FAILURE, "sonnet", "high", attempt=1)

        assert result is not None
        assert "quality gates" in result.additional_context.lower()

    def test_merge_conflict_adds_guidance(self) -> None:
        """Merge conflict adds merge guidance."""
        result = plan_healing("t1", FailureMode.MERGE_CONFLICT, "sonnet", "high", attempt=1)

        assert result is not None
        assert "merge" in result.additional_context.lower()

    def test_exceeds_max_retries(self) -> None:
        """Returns None when max retries exceeded."""
        result = plan_healing("t1", FailureMode.TIMEOUT, "sonnet", "high", attempt=4)

        assert result is None

    def test_exactly_at_max_retries(self) -> None:
        """Attempt 3 still returns a config."""
        result = plan_healing("t1", FailureMode.TIMEOUT, "sonnet", "high", attempt=3)

        assert result is not None

    def test_compaction_escalates_on_attempt_2(self) -> None:
        """Compaction escalates to moderate on second attempt."""
        result = plan_healing("t1", FailureMode.UNKNOWN, "sonnet", "high", attempt=2)

        assert result is not None
        assert result.compaction_level in ("moderate", "aggressive")

    def test_compaction_escalates_on_attempt_3(self) -> None:
        """Compaction escalates to aggressive on third attempt."""
        result = plan_healing("t1", FailureMode.SPAWN_FAILURE, "opus", "high", attempt=3)

        assert result is not None
        assert result.compaction_level == "aggressive"

    def test_preserves_original_values(self) -> None:
        """Original model and effort are preserved in config."""
        result = plan_healing("t1", FailureMode.RATE_LIMIT, "opus", "max", attempt=1)

        assert result is not None
        assert result.original_model == "opus"
        assert result.original_effort == "max"

    def test_no_downgrade_past_cheapest(self) -> None:
        """Model stays the same when already cheapest."""
        result = plan_healing("t1", FailureMode.RATE_LIMIT, "haiku", "low", attempt=1)

        assert result is not None
        assert result.adjusted_model == "haiku"

    def test_max_retries_field(self) -> None:
        """RetryConfig has max_retries=3."""
        result = plan_healing("t1", FailureMode.TIMEOUT, "sonnet", "high", attempt=1)

        assert result is not None
        assert result.max_retries == 3


# ---------------------------------------------------------------------------
# format_healing_plan
# ---------------------------------------------------------------------------


class TestFormatHealingPlan:
    """format_healing_plan formatting."""

    def test_basic_format(self) -> None:
        """Produces a multi-line human-readable string."""
        cfg = RetryConfig(
            task_id="t1",
            original_model="opus",
            original_effort="max",
            adjusted_model="sonnet",
            adjusted_effort="high",
            compaction_level="moderate",
        )

        text = format_healing_plan(cfg)

        assert "t1" in text
        assert "opus" in text
        assert "sonnet" in text
        assert "moderate" in text

    def test_includes_context(self) -> None:
        """Additional context appears in output."""
        cfg = RetryConfig(
            task_id="t2",
            original_model="sonnet",
            original_effort="high",
            adjusted_model="opus",
            adjusted_effort="high",
            additional_context="Pay attention to linting.",
        )

        text = format_healing_plan(cfg)

        assert "linting" in text

    def test_no_context_line_when_empty(self) -> None:
        """No Context line when additional_context is empty."""
        cfg = RetryConfig(
            task_id="t3",
            original_model="sonnet",
            original_effort="high",
            adjusted_model="sonnet",
            adjusted_effort="medium",
        )

        text = format_healing_plan(cfg)

        assert "Context:" not in text

    def test_max_retries_in_output(self) -> None:
        """Max retries value appears in the formatted output."""
        cfg = RetryConfig(
            task_id="t4",
            original_model="opus",
            original_effort="max",
            adjusted_model="sonnet",
            adjusted_effort="high",
        )

        text = format_healing_plan(cfg)

        assert "3" in text
