"""Unit tests for the operator-registered schedule store (#1798)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest

from bernstein.core.planning.schedule_store import (
    CronParseError,
    ScheduleStore,
    _canonical_schedule_id,
    parse_cron,
    validate_cron,
)

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
        ready = Barrier(2, timeout=5)
        rename_lock = Lock()
        real_replace = os.replace

        def replace_together(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if Path(dst) == target:
                # Both writers have staged their payloads before either publishes.
                ready.wait()
                with rename_lock:
                    real_replace(src, dst)
            else:
                real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_together)
        fire_time = 1_700_000_000.0
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(store.update_last_fire, schedule.id, fire_time)
            second = pool.submit(ScheduleStore(tmp_path).update_last_fire, schedule.id, fire_time)
            first.result(timeout=10)
            second.result(timeout=10)

        reloaded = store.get(schedule.id)
        assert reloaded is not None
        assert reloaded.last_fire_at == fire_time
        assert list(store.directory.iterdir()) == [target]


class TestScheduleStoreLoadCorrupt:
    def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        bad = tmp_path / "runtime" / "schedules" / "sched_corrupt.json"
        bad.write_text("{not json")
        # ``list()`` should swallow the corrupt file instead of raising.
        assert store.list() == []
        assert store.get("sched_corrupt") is None
