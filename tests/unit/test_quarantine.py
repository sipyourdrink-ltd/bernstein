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

import json
from datetime import date, timedelta
from pathlib import Path

from bernstein.core.quarantine import QUARANTINE_THRESHOLD, QuarantineEntry, QuarantineStore

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
            "task_title": "519 — Distributed cluster mode",
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
    assert entries[0].task_title == "519 — Distributed cluster mode"
    assert entries[0].fail_count == 3
    assert entries[0].action == "skip"


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = QuarantineEntry(
        task_title="533 — WASM fast-path",
        fail_count=3,
        last_failure=_today(),
        reason="Scope too large",
        action="decompose",
    )
    store.save([entry])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].task_title == "533 — WASM fast-path"
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
