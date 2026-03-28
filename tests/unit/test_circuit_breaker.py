"""Tests for CircuitBreaker."""

from bernstein.evolution.circuit import CircuitBreaker
from bernstein.evolution.types import CircuitState, RiskLevel


class TestCircuitBreaker:
    def test_closed_allows_l0(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        ok, _ = cb.can_evolve(RiskLevel.L0_CONFIG)
        assert ok

    def test_l3_always_blocked(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        ok, _ = cb.can_evolve(RiskLevel.L3_STRUCTURAL)
        assert not ok

    def test_rate_limit(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        for i in range(5):
            cb.record_change(RiskLevel.L0_CONFIG, f"p-{i}")
        ok, reason = cb.can_evolve(RiskLevel.L0_CONFIG)
        assert not ok
        assert "Rate limit" in reason

    def test_rollback_trips(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        cb.record_rollback("p-1")
        assert cb.state == CircuitState.OPEN

    def test_persists(self, tmp_path):
        cb1 = CircuitBreaker(state_dir=tmp_path)
        cb1.record_rollback("p-1")
        cb2 = CircuitBreaker(state_dir=tmp_path)
        assert cb2.state == CircuitState.OPEN

    def test_metrics_regression_trips(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        cb.check_metrics_regression(janitor_pass_rate_delta=-0.20, cost_per_task_delta=0.0)
        assert cb.state == CircuitState.OPEN

    def test_reset(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        cb.record_rollback("p-1")
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
