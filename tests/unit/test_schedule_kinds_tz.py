"""Timezone-deterministic fires across DST transitions (#2546, AC2).

For a recipe declaring an IANA timezone:

- hosts with different system timezones compute the identical fire
  projection hash for the same instant (the projection folds the declared
  zone; the instant itself is an epoch int, host-independent);
- replay across the DST-forward gap and the DST-back overlap resolves
  byte-identically per the declared ambiguity policy.

The resolver never reads the host locale (only stdlib ``zoneinfo``), so the
two-host proof is that setting ``TZ`` to different zones does not move the
resolved epoch or the projection hash.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from bernstein.core.orchestration.schedule_kinds import (
    DstPolicy,
    ScheduleKindError,
    canonical_timezone,
    interval_anchor_fires,
    is_ambiguous_local,
    is_imaginary_local,
    resolve_local_instant,
)
from bernstein.core.orchestration.schedule_projection import project_schedule_fire

# America/New_York 2026 transitions: spring-forward 2026-03-08 02:00 ->
# 03:00 (02:30 is imaginary); fall-back 2026-11-01 02:00 -> 01:00 (01:30 is
# ambiguous).
_GAP = datetime(2026, 3, 8, 2, 30)
_OVERLAP = datetime(2026, 11, 1, 1, 30)
_TZ = "America/New_York"


class _HostTimezone:
    """Context manager that pins the process TZ (and restores it)."""

    def __init__(self, tz: str) -> None:
        self._tz = tz
        self._prev: str | None = None

    def __enter__(self) -> None:
        self._prev = os.environ.get("TZ")
        os.environ["TZ"] = self._tz
        if hasattr(time, "tzset"):
            time.tzset()

    def __exit__(self, *_exc: object) -> None:
        if self._prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prev
        if hasattr(time, "tzset"):
            time.tzset()


class TestCanonicalTimezone:
    def test_empty_is_utc_sentinel(self) -> None:
        assert canonical_timezone("") == ""

    def test_valid_zone_round_trips(self) -> None:
        assert canonical_timezone("Europe/Berlin") == "Europe/Berlin"

    def test_unknown_zone_rejected(self) -> None:
        with pytest.raises(ScheduleKindError):
            canonical_timezone("Mars/Olympus_Mons")


class TestDstDetection:
    def test_gap_is_imaginary(self) -> None:
        from zoneinfo import ZoneInfo

        assert is_imaginary_local(ZoneInfo(_TZ), _GAP)

    def test_overlap_is_ambiguous(self) -> None:
        from zoneinfo import ZoneInfo

        assert is_ambiguous_local(ZoneInfo(_TZ), _OVERLAP)


class TestHostIndependence:
    @pytest.mark.parametrize("host_tz", ["UTC", "Asia/Tokyo", "America/Los_Angeles", "Europe/London"])
    def test_overlap_resolves_identically_on_every_host(self, host_tz: str) -> None:
        with _HostTimezone("UTC"):
            reference = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.POST_TRANSITION)
        with _HostTimezone(host_tz):
            got = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.POST_TRANSITION)
        assert got == reference

    @pytest.mark.parametrize("host_tz", ["UTC", "Asia/Tokyo", "America/Los_Angeles"])
    def test_gap_resolves_identically_on_every_host(self, host_tz: str) -> None:
        with _HostTimezone("UTC"):
            reference = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.PRE_TRANSITION)
        with _HostTimezone(host_tz):
            got = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.PRE_TRANSITION)
        assert got == reference


class TestDstPolicy:
    def test_overlap_pre_and_post_are_one_hour_apart(self) -> None:
        pre = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.PRE_TRANSITION)
        post = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.POST_TRANSITION)
        assert post - pre == 3600

    def test_gap_pre_precedes_post(self) -> None:
        pre = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.PRE_TRANSITION)
        post = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.POST_TRANSITION)
        assert pre < post

    def test_strict_rejects_gap(self) -> None:
        with pytest.raises(ScheduleKindError):
            resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.STRICT)

    def test_strict_rejects_overlap(self) -> None:
        with pytest.raises(ScheduleKindError):
            resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.STRICT)

    def test_aware_datetime_rejected(self) -> None:
        from zoneinfo import ZoneInfo

        aware = _OVERLAP.replace(tzinfo=ZoneInfo(_TZ))
        with pytest.raises(ScheduleKindError):
            resolve_local_instant(tz_name=_TZ, naive_local=aware, dst_policy=DstPolicy.PRE_TRANSITION)


class TestProjectionFolding:
    def test_declared_timezone_binds_the_graph_hash(self) -> None:
        instant = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.POST_TRANSITION)
        with_tz = project_schedule_fire(
            schedule_id="recipe_abc",
            fire_time=instant,
            last_state=None,
            timezone=_TZ,
            dst_policy=str(DstPolicy.POST_TRANSITION),
        )
        without_tz = project_schedule_fire(schedule_id="recipe_abc", fire_time=instant, last_state=None)
        assert with_tz.projection_hash != without_tz.projection_hash

    def test_same_instant_and_zone_gives_identical_hash_across_hosts(self) -> None:
        instant = resolve_local_instant(tz_name=_TZ, naive_local=_OVERLAP, dst_policy=DstPolicy.POST_TRANSITION)
        with _HostTimezone("Asia/Tokyo"):
            a = project_schedule_fire(
                schedule_id="recipe_abc",
                fire_time=instant,
                last_state=None,
                timezone=_TZ,
                dst_policy=str(DstPolicy.POST_TRANSITION),
            )
        with _HostTimezone("America/Los_Angeles"):
            b = project_schedule_fire(
                schedule_id="recipe_abc",
                fire_time=instant,
                last_state=None,
                timezone=_TZ,
                dst_policy=str(DstPolicy.POST_TRANSITION),
            )
        assert a.projection_hash == b.projection_hash
        assert a.canonical_bytes == b.canonical_bytes

    def test_zoneless_projection_is_byte_identical_to_pre_change(self) -> None:
        # AC8: a zone-less schedule stays byte-identical to a pre-#2546 projection.
        baseline = project_schedule_fire(schedule_id="s", fire_time=1_800_000_000, last_state=None, goal="g")
        again = project_schedule_fire(
            schedule_id="s",
            fire_time=1_800_000_000,
            last_state=None,
            goal="g",
            timezone="",
            dst_policy="",
        )
        assert baseline.canonical_bytes == again.canonical_bytes


class TestIntervalAnchor:
    def test_grid_is_pure_function_of_anchor(self) -> None:
        # Two operators without a shared clock agree on the fire grid.
        first = interval_anchor_fires(anchor_epoch=1000, interval_seconds=3600, after_epoch=1000)
        assert first == 1000 + 3600
        assert interval_anchor_fires(anchor_epoch=1000, interval_seconds=3600, after_epoch=5000) == 1000 + 2 * 3600

    def test_before_anchor_returns_anchor(self) -> None:
        assert interval_anchor_fires(anchor_epoch=1000, interval_seconds=60, after_epoch=500) == 1000

    def test_non_positive_interval_rejected(self) -> None:
        with pytest.raises(ScheduleKindError):
            interval_anchor_fires(anchor_epoch=0, interval_seconds=0, after_epoch=10)
