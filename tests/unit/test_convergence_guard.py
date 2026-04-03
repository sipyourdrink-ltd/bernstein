"""Tests for convergence_guard.py — spawn gating on merge queue, agents, error rate."""

from __future__ import annotations

import time

import pytest

from bernstein.core.convergence_guard import ConvergenceGuard, ConvergenceStatus
from bernstein.core.models import ConvergenceGuardConfig

# ---------------------------------------------------------------------------
# ConvergenceStatus
# ---------------------------------------------------------------------------


class TestConvergenceStatus:
    """Tests for ConvergenceStatus validation and construction."""

    def test_ready_with_no_reasons(self) -> None:
        status = ConvergenceStatus(ready=True)
        assert status.ready is True
        assert status.reasons == []

    def test_not_ready_with_reasons(self) -> None:
        status = ConvergenceStatus(ready=False, reasons=["too many agents"])
        assert status.ready is False
        assert "too many agents" in status.reasons

    def test_raises_when_ready_with_reasons(self) -> None:
        with pytest.raises(ValueError, match="ready=True but reasons is non-empty"):
            ConvergenceStatus(ready=True, reasons=["bogus"])


# ---------------------------------------------------------------------------
# ConvergenceGuardConfig
# ---------------------------------------------------------------------------


class TestConvergenceGuardConfig:
    """Tests for ConvergenceGuardConfig defaults."""

    def test_defaults(self) -> None:
        cfg = ConvergenceGuardConfig()
        assert cfg.max_pending_merges == 10
        assert cfg.max_active_agents == 8
        assert cfg.max_error_rate == 0.5
        assert cfg.max_spawn_rate == 12.0
        assert cfg.error_rate_window_seconds == 300
        assert cfg.spawn_rate_window_seconds == 60

    def test_custom_values(self) -> None:
        cfg = ConvergenceGuardConfig(max_active_agents=4, max_spawn_rate=6.0)
        assert cfg.max_active_agents == 4
        assert cfg.max_spawn_rate == 6.0
        # Others stay at defaults
        assert cfg.max_pending_merges == 10


# ---------------------------------------------------------------------------
# ConvergenceGuard.is_converged()
# ---------------------------------------------------------------------------


class TestConvergenceGuardIsConverged:
    """Tests for the main convergence check gates."""

    def test_all_pass(self) -> None:
        guard = ConvergenceGuard()
        status = guard.is_converged(
            pending_merges=2,
            active_agents=3,
            error_rate=0.1,
            spawn_rate=5.0,
        )
        assert status.ready is True
        assert status.reasons == []

    def test_rejects_high_merge_queue(self) -> None:
        guard = ConvergenceGuard()
        status = guard.is_converged(pending_merges=15)
        assert status.ready is False
        assert status.pending_merges == 15
        assert any("Merge queue" in r for r in status.reasons)

    def test_rejects_too_many_active_agents(self) -> None:
        guard = ConvergenceGuard()
        status = guard.is_converged(active_agents=10)
        assert status.ready is False
        assert status.active_agents == 10
        assert any("active agents" in r for r in status.reasons)

    def test_at_cap_is_not_ready(self) -> None:
        """active_agents == max_active_agents blocks spawns (>= check)."""
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_active_agents=5))
        status = guard.is_converged(active_agents=5)
        assert status.ready is False

    def test_one_below_cap_is_ok(self) -> None:
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_active_agents=5))
        status = guard.is_converged(active_agents=4)
        assert status.ready is True

    def test_rejects_high_error_rate(self) -> None:
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_error_rate=0.3))
        status = guard.is_converged(error_rate=0.6)
        assert status.ready is False
        assert status.error_rate == 0.6
        assert any("error rate" in r.lower() for r in status.reasons)

    def test_rejects_high_spawn_rate(self) -> None:
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_spawn_rate=5.0))
        status = guard.is_converged(spawn_rate=10.0)
        assert status.ready is False
        assert status.spawn_rate == 10.0
        assert any("Spawn rate" in r for r in status.reasons)

    def test_error_rate_at_threshold_not_rejected(self) -> None:
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_error_rate=0.5))
        status = guard.is_converged(error_rate=0.5)
        assert status.ready is True  # strictly > threshold

    def test_none_parameters_skipped(self) -> None:
        """Passing None skips that gate entirely."""
        guard = ConvergenceGuard()
        status = guard.is_converged(
            pending_merges=None,
            active_agents=None,
            error_rate=None,
            spawn_rate=None,
        )
        assert status.ready is True

    def test_multiple_reasons(self) -> None:
        guard = ConvergenceGuard(ConvergenceGuardConfig(max_active_agents=3, max_error_rate=0.2))
        status = guard.is_converged(
            pending_merges=20,
            active_agents=5,
            error_rate=0.8,
            spawn_rate=1.0,
        )
        assert status.ready is False
        assert len(status.reasons) == 3  # merges, agents, error (spawn passes)

    def test_custom_config(self) -> None:
        cfg = ConvergenceGuardConfig(max_pending_merges=2, max_active_agents=3)
        guard = ConvergenceGuard(cfg)
        status = guard.is_converged(pending_merges=3, active_agents=2)
        assert status.ready is False
        assert status.reasons == ["Merge queue overloaded (3/2)"]

    def test_config_property(self) -> None:
        cfg = ConvergenceGuardConfig(max_error_rate=0.1)
        guard = ConvergenceGuard(cfg)
        assert guard.config is cfg


