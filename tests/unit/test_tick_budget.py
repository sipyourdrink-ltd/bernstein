"""Tests for tick duration budget (ORCH-010)."""

from __future__ import annotations

import time

from bernstein.core.tick_budget import PhaseRecord, TickBudget, TickBudgetSummary


class TestTickBudget:
    def test_initial_state(self) -> None:
        budget = TickBudget(budget_ms=2000.0)
        assert budget.budget_ms == 2000.0
        assert budget.elapsed_ms() == 0.0
        assert budget.has_remaining() is True
        assert budget.phases == []
        assert budget.skipped_phases == []

    def test_start_resets_state(self) -> None:
        budget = TickBudget(budget_ms=2000.0)
        budget.start()
        # Elapsed should be very small
        assert budget.elapsed_ms() < 100.0
        assert budget.has_remaining() is True

    def test_phase_tracking(self) -> None:
        budget = TickBudget(budget_ms=5000.0)
        budget.start()

        with budget.phase("fetch_tasks", critical=True):
            pass  # instant

        assert len(budget.phases) == 1
        phase = budget.phases[0]
        assert phase.name == "fetch_tasks"
        assert phase.critical is True
        assert phase.duration_ms >= 0.0
        assert phase.skipped is False

    def test_record_skip(self) -> None:
        budget = TickBudget(budget_ms=5000.0)
        budget.start()
        budget.record_skip("metrics")

        assert len(budget.skipped_phases) == 1
        assert "metrics" in budget.skipped_phases
        assert len(budget.phases) == 1
        assert budget.phases[0].skipped is True

    def test_has_remaining_false_when_exceeded(self) -> None:
        budget = TickBudget(budget_ms=0.0)  # zero budget
        budget.start()
        time.sleep(0.001)  # ensure some time passes
        assert budget.has_remaining() is False

    def test_summary(self) -> None:
        budget = TickBudget(budget_ms=5000.0)
        budget.start()

        with budget.phase("heartbeat", critical=True):
            pass
        budget.record_skip("nudges")

        summary = budget.summary()
        assert isinstance(summary, TickBudgetSummary)
        assert summary.budget_ms == 5000.0
        assert summary.phases_executed == 1
        assert summary.phases_skipped == 1
        assert "nudges" in summary.skipped_phase_names
        assert "heartbeat" in summary.phase_durations

    def test_multiple_phases(self) -> None:
        budget = TickBudget(budget_ms=5000.0)
        budget.start()

        with budget.phase("phase_a", critical=True):
            pass
        with budget.phase("phase_b"):
            pass
        with budget.phase("phase_c"):
            pass

        assert len(budget.phases) == 3
        names = [p.name for p in budget.phases]
        assert names == ["phase_a", "phase_b", "phase_c"]


class TestPhaseRecord:
    def test_defaults(self) -> None:
        record = PhaseRecord(name="test")
        assert record.name == "test"
        assert record.critical is False
        assert record.duration_ms == 0.0
        assert record.skipped is False


class TestTickBudgetSummary:
    def test_over_budget_flag(self) -> None:
        budget = TickBudget(budget_ms=0.0)
        budget.start()
        time.sleep(0.001)
        summary = budget.summary()
        assert summary.over_budget is True
