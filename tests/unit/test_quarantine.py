"""Tests for cross-run task quarantine.

Covers:
- QuarantineStore CRUD (load/save/record/clear)
- Auto-quarantine after 3 failures
- Expired entries (>7 days) are treated as not quarantined
- is_quarantined returns False for unknown tasks
- clear() by title removes one entry; clear() all removes everything
- get_all() returns only active (non-expired) entries
"""

from __future__ import annotations

import builtins
import json
import logging
import os
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from bernstein.core.quarantine import QUARANTINE_THRESHOLD, QuarantineEntry, QuarantineStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> QuarantineStore:
    return QuarantineStore(tmp_path / "quarantine.json")


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# QUARANTINE_THRESHOLD constant
# ---------------------------------------------------------------------------


def test_quarantine_threshold_is_three() -> None:
    assert QUARANTINE_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Load / save round-trip
# ---------------------------------------------------------------------------


def test_load_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() == []


def test_load_returns_entries_from_file(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    data = [
        {
            "task_title": "519 - Distributed cluster mode",
            "fail_count": 3,
            "last_failure": _today(),
            "reason": "Agent died; no files modified",
            "action": "skip",
        }
    ]
    path.write_text(json.dumps(data))
    store = QuarantineStore(path)
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].task_title == "519 - Distributed cluster mode"
    assert entries[0].fail_count == 3
    assert entries[0].action == "skip"


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = QuarantineEntry(
        task_title="533 - WASM fast-path",
        fail_count=3,
        last_failure=_today(),
        reason="Scope too large",
        action="decompose",
    )
    store.save([entry])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].task_title == "533 - WASM fast-path"
    assert loaded[0].action == "decompose"


# ---------------------------------------------------------------------------
# record_failure
# ---------------------------------------------------------------------------


def test_record_failure_increments_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_failure("task A", "Agent died")
    store.record_failure("task A", "Agent died again")
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].fail_count == 2
    assert entries[0].task_title == "task A"


def test_record_failure_quarantines_after_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("heavy task", "No files modified")
    assert store.is_quarantined("heavy task")


def test_record_failure_not_quarantined_below_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD - 1):
        store.record_failure("light task", "minor failure")
    assert not store.is_quarantined("light task")


def test_record_failure_updates_reason_and_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_failure("task B", "first reason")
    store.record_failure("task B", "second reason")
    entry = store.get_entry("task B")
    assert entry is not None
    assert entry.reason == "second reason"
    assert entry.last_failure == _today()


