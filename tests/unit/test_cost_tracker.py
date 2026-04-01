"""Tests for bernstein.core.cost_tracker — per-run cost budget tracker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.cost_tracker import (
    BudgetStatus,
    CostTracker,
    TokenUsage,
    estimate_cost,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_known_model_sonnet(self) -> None:
        cost = estimate_cost("sonnet", input_tokens=1000, output_tokens=1000)
        # sonnet = 0.009 per 1k, 2k tokens total => 0.018
        assert cost == pytest.approx(0.018, abs=1e-6)

    def test_known_model_opus(self) -> None:
        cost = estimate_cost("opus", input_tokens=500, output_tokens=500)
        # opus = 0.015 per 1k, 1k tokens total => 0.015
        assert cost == pytest.approx(0.015, abs=1e-6)

    def test_known_model_haiku(self) -> None:
        cost = estimate_cost("haiku", input_tokens=10000, output_tokens=0)
        # haiku = 0.003 per 1k, 10k tokens => 0.03
        assert cost == pytest.approx(0.03, abs=1e-6)

    def test_unknown_model_uses_fallback(self) -> None:
        cost = estimate_cost("unknown-model-xyz", input_tokens=1000, output_tokens=0)
        # fallback = 0.005 per 1k, 1k tokens => 0.005
        assert cost == pytest.approx(0.005, abs=1e-6)

    def test_zero_tokens(self) -> None:
        cost = estimate_cost("sonnet", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_case_insensitive(self) -> None:
        cost = estimate_cost("Claude-Sonnet-3.5", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.009, abs=1e-6)


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_roundtrip_serialisation(self) -> None:
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=200,
            model="sonnet",
            cost_usd=0.001,
            agent_id="agent-abc",
            task_id="task-1",
            timestamp=1000.0,
        )
        d = usage.to_dict()
        restored = TokenUsage.from_dict(d)
        assert restored.input_tokens == 100
        assert restored.output_tokens == 200
        assert restored.model == "sonnet"
        assert restored.cost_usd == pytest.approx(0.001)
        assert restored.agent_id == "agent-abc"
        assert restored.task_id == "task-1"
        assert restored.timestamp == 1000.0


# ---------------------------------------------------------------------------
# BudgetStatus
# ---------------------------------------------------------------------------


class TestBudgetStatus:
    def test_to_dict(self) -> None:
        status = BudgetStatus(
            run_id="run-1",
            budget_usd=10.0,
            spent_usd=5.0,
            remaining_usd=5.0,
            percentage_used=0.5,
            should_warn=False,
            should_stop=False,
        )
        d = status.to_dict()
        assert d["run_id"] == "run-1"
        assert d["budget_usd"] == 10.0
        assert d["spent_usd"] == 5.0
        assert d["remaining_usd"] == 5.0
        assert d["percentage_used"] == 0.5
        assert d["should_warn"] is False
        assert d["should_stop"] is False


# ---------------------------------------------------------------------------
# CostTracker — recording and status
# ---------------------------------------------------------------------------


class TestCostTrackerRecording:
    def test_record_updates_spent(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=1000,
            output_tokens=1000,
        )
        assert tracker.spent_usd == pytest.approx(0.018, abs=1e-6)

    def test_record_with_explicit_cost(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=1.50,
        )
        assert tracker.spent_usd == pytest.approx(1.50)

    def test_multiple_records_accumulate(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=1.0)
        tracker.record(agent_id="a2", task_id="t2", model="opus", input_tokens=0, output_tokens=0, cost_usd=2.0)
        assert tracker.spent_usd == pytest.approx(3.0)
        assert len(tracker.usages) == 2

    def test_usages_returns_copy(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=1.0)
        usages = tracker.usages
        usages.clear()  # mutate the copy
        assert len(tracker.usages) == 1  # original unchanged

    def test_record_cumulative_is_delta_safe(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        first = tracker.record_cumulative(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=0.9,
        )
        second = tracker.record_cumulative(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=0.9,
        )
        assert first > 0.0
        assert second == 0.0
        assert len(tracker.usages) == 1

    def test_spent_helpers_track_agent_and_model(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=0.7)
        tracker.record(agent_id="a2", task_id="t2", model="opus", input_tokens=0, output_tokens=0, cost_usd=0.3)
        assert tracker.spent_for_agent("a1") == pytest.approx(0.7)
        by_model = tracker.spent_by_model()
        assert by_model["sonnet"] == pytest.approx(0.7)
        assert by_model["opus"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# CostTracker — unlimited budget
# ---------------------------------------------------------------------------


class TestCostTrackerUnlimited:
    def test_unlimited_budget_never_warns_or_stops(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=0.0)
        for _ in range(100):
            tracker.record(agent_id="a1", task_id="t1", model="opus", input_tokens=0, output_tokens=0, cost_usd=100.0)
        status = tracker.status()
        assert status.should_warn is False
        assert status.should_stop is False
        assert status.remaining_usd == float("inf")
        assert status.percentage_used == 0.0


# ---------------------------------------------------------------------------
# CostTracker — threshold warnings
# ---------------------------------------------------------------------------


class TestCostTrackerThresholds:
    def test_no_warning_below_80_pct(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=7.99
        )
        assert status.should_warn is False
        assert status.should_stop is False

    def test_warning_at_80_pct(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=8.0
        )
        assert status.should_warn is True
        assert status.should_stop is False

    def test_warning_at_95_pct(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=9.5
        )
        assert status.should_warn is True
        assert status.should_stop is False
        assert status.percentage_used == pytest.approx(0.95)

    def test_hard_stop_at_100_pct(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=10.0
        )
        assert status.should_stop is True
        assert status.remaining_usd == pytest.approx(0.0)

    def test_hard_stop_over_budget(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=15.0
        )
        assert status.should_stop is True
        assert status.remaining_usd == 0.0
        assert status.percentage_used == pytest.approx(1.5)

    def test_custom_thresholds(self) -> None:
        tracker = CostTracker(
            run_id="run-1",
            budget_usd=10.0,
            warn_threshold=0.50,
            critical_threshold=0.70,
            hard_stop_threshold=0.90,
        )
        # At 5.0 / 10.0 = 50% — should warn
        status = tracker.record(
            agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=5.0
        )
        assert status.should_warn is True
        assert status.should_stop is False

        # At 9.0 / 10.0 = 90% — should stop
        status = tracker.record(
            agent_id="a2", task_id="t2", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=4.0
        )
        assert status.should_stop is True


# ---------------------------------------------------------------------------
# CostTracker — logging
# ---------------------------------------------------------------------------


class TestCostTrackerLogging:
    def test_warn_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        with caplog.at_level("WARNING"):
            tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=8.5)
            tracker.record(agent_id="a2", task_id="t2", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=0.1)
        warn_messages = [r.message for r in caplog.records if "Budget warning" in r.message]
        assert len(warn_messages) == 1

    def test_critical_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        with caplog.at_level("WARNING"):
            tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=9.5)
            tracker.record(agent_id="a2", task_id="t2", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=0.01)
        critical_messages = [r.message for r in caplog.records if "BUDGET CRITICAL" in r.message]
        assert len(critical_messages) == 1

    def test_hard_stop_logged_every_time(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        with caplog.at_level("WARNING"):
            tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=10.0)
            tracker.record(agent_id="a2", task_id="t2", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=1.0)
        exceeded_messages = [r.message for r in caplog.records if "BUDGET EXCEEDED" in r.message]
        assert len(exceeded_messages) == 2


# ---------------------------------------------------------------------------
# CostTracker — persistence
# ---------------------------------------------------------------------------


class TestCostTrackerPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        tracker = CostTracker(run_id="test-run", budget_usd=5.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=500, output_tokens=500, cost_usd=0.003)
        tracker.record(agent_id="a2", task_id="t2", model="opus", input_tokens=1000, output_tokens=1000, cost_usd=0.03)

        saved_path = tracker.save(tmp_path)
        assert saved_path.exists()

        loaded = CostTracker.load(tmp_path, "test-run")
        assert loaded is not None
        assert loaded.run_id == "test-run"
        assert loaded.budget_usd == pytest.approx(5.0)
        assert loaded.spent_usd == pytest.approx(0.033, abs=1e-6)
        assert len(loaded.usages) == 2

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        tracker = CostTracker(run_id="run-dir-test", budget_usd=1.0)
        saved_path = tracker.save(tmp_path / "nested" / "sdd")
        assert saved_path.exists()
        assert saved_path.parent.name == "costs"

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = CostTracker.load(tmp_path, "nonexistent-run")
        assert result is None

    def test_load_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        costs_dir = tmp_path / "runtime" / "costs"
        costs_dir.mkdir(parents=True)
        (costs_dir / "bad-run.json").write_text("not valid json{{{")
        result = CostTracker.load(tmp_path, "bad-run")
        assert result is None

    def test_persisted_json_structure(self, tmp_path: Path) -> None:
        tracker = CostTracker(run_id="struct-test", budget_usd=10.0)
        tracker.record(agent_id="a1", task_id="t1", model="haiku", input_tokens=100, output_tokens=50, cost_usd=0.001)
        path = tracker.save(tmp_path)
        data = json.loads(path.read_text())
        assert data["run_id"] == "struct-test"
        assert data["budget_usd"] == 10.0
        assert len(data["usages"]) == 1
        assert data["usages"][0]["agent_id"] == "a1"
        assert data["usages"][0]["model"] == "haiku"

    def test_load_preserves_custom_thresholds(self, tmp_path: Path) -> None:
        tracker = CostTracker(
            run_id="thresh-test",
            budget_usd=5.0,
            warn_threshold=0.50,
            critical_threshold=0.75,
            hard_stop_threshold=0.90,
        )
        tracker.save(tmp_path)
        loaded = CostTracker.load(tmp_path, "thresh-test")
        assert loaded is not None
        assert loaded.warn_threshold == pytest.approx(0.50)
        assert loaded.critical_threshold == pytest.approx(0.75)
        assert loaded.hard_stop_threshold == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# CostTracker — budget status reporting
# ---------------------------------------------------------------------------


class TestCostTrackerBudgetReport:
    def test_fresh_tracker_status(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        status = tracker.status()
        assert status.spent_usd == 0.0
        assert status.remaining_usd == pytest.approx(10.0)
        assert status.percentage_used == 0.0
        assert status.should_warn is False
        assert status.should_stop is False

    def test_status_percentage_calculation(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=10.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=2.5)
        status = tracker.status()
        assert status.percentage_used == pytest.approx(0.25)
        assert status.remaining_usd == pytest.approx(7.5)

    def test_status_remaining_clamped_to_zero(self) -> None:
        tracker = CostTracker(run_id="run-1", budget_usd=5.0)
        tracker.record(agent_id="a1", task_id="t1", model="sonnet", input_tokens=0, output_tokens=0, cost_usd=8.0)
        status = tracker.status()
        assert status.remaining_usd == 0.0
        assert status.percentage_used == pytest.approx(1.6)
