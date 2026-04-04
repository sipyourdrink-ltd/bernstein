"""Additional tests for SLO, Error Budget, and orchestrator wiring.

Extends existing test_slo.py coverage for:
- apply_error_budget_adjustments()
- SLOTracker persistence (save/load roundtrip)
- SLO status threshold edge cases
- adaptive_parallelism.set_slo_constraint()
- /slo REST endpoints
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.slo import (
    ErrorBudget,
    SLOStatus,
    SLOTracker,
    apply_error_budget_adjustments,
)

# ===================================================================
# apply_error_budget_adjustments tests
# ===================================================================


class TestApplyErrorBudgetAdjustments:
    """Tests for apply_error_budget_adjustments()."""

    def test_no_adjustment_when_budget_healthy(self) -> None:
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 10
        tracker.error_budget.failed_tasks = 0  # 100% success
        adjusted, override = apply_error_budget_adjustments(6, tracker)
        assert adjusted == 6
        assert override is None

    def test_throttling_disabled(self) -> None:
        """SLO throttling is disabled — always returns config values."""
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 20
        tracker.error_budget.failed_tasks = 4
        adjusted, override = apply_error_budget_adjustments(6, tracker)
        assert adjusted == 6
        assert override is None

    def test_no_adjustment_when_no_tasks(self) -> None:
        tracker = SLOTracker()
        adjusted, override = apply_error_budget_adjustments(6, tracker)
        assert adjusted == 6
        assert override is None

    def test_model_override_disabled(self) -> None:
        """SLO throttling disabled — no model override even when depleted."""
        tracker = SLOTracker()
        tracker.error_budget.total_tasks = 10
        tracker.error_budget.failed_tasks = 4
        tracker.error_budget_policy.upgrade_model = "sonnet"
        _, override = apply_error_budget_adjustments(6, tracker)
        assert override is None


# ===================================================================
# SLO persistence tests
# ===================================================================


class TestSLOPersistence:
    """Tests for SLOTracker save/load."""

    def test_save_and_load_roundtrip(self, tmp_path) -> None:
        tracker = SLOTracker()
        tracker.targets["task_success"].current = 0.85
        tracker.error_budget.total_tasks = 100
        tracker.error_budget.failed_tasks = 15
        tracker._last_save = 0  # Force save

        tracker.save(tmp_path)
        loaded = SLOTracker.load(tmp_path)
        assert loaded.targets["task_success"].current == 0.85
        assert loaded.error_budget.total_tasks == 100
        assert loaded.error_budget.failed_tasks == 15

    def test_save_throttles_within_10s(self, tmp_path) -> None:
        tracker = SLOTracker()
        tracker._last_save = time.time()  # Pretend we just saved
        tracker.save(tmp_path)
        # No file should be written due to throttle
        slo_file = tmp_path / "slos.json"
        assert not slo_file.exists()

    def test_load_missing_file_returns_fresh_tracker(self, tmp_path) -> None:
        tracker = SLOTracker.load(tmp_path)
        assert tracker.error_budget.total_tasks == 0
        assert tracker.error_budget.failed_tasks == 0

    def test_load_corrupt_file_returns_fresh_tracker(self, tmp_path) -> None:
        slo_file = tmp_path / "slos.json"
        slo_file.write_text("not json{{{")
        tracker = SLOTracker.load(tmp_path)
        assert tracker.error_budget.total_tasks == 0


# ===================================================================
# SLO status edge cases
# ===================================================================


class TestSLOStatus:
    """Tests for SLO and error budget status edge cases."""

    def test_zero_tasks_budget_fraction_is_one(self) -> None:
        eb = ErrorBudget(total_tasks=0, failed_tasks=0)
        assert eb.budget_fraction == 1.0
        assert not eb.is_depleted

    def test_budget_exactly_at_depletion(self) -> None:
        eb = ErrorBudget(total_tasks=10, failed_tasks=3, slo_target=0.90)
        # budget_total = max(3, round(10 * 0.1)) = 3, budget_remaining = 0
        assert eb.is_depleted
        assert eb.status == SLOStatus.RED

    def test_time_to_exhaustion_when_budget_healthy(self) -> None:
        eb = ErrorBudget(total_tasks=100, failed_tasks=5, slo_target=0.90)
        # burn_rate < 1.0, so returns None
        assert eb.time_to_exhaustion_tasks is None

    def test_time_to_exhaustion_when_budget_depleting(self) -> None:
        # Need a case where failure rate > allowed rate
        eb2 = ErrorBudget(total_tasks=10, failed_tasks=5, slo_target=0.80)
        # 50% failure rate, allowed 20% -> rate > allowed
        assert eb2.time_to_exhaustion_tasks is not None
        assert eb2.time_to_exhaustion_tasks >= 0

    def test_record_task_transition_depleted_to_recovered(self) -> None:
        eb = ErrorBudget(total_tasks=0, failed_tasks=0, slo_target=0.50)
        # Create many failures to deplete
        for _ in range(10):
            eb.record_task(success=False)
        assert eb.is_depleted
        assert eb._depleted_since is not None
        # Now record successes until we recover
        for _ in range(100):
            eb.record_task(success=True)
        # total_tasks now 110, failed 10 -> ~91% success > 50% target
        # budget_total = round(110 * 0.5) = 55, remaining = 45 -> not depleted
        assert not eb.is_depleted
        assert eb._depleted_since is None


# ===================================================================
# AdaptiveParallelism SLO constraint
# ===================================================================


class TestAdaptiveParallelismSLOConstraint:
    """Tests for AdaptiveParallelism.set_slo_constraint()."""

    def test_slo_constraint_reduces_effective_max(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap.set_slo_constraint(2)
        assert ap.effective_max_agents() <= 2

    def test_slo_constraint_can_be_cleared(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap.set_slo_constraint(2)
        assert ap.effective_max_agents() <= 2
        ap.set_slo_constraint(None)
        # After clearing, _current_max is min(previous, 2) = 2, but
        # without the SLO cap, effective_max_agents can now grow back
        # toward configured_max over time. Verify the constraint is gone.
        assert ap._slo_constrained_max is None
        # _current_max is 2 from the constraint being applied before clearing
        # but the constraint itself (min check) no longer limits it
        result = ap.effective_max_agents()
        assert result >= 2  # Not further reduced

    def test_slo_constraint_lower_than_adaptive_result(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        # Force _current_max to 4
        ap._current_max = 4
        ap.set_slo_constraint(2)
        assert ap.effective_max_agents() == 2

    def test_slo_constraint_higher_than_adaptive_result_is_noop(self) -> None:
        ap = AdaptiveParallelism(configured_max=6)
        ap._current_max = 2
        ap.set_slo_constraint(5)
        # SLO cap (5) > current (2), so SLO doesn't reduce further.
        # But minimum floor (configured_max // 2 = 3) still applies
        # since SLO cap (5) > floor (3).
        assert ap.effective_max_agents() == 3


# ===================================================================
# /slo REST endpoint tests
# ===================================================================


@pytest.fixture()
def jsonl_path(tmp_path):
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path):
    from bernstein.core.server import create_app

    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_get_slo_status_returns_dashboard(client) -> None:
    """GET /slo returns the SLO dashboard JSON."""
    resp = await client.get("/slo")
    assert resp.status_code == 200
    data = resp.json()
    assert "slos" in data
    assert "error_budget" in data
    assert "actions" in data


@pytest.mark.anyio
async def test_get_error_budget_returns_budget(client) -> None:
    """GET /slo/budget returns focused error budget data."""
    resp = await client.get("/slo/budget")
    assert resp.status_code == 200
    data = resp.json()
    assert "budget_fraction" in data
    assert "burn_rate" in data
    assert "is_depleted" in data
    assert "actions" in data


@pytest.mark.anyio
async def test_post_slo_reset_returns_200(client) -> None:
    """POST /slo/reset clears all SLO tracker state."""
    resp = await client.post("/slo/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reset"
