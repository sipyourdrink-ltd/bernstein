"""Tests for peak-hour smart scheduling."""

from __future__ import annotations

import datetime

import pytest

from bernstein.core.cost.peak_hour_router import (
    PeakHourConfig,
    TimeSlot,
    get_current_slot,
    get_optimal_schedule,
    should_downgrade_model,
)


def _utc(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    """Build a UTC datetime for testing."""
    return datetime.datetime(year, month, day, hour, 0, 0, tzinfo=datetime.UTC)


# --- TimeSlot / PeakHourConfig frozen dataclass ---


class TestDataclasses:
    def test_timeslot_is_frozen(self) -> None:
        slot = TimeSlot(hour_utc=14, is_peak=True, recommended_model="sonnet", reason="peak")
        with pytest.raises(AttributeError):
            slot.hour_utc = 10  # type: ignore[misc]

    def test_peak_hour_config_defaults(self) -> None:
        cfg = PeakHourConfig()
        assert cfg.peak_start_utc == 13
        assert cfg.peak_end_utc == 19
        assert cfg.peak_days == (0, 1, 2, 3, 4)
        assert cfg.peak_model == "sonnet"
        assert cfg.offpeak_model == "opus"


# --- get_current_slot ---


class TestGetCurrentSlot:
    def test_weekday_peak_returns_sonnet(self) -> None:
        # Wednesday 15:00 UTC => peak
        now = _utc(2026, 4, 15, 15)
        slot = get_current_slot(_now=now)
        assert slot.is_peak is True
        assert slot.recommended_model == "sonnet"
        assert slot.hour_utc == 15

    def test_weekday_offpeak_returns_opus(self) -> None:
        # Wednesday 06:00 UTC => off-peak
        now = _utc(2026, 4, 15, 6)
        slot = get_current_slot(_now=now)
        assert slot.is_peak is False
        assert slot.recommended_model == "opus"

    def test_weekend_always_offpeak(self) -> None:
        # Saturday 15:00 UTC => off-peak (weekend)
        now = _utc(2026, 4, 18, 15)
        slot = get_current_slot(_now=now)
        assert slot.is_peak is False
        assert slot.recommended_model == "opus"

    def test_custom_config(self) -> None:
        cfg = PeakHourConfig(peak_start_utc=8, peak_end_utc=12, peak_model="haiku", offpeak_model="sonnet")
        now = _utc(2026, 4, 13, 10)  # Monday 10:00 UTC
        slot = get_current_slot(cfg, _now=now)
        assert slot.is_peak is True
        assert slot.recommended_model == "haiku"

    def test_boundary_start_inclusive(self) -> None:
        now = _utc(2026, 4, 13, 13)  # exactly 13:00
        slot = get_current_slot(_now=now)
        assert slot.is_peak is True

    def test_boundary_end_exclusive(self) -> None:
        now = _utc(2026, 4, 13, 19)  # exactly 19:00
        slot = get_current_slot(_now=now)
        assert slot.is_peak is False

    def test_reason_contains_model_name(self) -> None:
        now = _utc(2026, 4, 13, 15)
        slot = get_current_slot(_now=now)
        assert "sonnet" in slot.reason


# --- should_downgrade_model ---


class TestShouldDowngradeModel:
    def test_peak_returns_true(self) -> None:
        now = _utc(2026, 4, 13, 15)
        assert should_downgrade_model(_now=now) is True

    def test_offpeak_returns_false(self) -> None:
        now = _utc(2026, 4, 13, 6)
        assert should_downgrade_model(_now=now) is False

    def test_weekend_returns_false(self) -> None:
        now = _utc(2026, 4, 18, 15)
        assert should_downgrade_model(_now=now) is False


# --- get_optimal_schedule ---


class TestGetOptimalSchedule:
    @pytest.fixture()
    def mixed_tasks(self) -> list[dict[str, object]]:
        return [
            {"id": "t1", "complexity": "low", "scope": "small"},
            {"id": "t2", "complexity": "high", "scope": "large"},
            {"id": "t3", "complexity": "medium", "scope": "medium"},
            {"id": "t4", "complexity": "high", "scope": "small"},
        ]

    def test_peak_cheap_first(self, mixed_tasks: list[dict[str, object]]) -> None:
        now = _utc(2026, 4, 13, 15)  # peak
        result = get_optimal_schedule(mixed_tasks, _now=now)
        ids = [t["id"] for t in result]
        # Cheap tasks (t1, t3) come before expensive (t2, t4)
        assert ids.index("t1") < ids.index("t2")
        assert ids.index("t3") < ids.index("t2")

    def test_offpeak_expensive_first(self, mixed_tasks: list[dict[str, object]]) -> None:
        now = _utc(2026, 4, 13, 6)  # off-peak
        result = get_optimal_schedule(mixed_tasks, _now=now)
        ids = [t["id"] for t in result]
        # Expensive tasks (t2, t4) come before cheap (t1, t3)
        assert ids.index("t2") < ids.index("t1")
        assert ids.index("t4") < ids.index("t3")

    def test_recommended_model_added(self, mixed_tasks: list[dict[str, object]]) -> None:
        now = _utc(2026, 4, 13, 15)
        result = get_optimal_schedule(mixed_tasks, _now=now)
        for t in result:
            assert "recommended_model" in t

    def test_does_not_mutate_input(self, mixed_tasks: list[dict[str, object]]) -> None:
        now = _utc(2026, 4, 13, 15)
        original_ids = [t["id"] for t in mixed_tasks]
        get_optimal_schedule(mixed_tasks, _now=now)
        assert [t["id"] for t in mixed_tasks] == original_ids
        assert "recommended_model" not in mixed_tasks[0]

    def test_empty_tasks(self) -> None:
        result = get_optimal_schedule([], _now=_utc(2026, 4, 13, 15))
        assert result == []

    def test_all_cheap_unchanged_order(self) -> None:
        tasks = [
            {"id": "a", "complexity": "low"},
            {"id": "b", "complexity": "low"},
        ]
        result = get_optimal_schedule(tasks, _now=_utc(2026, 4, 13, 15))
        assert [t["id"] for t in result] == ["a", "b"]
