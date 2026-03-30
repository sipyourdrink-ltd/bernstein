"""Tests for bernstein.core.batch_router — batch API routing for non-urgent tasks."""

from __future__ import annotations

import pytest

from bernstein.core.batch_router import (
    BATCH_DISCOUNT_FACTOR,
    BatchClassification,
    BatchMode,
    apply_batch_discount,
    classify_batch_mode,
)
from bernstein.core.models import Complexity, Scope, Task, TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(**kwargs: object) -> Task:
    """Build a minimal Task with sensible defaults for testing."""
    defaults: dict[str, object] = {
        "id": "test-id",
        "title": "Some task",
        "description": "Some description",
        "role": "backend",
        "priority": 2,
        "scope": Scope.SMALL,
        "complexity": Complexity.MEDIUM,
        "task_type": TaskType.STANDARD,
        "model": None,
        "batch_eligible": None,  # None = auto-detect; tests that want explicit can pass True/False
    }
    defaults.update(kwargs)
    return Task(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BATCH_DISCOUNT_FACTOR constant
# ---------------------------------------------------------------------------


class TestBatchDiscountFactor:
    def test_discount_is_50_percent(self) -> None:
        assert BATCH_DISCOUNT_FACTOR == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# classify_batch_mode — hard REALTIME gates
# ---------------------------------------------------------------------------


class TestClassifyBatchModeRealtimeGates:
    @pytest.mark.parametrize("role", ["manager", "architect", "security", "orchestrator"])
    def test_realtime_roles_are_not_batch_eligible(self, role: str) -> None:
        task = _task(role=role, complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert role in result.reason

    def test_critical_priority_1_is_realtime(self) -> None:
        task = _task(priority=1, complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert "critical" in result.reason.lower()

    def test_large_scope_is_realtime(self) -> None:
        task = _task(scope=Scope.LARGE, complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert "large" in result.reason.lower()

    def test_high_complexity_is_realtime(self) -> None:
        task = _task(complexity=Complexity.HIGH)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert "high" in result.reason.lower()

    def test_research_task_type_is_realtime(self) -> None:
        task = _task(task_type=TaskType.RESEARCH, complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0

    def test_upgrade_proposal_task_type_is_realtime(self) -> None:
        task = _task(task_type=TaskType.UPGRADE_PROPOSAL, complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0

    def test_opus_model_override_is_realtime(self) -> None:
        task = _task(model="opus", complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert "opus" in result.reason.lower()

    def test_explicit_batch_eligible_false_is_realtime(self) -> None:
        task = _task(batch_eligible=False, complexity=Complexity.LOW)
        # LOW complexity would normally batch, but explicit False overrides
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0
        assert "False" in result.reason


# ---------------------------------------------------------------------------
# classify_batch_mode — BATCH paths
# ---------------------------------------------------------------------------


class TestClassifyBatchModeBatchPaths:
    def test_explicit_batch_eligible_true_routes_to_batch(self) -> None:
        task = _task(batch_eligible=True, complexity=Complexity.MEDIUM)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.BATCH
        assert result.discount_factor == pytest.approx(BATCH_DISCOUNT_FACTOR)
        assert "explicit" in result.reason.lower()

    def test_low_complexity_auto_routes_to_batch(self) -> None:
        # batch_eligible=None means "auto-detect" — LOW complexity should qualify
        task = _task(complexity=Complexity.LOW)  # uses default batch_eligible=None
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.BATCH
        assert result.discount_factor == pytest.approx(BATCH_DISCOUNT_FACTOR)
        assert "LOW" in result.reason

    @pytest.mark.parametrize(
        "title",
        [
            "Update docs for new API",
            "Add docstring to helpers",
            "Fix formatting issues",
            "Run ruff style check",
            "Add unit test for auth module",
            "Write documentation",
            "Update changelog entry",
            "Bump version to 1.2.3",
        ],
    )
    def test_batch_keyword_in_title_routes_to_batch(self, title: str) -> None:
        task = _task(title=title, complexity=Complexity.MEDIUM)
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.BATCH
        assert result.discount_factor == pytest.approx(BATCH_DISCOUNT_FACTOR)
        assert "keyword" in result.reason.lower()

    def test_batch_keyword_in_description_routes_to_batch(self) -> None:
        task = _task(
            title="Misc work",
            description="Please update the documentation for the module",
            complexity=Complexity.MEDIUM,
        )
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.BATCH

    def test_discount_factor_is_50_percent_for_batch(self) -> None:
        task = _task(complexity=Complexity.LOW)
        result = classify_batch_mode(task)
        assert result.discount_factor == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# classify_batch_mode — default REALTIME
# ---------------------------------------------------------------------------


class TestClassifyBatchModeDefault:
    def test_medium_complexity_no_keywords_defaults_to_realtime(self) -> None:
        task = _task(
            title="Implement payment gateway integration",
            description="Wire up Stripe",
            complexity=Complexity.MEDIUM,
            role="backend",
        )
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME
        assert result.discount_factor == 1.0

    def test_standard_task_no_batch_criteria_is_realtime(self) -> None:
        task = _task(
            title="Add OAuth login flow",
            description="Support Google and GitHub",
            complexity=Complexity.MEDIUM,
            task_type=TaskType.STANDARD,
        )
        result = classify_batch_mode(task)
        assert result.mode == BatchMode.REALTIME


# ---------------------------------------------------------------------------
# apply_batch_discount
# ---------------------------------------------------------------------------


class TestApplyBatchDiscount:
    def test_batch_classification_halves_cost(self) -> None:
        classification = BatchClassification(
            mode=BatchMode.BATCH,
            reason="test",
            discount_factor=0.50,
        )
        discounted = apply_batch_discount(1.00, classification)
        assert discounted == pytest.approx(0.50)

    def test_realtime_classification_leaves_cost_unchanged(self) -> None:
        classification = BatchClassification(
            mode=BatchMode.REALTIME,
            reason="test",
            discount_factor=1.0,
        )
        discounted = apply_batch_discount(1.00, classification)
        assert discounted == pytest.approx(1.00)

    def test_zero_cost_stays_zero(self) -> None:
        classification = BatchClassification(
            mode=BatchMode.BATCH,
            reason="test",
            discount_factor=0.50,
        )
        assert apply_batch_discount(0.0, classification) == pytest.approx(0.0)

    def test_discount_scales_with_cost(self) -> None:
        classification = BatchClassification(
            mode=BatchMode.BATCH,
            reason="test",
            discount_factor=BATCH_DISCOUNT_FACTOR,
        )
        # $0.04 real-time → $0.02 batch
        assert apply_batch_discount(0.04, classification) == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# BatchClassification dataclass
# ---------------------------------------------------------------------------


class TestBatchClassification:
    def test_is_frozen(self) -> None:
        c = BatchClassification(mode=BatchMode.BATCH, reason="ok", discount_factor=0.5)
        with pytest.raises((AttributeError, TypeError)):
            c.mode = BatchMode.REALTIME  # type: ignore[misc]

    def test_fields_accessible(self) -> None:
        c = BatchClassification(mode=BatchMode.REALTIME, reason="test reason", discount_factor=1.0)
        assert c.mode == BatchMode.REALTIME
        assert c.reason == "test reason"
        assert c.discount_factor == 1.0
