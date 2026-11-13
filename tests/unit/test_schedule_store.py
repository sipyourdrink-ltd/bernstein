"""Unit tests for the operator-registered schedule store (#1798)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import IO, TYPE_CHECKING

import pytest

from bernstein.core.persistence import file_locks
from bernstein.core.planning.schedule_store import (
    FIRE_CURSOR_KEY,
    CronParseError,
    ScheduleStore,
    _canonical_schedule_id,
    parse_cron,
    validate_cron,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _overlap_writes(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    first_write: Callable[[], None],
    second_write: Callable[[], None],
) -> None:
    first_at_publish = Event()
    release_first = Event()
    real_replace = os.replace
    real_try_lock = file_locks._os_try_lock

    def publish(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) != target:
            real_replace(src, dst)
        elif first_at_publish.is_set():
            real_replace(src, dst)
            release_first.set()
        else:
            first_at_publish.set()
            # A second writer either published, or attempted to lock the
            # record before reading the first writer's pending update.
            assert release_first.wait(timeout=30)
            real_replace(src, dst)

    def observe_contention(handle: IO[bytes]) -> bool:
        acquired = real_try_lock(handle)
        if not acquired:
            release_first.set()
        return acquired

    monkeypatch.setattr(os, "replace", publish)
    monkeypatch.setattr(file_locks, "_os_try_lock", observe_contention)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_write)
        assert first_at_publish.wait(timeout=30)
        second = pool.submit(second_write)
        first.result(timeout=60)
        second.result(timeout=60)


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------


class TestValidateCron:
    def test_every_minute(self) -> None:
        validate_cron("* * * * *")

    def test_explicit_minute(self) -> None:
        validate_cron("5 * * * *")

    def test_range_and_step(self) -> None:
        validate_cron("0 9-17 * * 1-5")

    def test_step_with_wildcard(self) -> None:
        validate_cron("*/15 * * * *")

    def test_named_month(self) -> None:
        validate_cron("0 0 1 jan *")

    def test_named_weekday(self) -> None:
        validate_cron("0 9 * * mon")

    @pytest.mark.parametrize(
        "expression",
        [
            "",  # empty
            "* * * *",  # too few fields
            "* * * * * *",  # too many fields
            "60 * * * *",  # minute out of range
            "* 24 * * *",  # hour out of range
            "* * 0 * *",  # day below range
            "* * * 13 *",  # month out of range
            "* * * * 7",  # weekday out of range
            "*/0 * * * *",  # zero step
            "a * * * *",  # garbage atom
            "5-1 * * * *",  # reversed range
        ],
    )
    def test_invalid_expressions(self, expression: str) -> None:
        with pytest.raises(CronParseError):
            validate_cron(expression)


class TestParseCronExpansion:
    def test_wildcard_minute_covers_full_range(self) -> None:
        parsed = parse_cron("* * * * *")
        assert parsed.minutes == frozenset(range(0, 60))
        assert parsed.hours == frozenset(range(0, 24))
        assert parsed.weekdays == frozenset(range(0, 7))

    def test_step_minutes(self) -> None:
        parsed = parse_cron("*/15 * * * *")
        assert parsed.minutes == frozenset({0, 15, 30, 45})

    def test_named_weekday_aliases(self) -> None:
        parsed = parse_cron("0 0 * * mon")
        assert parsed.weekdays == frozenset({1})

    def test_list_in_field(self) -> None:
        parsed = parse_cron("0,30 * * * *")
        assert parsed.minutes == frozenset({0, 30})


# ---------------------------------------------------------------------------
# Schedule id derivation
# ---------------------------------------------------------------------------


class TestCanonicalScheduleId:
    def test_same_inputs_same_id(self) -> None:
        a = _canonical_schedule_id("0 9 * * *", "Send daily digest", "")
        b = _canonical_schedule_id("0 9 * * *", "Send daily digest", "")
        assert a == b

    def test_different_cron_different_id(self) -> None:
        a = _canonical_schedule_id("0 9 * * *", "Send daily digest", "")
        b = _canonical_schedule_id("0 10 * * *", "Send daily digest", "")
        assert a != b

    def test_different_goal_different_id(self) -> None:
        a = _canonical_schedule_id("0 9 * * *", "Send daily digest", "")
        b = _canonical_schedule_id("0 9 * * *", "Send weekly digest", "")
        assert a != b

    def test_prefix(self) -> None:
        id_ = _canonical_schedule_id("0 9 * * *", "G", "")
        assert id_.startswith("sched_")


# ---------------------------------------------------------------------------
# ScheduleStore CRUD
# ---------------------------------------------------------------------------


class TestScheduleStoreAdd:
    def test_add_persists_to_disk(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="Daily digest")
        path = tmp_path / "runtime" / "schedules" / f"{schedule.id}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["id"] == schedule.id
        assert data["cron"] == "0 9 * * *"
        assert data["goal"] == "Daily digest"
        assert data["misfire_policy"] == "skip"

    def test_add_idempotent(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        a = store.add(cron="0 9 * * *", goal="Daily digest")
        b = store.add(cron="0 9 * * *", goal="Daily digest")
        assert a.id == b.id
        # No second JSON file.
        files = list((tmp_path / "runtime" / "schedules").glob("*.json"))
        assert len(files) == 1

    def test_add_validates_cron(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        with pytest.raises(CronParseError):
            store.add(cron="* * *", goal="x")  # invalid

    def test_add_requires_goal_or_scenario(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        with pytest.raises(ValueError):
            store.add(cron="0 9 * * *", goal="", scenario_id="")

    def test_add_with_catch_up_policy(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="Daily", misfire_policy="catch_up")
        assert schedule.misfire_policy == "catch_up"

    def test_add_rejects_unknown_policy(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        with pytest.raises(ValueError):
            store.add(cron="0 9 * * *", goal="x", misfire_policy="warp_speed")  # type: ignore[arg-type]


class TestScheduleStoreList:
    def test_list_empty(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        assert store.list() == []

    def test_list_returns_sorted_by_id(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        a = store.add(cron="0 9 * * *", goal="Alpha")
        b = store.add(cron="0 10 * * *", goal="Bravo")
        result = store.list()
        ids = [s.id for s in result]
        assert ids == sorted(ids)
        assert {a.id, b.id} == set(ids)


class TestScheduleStoreRemove:
    def test_remove_existing(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="x")
        assert store.remove(schedule.id) is True
        assert store.get(schedule.id) is None

    def test_remove_missing_returns_false(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        assert store.remove("sched_does_not_exist") is False


class TestScheduleStoreUpdateLastFire:
    def test_update_last_fire(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="x")
        store.update_last_fire(schedule.id, 1_700_000_000.0)
        reloaded = store.get(schedule.id)
        assert reloaded is not None
        assert reloaded.last_fire_at == pytest.approx(1_700_000_000.0)

    def test_concurrent_fire_updates_both_commit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="Daily digest")
        target = store.directory / f"{schedule.id}.json"
        fire_time = 1_700_000_000.0
        second = ScheduleStore(tmp_path)
        _overlap_writes(
            monkeypatch,
            target,
            lambda: store.update_last_fire(schedule.id, fire_time),
            lambda: second.update_last_fire(schedule.id, fire_time),
        )

        reloaded = store.get(schedule.id)
        assert reloaded is not None
        assert reloaded.last_fire_at == fire_time
        assert list(store.directory.glob("*.json*")) == [target]

    @pytest.mark.parametrize("use_case_alias", [False, True], ids=["canonical-id", "case-alias"])
    def test_concurrent_fire_and_cursor_updates_preserve_both_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_case_alias: bool
    ) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="Daily digest")
        target = store.directory / f"{schedule.id}.json"
        cursor_id = schedule.id.upper() if use_case_alias else schedule.id
        second = ScheduleStore(tmp_path)
        if second.get(cursor_id) is None:
            pytest.skip("schedule directory is case-sensitive")

        fire_time = 1_700_000_000.0
        cursor_time = fire_time + 60
        _overlap_writes(
            monkeypatch,
            target,
            lambda: store.update_last_fire(schedule.id, fire_time),
            lambda: second.advance_fire_cursor(cursor_id, cursor_time),
        )

        reloaded = store.get(schedule.id)
        assert reloaded is not None
        assert (reloaded.last_fire_at, reloaded.extra.get(FIRE_CURSOR_KEY)) == (fire_time, cursor_time)


class TestScheduleStoreLoadCorrupt:
    def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        bad = tmp_path / "runtime" / "schedules" / "sched_corrupt.json"
        bad.write_text("{not json")
        # ``list()`` should swallow the corrupt file instead of raising.
        assert store.list() == []
        assert store.get("sched_corrupt") is None
