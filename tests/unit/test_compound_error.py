"""Tests for compound error rate tracking."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bernstein.core.observability.compound_error import (
    CompoundErrorTracker,
    StepOutcome,
)


def _outcome(
    *,
    success: bool = True,
    model: str = "claude-sonnet",
    role: str = "backend",
    step_count: int = 5,
    task_id: str = "t1",
    complexity: str = "medium",
    timestamp: float = 1000.0,
) -> StepOutcome:
    return StepOutcome(
        task_id=task_id,
        model=model,
        role=role,
        complexity=complexity,
        step_count=step_count,
        success=success,
        timestamp=timestamp,
    )


class TestPerStepSuccessRate:
    def test_empty_tracker(self) -> None:
        tracker = CompoundErrorTracker()
        assert tracker.per_step_success_rate() == 0.0

    def test_all_success(self) -> None:
        tracker = CompoundErrorTracker()
        for _ in range(5):
            tracker.record(_outcome(success=True))
        assert tracker.per_step_success_rate() == 1.0

    def test_known_rate(self) -> None:
        tracker = CompoundErrorTracker()
        for i in range(20):
            tracker.record(_outcome(success=(i < 17)))
        assert tracker.per_step_success_rate() == pytest.approx(0.85, abs=0.01)


class TestCompoundSuccessRate:
    def test_empty_tracker(self) -> None:
        tracker = CompoundErrorTracker()
        assert tracker.compound_success_rate() == 0.0

    def test_85_pct_10_steps(self) -> None:
        """85% per step over 10 steps -> ~19.7%."""
        tracker = CompoundErrorTracker()
        for i in range(20):
            tracker.record(_outcome(success=(i < 17), step_count=10))
        result = tracker.compound_success_rate()
        assert result == pytest.approx(0.85**10, abs=0.01)

    def test_explicit_steps_override(self) -> None:
        tracker = CompoundErrorTracker()
        for i in range(10):
            tracker.record(_outcome(success=(i < 9), step_count=5))
        # Override step count
        result = tracker.compound_success_rate(steps=3)
        expected = (9 / 10) ** 3
        assert result == pytest.approx(expected, abs=0.01)

    def test_perfect_rate(self) -> None:
        tracker = CompoundErrorTracker()
        for _ in range(5):
            tracker.record(_outcome(success=True, step_count=10))
        assert tracker.compound_success_rate() == pytest.approx(1.0)


class TestAvgStepCount:
    def test_empty_tracker(self) -> None:
        tracker = CompoundErrorTracker()
        assert tracker.avg_step_count() == 1.0

    def test_known_average(self) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome(step_count=4))
        tracker.record(_outcome(step_count=6))
        assert tracker.avg_step_count() == pytest.approx(5.0)

    def test_zero_step_count_excluded(self) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome(step_count=0))
        tracker.record(_outcome(step_count=10))
        assert tracker.avg_step_count() == pytest.approx(10.0)


class TestSuccessRateByModel:
    def test_grouping(self) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome(model="sonnet", success=True))
        tracker.record(_outcome(model="sonnet", success=False))
        tracker.record(_outcome(model="haiku", success=True))
        tracker.record(_outcome(model="haiku", success=True))
        rates = tracker.success_rate_by_model()
        assert rates["sonnet"] == pytest.approx(0.5)
        assert rates["haiku"] == pytest.approx(1.0)

    def test_empty(self) -> None:
        tracker = CompoundErrorTracker()
        assert tracker.success_rate_by_model() == {}


class TestShouldEscalateModel:
    def test_low_compound_rate_triggers_escalation(self) -> None:
        tracker = CompoundErrorTracker()
        # 3 outcomes, 1/3 success rate, step_count=5 -> compound = 0.33^5 ~ 0.004
        for i in range(3):
            tracker.record(_outcome(model="weak-model", success=(i == 0), step_count=5))
        assert tracker.should_escalate_model("weak-model") is True

    def test_high_compound_rate_no_escalation(self) -> None:
        tracker = CompoundErrorTracker()
        for _ in range(5):
            tracker.record(_outcome(model="strong-model", success=True, step_count=1))
        assert tracker.should_escalate_model("strong-model") is False

    def test_insufficient_data(self) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome(model="new-model", success=False, step_count=10))
        assert tracker.should_escalate_model("new-model") is False


class TestSaveLoad:
    def test_roundtrip(self, tmp_path: Path) -> None:
        tracker = CompoundErrorTracker(_alert_threshold=0.3)
        tracker.record(_outcome(task_id="a", model="sonnet", step_count=3, success=True, timestamp=100.0))
        tracker.record(_outcome(task_id="b", model="haiku", step_count=7, success=False, timestamp=200.0))

        path = tmp_path / "outcomes.json"
        tracker.save(path)

        loaded = CompoundErrorTracker.load(path, alert_threshold=0.3)
        assert len(loaded.outcomes) == 2
        assert loaded.outcomes[0].task_id == "a"
        assert loaded.outcomes[1].success is False
        assert loaded.per_step_success_rate() == tracker.per_step_success_rate()

    def test_load_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        loaded = CompoundErrorTracker.load(path)
        assert len(loaded.outcomes) == 0

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome())
        path = tmp_path / "deep" / "nested" / "outcomes.json"
        tracker.save(path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1


class TestAlertLogging:
    def test_alert_when_below_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = CompoundErrorTracker(_alert_threshold=0.8)
        # Record 4 outcomes first (no alert below 5)
        for _ in range(4):
            tracker.record(_outcome(success=True, step_count=1))

        # 5th outcome brings total to 5, with 80% success and step_count=5
        # compound = 0.8^5 ~ 0.328 < 0.8 threshold
        with caplog.at_level(logging.WARNING):
            tracker.record(_outcome(success=False, step_count=5))

        assert any("Compound success rate" in msg for msg in caplog.messages)

    def test_no_alert_above_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = CompoundErrorTracker(_alert_threshold=0.1)
        for _ in range(5):
            tracker.record(_outcome(success=True, step_count=1))
        with caplog.at_level(logging.WARNING):
            tracker.record(_outcome(success=True, step_count=1))
        assert not any("Compound success rate" in msg for msg in caplog.messages)


class TestToSummary:
    def test_summary_structure(self) -> None:
        tracker = CompoundErrorTracker()
        tracker.record(_outcome(model="sonnet", success=True, step_count=3))
        tracker.record(_outcome(model="sonnet", success=False, step_count=5))
        summary = tracker.to_summary()
        assert summary["total_outcomes"] == 2
        assert "per_step_success_rate" in summary
        assert "avg_step_count" in summary
        assert "compound_success_rate" in summary
        assert "sonnet" in summary["by_model"]
        assert "success_rate" in summary["by_model"]["sonnet"]
        assert "avg_steps" in summary["by_model"]["sonnet"]

    def test_empty_summary(self) -> None:
        tracker = CompoundErrorTracker()
        summary = tracker.to_summary()
        assert summary["total_outcomes"] == 0
        assert summary["per_step_success_rate"] == 0.0