# ---------------------------------------------------------------------------
# Sliding-window helpers
# ---------------------------------------------------------------------------


class TestSlidingWindows:
    """Tests for record_spawn, record_success, record_failure, current_*."""

    def _guard(self) -> ConvergenceGuard:
        return ConvergenceGuard()

    def test_record_spawn_current_rate(self) -> None:
        now = time.time()
        guard = self._guard()
        # 6 spawns in a 60-second window -> 6 per minute
        for i in range(6):
            guard.record_spawn(now=now - (i * 5))
        rate = guard.current_spawn_rate(now=now)
        assert rate == pytest.approx(6.0, abs=0.01)

    def test_spawn_pruning_removes_old(self) -> None:
        now = time.time()
        guard = ConvergenceGuard(ConvergenceGuardConfig(spawn_rate_window_seconds=60))
        # 1 spawn 120 seconds ago, 1 recent
        guard.record_spawn(now=now - 120)
        guard.record_spawn(now=now - 10)
        rate = guard.current_spawn_rate(now=now)
        assert rate == pytest.approx(1.0, abs=0.01)

    def test_error_rate_no_data_returns_negative_one(self) -> None:
        guard = self._guard()
        assert guard.current_error_rate() == -1.0

    def test_error_rate_all_success(self) -> None:
        now = time.time()
        guard = self._guard()
        for i in range(5):
            guard.record_success(now=now - i * 10)
        assert guard.current_error_rate(now=now) == 0.0

    def test_error_rate_half_failures(self) -> None:
        now = time.time()
        guard = self._guard()
        for i in range(5):
            guard.record_success(now=now - i * 10)
            guard.record_failure(now=now - i * 10)
        assert guard.current_error_rate(now=now) == pytest.approx(0.5, abs=0.01)

    def test_error_rate_all_failures(self) -> None:
        now = time.time()
        guard = self._guard()
        for i in range(3):
            guard.record_failure(now=now - i * 10)
        assert guard.current_error_rate(now=now) == pytest.approx(1.0, abs=0.01)

    def test_error_rate_pruning_removes_old_failures(self) -> None:
        now = time.time()
        guard = ConvergenceGuard(ConvergenceGuardConfig(error_rate_window_seconds=60))
        # 5 old failures outside window, 2 recent successes inside
        guard.record_failure(now=now - 120)
        guard.record_failure(now=now - 120)
        guard.record_failure(now=now - 120)
        guard.record_failure(now=now - 120)
        guard.record_failure(now=now - 120)
        guard.record_success(now=now - 10)
        guard.record_success(now=now - 20)
        rate = guard.current_error_rate(now=now)
        assert rate == 0.0  # old failures pruned, only successes remain

    def test_spawn_rate_zero_when_no_spawns(self) -> None:
        guard = self._guard()
        assert guard.current_spawn_rate() == 0.0

    def test_reset_clears_all(self) -> None:
        now = time.time()
        guard = self._guard()
        guard.record_spawn(now=now)
        guard.record_success(now=now)
        guard.record_failure(now=now)
        guard.reset()
        assert guard.current_spawn_rate(now=now) == 0.0
        assert guard.current_error_rate(now=now) == -1.0
