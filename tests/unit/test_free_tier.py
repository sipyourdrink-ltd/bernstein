"""Tests for the FreeTierMaximizer and FreeTierStatus."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.cost.free_tier import (
    FREE_TIER_LIMITS,
    FreeTierMaximizer,
    FreeTierStatus,
)

# --- FreeTierStatus tests ---


class TestFreeTierStatus:
    """Tests for FreeTierStatus dataclass."""

    def test_utilization_pct(self) -> None:
        status = FreeTierStatus(
            provider="gemini",
            remaining_today=750,
            limit_today=1500,
            remaining_minute=15,
            limit_minute=15,
        )
        assert status.utilization_pct == 50.0

    def test_utilization_pct_zero_limit(self) -> None:
        status = FreeTierStatus(
            provider="test",
            remaining_today=0,
            limit_today=0,
            remaining_minute=0,
            limit_minute=0,
        )
        assert status.utilization_pct == 0.0

    def test_is_available_true(self) -> None:
        status = FreeTierStatus(
            provider="gemini",
            remaining_today=100,
            limit_today=1500,
            remaining_minute=5,
            limit_minute=15,
        )
        assert status.is_available is True

    def test_is_available_false_daily_exhausted(self) -> None:
        status = FreeTierStatus(
            provider="gemini",
            remaining_today=0,
            limit_today=1500,
            remaining_minute=5,
            limit_minute=15,
        )
        assert status.is_available is False

    def test_is_available_false_minute_exhausted(self) -> None:
        status = FreeTierStatus(
            provider="gemini",
            remaining_today=100,
            limit_today=1500,
            remaining_minute=0,
            limit_minute=15,
        )
        assert status.is_available is False

    def test_to_dict(self) -> None:
        status = FreeTierStatus(
            provider="codex",
            remaining_today=40,
            limit_today=50,
            remaining_minute=3,
            limit_minute=3,
        )
        d = status.to_dict()
        assert d["provider"] == "codex"
        assert d["remaining_today"] == 40
        assert d["is_available"] is True
        assert "utilization_pct" in d


# --- FreeTierMaximizer tests ---


class TestFreeTierMaximizer:
    """Tests for FreeTierMaximizer."""

    def test_initializes_with_defaults(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        statuses = ftm.get_all_statuses()
        providers = {s.provider for s in statuses}
        assert "gemini" in providers
        assert "codex" in providers
        assert "qwen" in providers

    def test_record_request_decrements(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        initial = next(s for s in ftm.get_all_statuses() if s.provider == "gemini")
        initial_remaining = initial.remaining_today

        ftm.record_request("gemini")

        updated = next(s for s in ftm.get_all_statuses() if s.provider == "gemini")
        assert updated.remaining_today == initial_remaining - 1

    def test_record_request_unknown_provider_noop(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        ftm.record_request("nonexistent_provider")
        # Should not raise

    def test_record_request_never_goes_below_zero(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        # Exhaust a small quota
        for _ in range(55):
            ftm.record_request("codex")
        status = next(s for s in ftm.get_all_statuses() if s.provider == "codex")
        assert status.remaining_today == 0

    def test_get_best_free_provider(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        best = ftm.get_best_free_provider()
        # Gemini has the highest limit (1500)
        assert best == "gemini"

    def test_get_best_free_provider_none_when_exhausted(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        # Exhaust all providers
        for status in ftm._statuses.values():
            status.remaining_today = 0
            status.remaining_minute = 0
        assert ftm.get_best_free_provider() is None

    def test_should_use_free_tier(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        # Fresh state, well under 80% utilization
        assert ftm.should_use_free_tier("gemini") is True

    def test_should_use_free_tier_unknown_provider(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        assert ftm.should_use_free_tier("unknown") is False

    def test_should_use_free_tier_high_utilization(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        status = ftm._statuses["codex"]
        # Set utilization above 80%
        status.remaining_today = 5  # out of 50 = 90% used
        assert ftm.should_use_free_tier("codex") is False

    def test_get_summary(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        summary = ftm.get_summary()
        assert summary["total_providers"] == len(FREE_TIER_LIMITS)
        assert summary["available_providers"] == len(FREE_TIER_LIMITS)
        assert "overall_utilization_pct" in summary
        assert "providers" in summary

    def test_persistence(self, tmp_path: Path) -> None:
        ftm1 = FreeTierMaximizer(tmp_path)
        ftm1.record_request("gemini")
        remaining_after_1 = next(s for s in ftm1.get_all_statuses() if s.provider == "gemini").remaining_today

        # Create new instance from same workdir
        ftm2 = FreeTierMaximizer(tmp_path)
        remaining_loaded = next(s for s in ftm2.get_all_statuses() if s.provider == "gemini").remaining_today
        assert remaining_loaded == remaining_after_1

    def test_reset_daily_limits(self, tmp_path: Path) -> None:
        ftm = FreeTierMaximizer(tmp_path)
        # Exhaust and set reset_time to the past
        status = ftm._statuses["codex"]
        status.remaining_today = 0
        status.reset_time = time.time() - 1

        ftm.reset_daily_limits()

        updated = ftm._statuses["codex"]
        assert updated.remaining_today == updated.limit_today
