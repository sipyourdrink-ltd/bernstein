"""Schedule store parameter block (#2545, AC5).

Params ride next to cron and goal on the Schedule dataclass. A schedule without
params keeps the exact id it had before params existed; a schedule with params
gets a distinct id and round-trips its params through disk.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.planning.schedule_store import Schedule, ScheduleStore, _canonical_schedule_id


def test_paramless_id_unchanged() -> None:
    # The id of a params-less schedule is byte-identical to the pre-#2545 id
    # (the params block folds nothing when empty).
    without_args = _canonical_schedule_id("0 9 * * *", "nightly", "")
    with_empty_args = _canonical_schedule_id("0 9 * * *", "nightly", "", [], {})
    assert without_args == with_empty_args


def test_params_change_id() -> None:
    a = _canonical_schedule_id("0 9 * * *", "nightly", "", [{"name": "t", "type": "string"}], {"t": "a"})
    b = _canonical_schedule_id("0 9 * * *", "nightly", "", [{"name": "t", "type": "string"}], {"t": "b"})
    plain = _canonical_schedule_id("0 9 * * *", "nightly", "")
    assert a != b
    assert a != plain


def test_add_persists_and_round_trips_params(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / ".sdd")
    schema = [{"name": "target", "type": "string", "required": True}]
    schedule = store.add(cron="0 9 * * *", goal="nightly", params_schema=schema, params={"target": "svc-a"})
    loaded = store.get(schedule.id)
    assert loaded is not None
    assert loaded.params_schema == schema
    assert loaded.params == {"target": "svc-a"}


def test_add_is_idempotent_with_params(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / ".sdd")
    schema = [{"name": "target", "type": "string"}]
    a = store.add(cron="0 9 * * *", goal="nightly", params_schema=schema, params={"target": "x"})
    b = store.add(cron="0 9 * * *", goal="nightly", params_schema=schema, params={"target": "x"})
    assert a.id == b.id


def test_paramless_add_keeps_legacy_id(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / ".sdd")
    schedule = store.add(cron="0 9 * * *", goal="nightly")
    assert schedule.id == _canonical_schedule_id("0 9 * * *", "nightly", "")
    assert schedule.params_schema == []
    assert schedule.params == {}


def test_malformed_schema_rejected_at_registration(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / ".sdd")
    # 'goal' is a reserved param name -> the schema shape is invalid.
    try:
        store.add(cron="0 9 * * *", goal="nightly", params_schema=[{"name": "goal", "type": "string"}])
    except ValueError:
        return
    raise AssertionError("expected a malformed schema to be rejected at registration")


def test_dataclass_defaults_are_empty() -> None:
    s = Schedule(id="x", cron="0 9 * * *", goal="g")
    assert s.params_schema == []
    assert s.params == {}
