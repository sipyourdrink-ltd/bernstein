"""Tests for CircuitBreaker."""

import inspect
import time

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


class TestRollbackHaltRule:
    """What the rollback rule actually says, as opposed to what it appears to say.

    `record_rollback` appends `now` and then trips if any rollback falls inside
    48 hours -- so the first rollback always trips. That is the policy, and
    `test_rollback_trips` above pins it.

    It also carried an `elif len(self.recent_rollbacks) > 2` arm reading
    ">2 rollbacks in 7 days", which could never run: the append guarantees the
    48-hour list is non-empty whenever the list is. A rule stated in code that
    cannot fire reads as a policy the system has and does not.
    """

    def test_one_rollback_trips_even_with_no_history(self, tmp_path):
        cb = CircuitBreaker(state_dir=tmp_path)
        assert cb.recent_rollbacks == []
        cb.record_rollback("p-1")
        assert cb.state == CircuitState.OPEN

    def test_rollbacks_older_than_48h_do_not_soften_the_current_one(self, tmp_path):
        # The arrangement that would have been needed to reach the old `elif`:
        # three rollbacks well outside the window, then a fourth. It trips on
        # the fourth via the 48-hour rule, never via a 7-day count.
        cb = CircuitBreaker(state_dir=tmp_path)
        stale = time.time() - 72 * 3600
        cb.recent_rollbacks = [stale, stale, stale]
        cb.record_rollback("p-4")
        assert cb.state == CircuitState.OPEN

    def test_the_seven_day_rule_is_not_reachable_and_is_gone(self, tmp_path):
        # Asserted on the source of THIS METHOD, because the property is that a
        # branch does not exist rather than that it behaves a certain way, and a
        # behavioural test cannot distinguish an unreachable branch from an
        # absent one. Scoped with `getsource` rather than reading the file, so
        # the explanation in the comments above it does not answer for the code.
        body = inspect.getsource(CircuitBreaker.record_rollback)
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        assert "elif" not in code, "the unreachable second halt rule is back"
        assert code.count("_trip") == 1