def test_record_failure_separate_tasks_tracked_independently(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task X", "fail X")
    store.record_failure("task Y", "fail Y")
    assert store.is_quarantined("task X")
    assert not store.is_quarantined("task Y")


# ---------------------------------------------------------------------------
# is_quarantined / get_entry
# ---------------------------------------------------------------------------


def test_is_quarantined_false_for_unknown_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not store.is_quarantined("nonexistent task")


def test_get_entry_returns_none_for_unknown_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_entry("nonexistent") is None


def test_is_quarantined_false_after_7_days_expiry(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    data = [
        {
            "task_title": "old task",
            "fail_count": 3,
            "last_failure": _days_ago(8),
            "reason": "old reason",
            "action": "skip",
        }
    ]
    path.write_text(json.dumps(data))
    store = QuarantineStore(path)
    assert not store.is_quarantined("old task")


def test_is_quarantined_true_within_7_days(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    data = [
        {
            "task_title": "recent task",
            "fail_count": 3,
            "last_failure": _days_ago(6),
            "reason": "reason",
            "action": "skip",
        }
    ]
    path.write_text(json.dumps(data))
    store = QuarantineStore(path)
    assert store.is_quarantined("recent task")


# ---------------------------------------------------------------------------
# get_all (active entries only)
# ---------------------------------------------------------------------------


def test_get_all_returns_only_active_entries(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    data = [
        {
            "task_title": "active task",
            "fail_count": 3,
            "last_failure": _days_ago(2),
            "reason": "reason",
            "action": "skip",
        },
        {
            "task_title": "expired task",
            "fail_count": 4,
            "last_failure": _days_ago(10),
            "reason": "old reason",
            "action": "skip",
        },
    ]
    path.write_text(json.dumps(data))
    store = QuarantineStore(path)
    active = store.get_all()
    assert len(active) == 1
    assert active[0].task_title == "active task"


def test_get_all_returns_empty_when_all_expired(tmp_path: Path) -> None:
    path = tmp_path / "quarantine.json"
    data = [
        {
            "task_title": "task",
            "fail_count": 3,
            "last_failure": _days_ago(8),
            "reason": "r",
            "action": "skip",
        }
    ]
    path.write_text(json.dumps(data))
    store = QuarantineStore(path)
    assert store.get_all() == []


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_by_title_removes_one_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task to clear", "fail")
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task to keep", "fail")
    store.clear("task to clear")
    assert not store.is_quarantined("task to clear")
    assert store.is_quarantined("task to keep")


def test_clear_all_empties_quarantine(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task A", "fail")
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task B", "fail")
    store.clear()
    assert store.load() == []


def test_clear_nonexistent_title_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("task", "fail")
    store.clear("does not exist")
    assert store.is_quarantined("task")


# ---------------------------------------------------------------------------
# get_action
# ---------------------------------------------------------------------------


def test_get_action_returns_skip_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("complex task", "agent died")
    entry = store.get_entry("complex task")
    assert entry is not None
    assert entry.action == "skip"


def test_get_action_returns_none_for_unknown_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_entry("unknown") is None


# ---------------------------------------------------------------------------
# The write is crash-safe
# ---------------------------------------------------------------------------
#
# ``load`` reads an unparseable file as an empty list - every quarantined task
# becomes eligible again. That is the permissive answer, and it is reachable
# from the store's own writes: a plain ``write_text`` truncates the
# destination before writing it, so a crash between those two steps leaves
# zero bytes where the quarantine set used to be. The next run reschedules
# every known-bad task, with a single log line as the only trace.
#
# ``write_atomic_json`` writes a temporary file, fsyncs it, and renames it
# over the destination, so a reader sees the previous set or the new one and
# never neither.


def test_the_destination_is_never_opened_for_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant that makes the write atomic, asserted directly.

    An in-place write has to truncate the destination; a rename never touches
    it. So "was the destination ever opened for writing" separates the two
    implementations exactly, without needing to catch the crash window in the
    act.
    """
    store_path = tmp_path / "quarantine.json"
    opened_for_write: list[str] = []

    real_open = os.open

    def spy(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR):
            opened_for_write.append(str(path))
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    real_builtin_open = builtins.open

    def builtin_spy(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            opened_for_write.append(str(file))
        return real_builtin_open(file, mode, *args, **kwargs)  # type: ignore[arg-type, call-overload]

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(builtins, "open", builtin_spy)

    QuarantineStore(store_path).save([QuarantineEntry("t", 1, date.today().isoformat(), "boom")])

    assert str(store_path) not in opened_for_write
    assert opened_for_write, "nothing was opened for writing at all - the spy is not wired up"


def test_the_write_is_flushed_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename that outlives its own bytes is not a crash-safe write."""
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    QuarantineStore(tmp_path / "quarantine.json").save([QuarantineEntry("t", 1, date.today().isoformat(), "boom")])

    assert synced, "quarantine state was published without being fsynced"


def test_a_save_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    store_path = tmp_path / "quarantine.json"
    store = QuarantineStore(store_path)
    store.save([QuarantineEntry("t", 1, date.today().isoformat(), "boom")])
    store.save([QuarantineEntry("t", 2, date.today().isoformat(), "boom again")])

    assert sorted(p.name for p in tmp_path.iterdir()) == ["quarantine.json"]


def test_a_rewrite_replaces_the_previous_contents(tmp_path: Path) -> None:
    """``Path.rename`` refuses an existing destination on Windows.

    Delegating must not turn the second save of a run into a crash there.
    """
    store_path = tmp_path / "quarantine.json"
    store = QuarantineStore(store_path)
    today = date.today().isoformat()
    store.save([QuarantineEntry("first", 1, today, "a")])
    store.save([QuarantineEntry("second", 2, today, "b")])

    assert [e.task_title for e in store.load()] == ["second"]


def test_a_truncated_file_releases_every_quarantined_task(tmp_path: Path) -> None:
    """The consequence the atomic write exists to prevent, spelled out.

    This is documentation rather than a regression: it passes either way. It
    is what a torn write costs, and it is why ``load`` degrading to ``[]``
    makes the write path the thing that has to be safe.
    """
    store_path = tmp_path / "quarantine.json"
    store = QuarantineStore(store_path)
    for _ in range(QUARANTINE_THRESHOLD):
        store.record_failure("known bad task", "boom")
    assert store.is_quarantined("known bad task")

    intact = store_path.read_text()
    store_path.write_text(intact[: len(intact) // 2])

    assert not QuarantineStore(store_path).is_quarantined("known bad task")


def test_an_unreadable_file_is_logged_as_an_error_naming_what_was_lost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A WARNING understated it: the whole quarantine set stops applying."""
    store_path = tmp_path / "quarantine.json"
    store_path.write_text("{ not json")

    with caplog.at_level(logging.ERROR, logger="bernstein.core.security.quarantine"):
        assert QuarantineStore(store_path).load() == []

    assert "treating every task as not quarantined" in caplog.text
