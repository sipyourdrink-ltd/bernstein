"""Smart scheduling based on time-of-day.

Recommends cheaper models during peak API hours (weekday business hours
UTC) to avoid rate limits and reduce cost, and routes expensive tasks
to off-peak windows where throughput is higher.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimeSlot:
    """Describes the current time window and its model recommendation.

    Attributes:
        hour_utc: Current hour in UTC (0-23).
        is_peak: Whether this hour falls in the peak window.
        recommended_model: Model alias recommended for this slot.
        reason: Human-readable explanation for the recommendation.
    """

    hour_utc: int
    is_peak: bool
    recommended_model: str
    reason: str


@dataclass(frozen=True)
class PeakHourConfig:
    """Configuration for peak/off-peak scheduling.

    Attributes:
        peak_start_utc: Hour (0-23) when peak window starts (inclusive).
        peak_end_utc: Hour (0-23) when peak window ends (exclusive).
        peak_days: Weekday indices (0=Mon .. 6=Sun) considered peak.
        peak_model: Model alias to use during peak hours.
        offpeak_model: Model alias to use during off-peak hours.
    """

    peak_start_utc: int = 13  # 1pm UTC
    peak_end_utc: int = 19  # 7pm UTC
    peak_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon-Fri
    peak_model: str = "sonnet"  # cheaper during peak
    offpeak_model: str = "opus"  # expensive during off-peak


def _is_peak(now: datetime.datetime, config: PeakHourConfig) -> bool:
    """Check whether *now* falls within the peak window.

    Args:
        now: Timezone-aware or UTC datetime to check.
        config: Peak-hour configuration.

    Returns:
        True if *now* is in the configured peak window.
    """
    weekday = now.weekday()
    if weekday not in config.peak_days:
        return False
    hour = now.hour
    if config.peak_start_utc <= config.peak_end_utc:
        return config.peak_start_utc <= hour < config.peak_end_utc
    # Wraps midnight (e.g. 22:00 - 06:00)
    return hour >= config.peak_start_utc or hour < config.peak_end_utc


def get_current_slot(
    config: PeakHourConfig | None = None,
    *,
    _now: datetime.datetime | None = None,
) -> TimeSlot:
    """Return current time slot with model recommendation.

    Args:
        config: Peak-hour configuration; uses defaults if ``None``.
        _now: Override current time (for testing).

    Returns:
        A :class:`TimeSlot` describing the current window.
    """
    if config is None:
        config = PeakHourConfig()
    now = _now if _now is not None else datetime.datetime.now(datetime.UTC)
    peak = _is_peak(now, config)

    if peak:
        return TimeSlot(
            hour_utc=now.hour,
            is_peak=True,
            recommended_model=config.peak_model,
            reason=(
                f"Peak hours ({config.peak_start_utc}:00-{config.peak_end_utc}:00 UTC) "
                f"— using {config.peak_model} to avoid rate limits"
            ),
        )
    return TimeSlot(
        hour_utc=now.hour,
        is_peak=False,
        recommended_model=config.offpeak_model,
        reason=(f"Off-peak hours — using {config.offpeak_model} for higher quality"),
    )


def should_downgrade_model(
    config: PeakHourConfig | None = None,
    *,
    _now: datetime.datetime | None = None,
) -> bool:
    """During peak hours, recommend cheaper models to avoid rate limits.

    Args:
        config: Peak-hour configuration; uses defaults if ``None``.
        _now: Override current time (for testing).

    Returns:
        ``True`` if the current time is in the peak window.
    """
    if config is None:
        config = PeakHourConfig()
    now = _now if _now is not None else datetime.datetime.now(datetime.UTC)
    return _is_peak(now, config)


def get_optimal_schedule(
    tasks: list[dict[str, Any]],
    config: PeakHourConfig | None = None,
    *,
    _now: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    """Reorder tasks to run expensive ones during off-peak.

    Tasks with ``complexity="high"`` or ``scope="large"`` are moved to the
    front when off-peak (so they run now with the powerful model) and to
    the back when peak (so cheaper tasks run first and expensive ones are
    deferred).

    Each returned dict is a shallow copy of the input with an added
    ``recommended_model`` key.

    Args:
        tasks: List of task dicts (must have at least ``"id"``).
        config: Peak-hour configuration; uses defaults if ``None``.
        _now: Override current time (for testing).

    Returns:
        Reordered list of task dicts with ``recommended_model`` added.
    """
    if config is None:
        config = PeakHourConfig()
    now = _now if _now is not None else datetime.datetime.now(datetime.UTC)
    peak = _is_peak(now, config)

    def _is_expensive(task: dict[str, Any]) -> bool:
        return str(task.get("complexity", "")).lower() == "high" or str(task.get("scope", "")).lower() == "large"

    expensive: list[dict[str, Any]] = []
    cheap: list[dict[str, Any]] = []
    for t in tasks:
        if _is_expensive(t):
            expensive.append(t)
        else:
            cheap.append(t)

    # Peak: cheap first (defer expensive). Off-peak: expensive first (use opus).
    ordered = cheap + expensive if peak else expensive + cheap

    result: list[dict[str, Any]] = []
    for t in ordered:
        copy = dict(t)
        if _is_expensive(t):
            copy["recommended_model"] = config.offpeak_model if not peak else config.peak_model
        else:
            copy["recommended_model"] = config.peak_model if peak else config.offpeak_model
        result.append(copy)

    return result
