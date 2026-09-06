"""Issue #5126: a batch's resume point and its daily cap come from one record.

Two properties that are really one. An item recorded as done is skipped on the
next pass, so a crash costs the item in flight and nothing before it. And the
daily cap is derived from the ledger's own entries rather than a counter in
memory -- because an in-memory count resets to zero on restart, which is exactly
the moment an operator most wants the cap to hold: a process that keeps dying
and retrying is the one that would blow through it.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from bernstein.core.persistence.batch_ledger import (
    GENESIS_HASH,
    BatchLedger,
    BatchLedgerError,
    DailyCapReached,
    compact,
)

if TYPE_CHECKING:
    from pathlib import Path

DAY = 86400.0


def _at(day: str, hour: int = 12) -> float:
    """A deterministic instant inside a named UTC day."""
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_an_absent_ledger_has_nothing_done(tmp_path: Path) -> None:
    """First run: everything is pending, and no file is created by reading."""
    ledger = BatchLedger(tmp_path)
    assert ledger.entries() == []
    assert ledger.pending(["a", "b"]) == ["a", "b"]
    assert ledger.head_hash() == GENESIS_HASH
    assert not ledger.path.exists()


def test_a_recorded_item_is_skipped_on_resume(tmp_path: Path) -> None:
    ledger = BatchLedger(tmp_path)
    ledger.record("a")
    ledger.record("c")

    assert ledger.pending(["a", "b", "c", "d"]) == ["b", "d"]


def test_pending_preserves_the_order_it_was_given(tmp_path: Path) -> None:
    """A batch's order is often deliberate; returning a set would discard it."""
    ledger = BatchLedger(tmp_path)
    ledger.record("m")

    assert ledger.pending(["z", "m", "a", "q"]) == ["z", "a", "q"]


def test_a_second_process_sees_the_first_ones_work(tmp_path: Path) -> None:
    """The resume path: a new instance reads the record, it does not inherit state."""
    BatchLedger(tmp_path).record("a")

    restarted = BatchLedger(tmp_path)
    assert restarted.pending(["a", "b"]) == ["b"]


def test_last_success_reports_the_most_recent_occurrence(tmp_path: Path) -> None:
    """An item re-processed after a policy change has two entries."""
    ledger = BatchLedger(tmp_path)
    ledger.record("a", at=100.0)
    ledger.record("a", at=200.0)

    assert ledger.last_success("a") == 200.0
    assert ledger.last_success("never-seen") is None


# ---------------------------------------------------------------------------
# The daily cap, derived from the ledger
# ---------------------------------------------------------------------------


def test_the_cap_survives_a_restart(tmp_path: Path) -> None:
    """The load-bearing case.

    A cap enforced from in-memory state resets to zero on every restart. Here
    the second process reads the first one's entries and refuses.
    """
    now = _at("2026-09-03")
    first = BatchLedger(tmp_path)
    for i in range(3):
        first.record(f"item-{i}", at=now, cap=3)

    restarted = BatchLedger(tmp_path)
    assert restarted.done_today(now=now) == 3
    with pytest.raises(DailyCapReached) as excinfo:
        restarted.record("item-3", at=now, cap=3)

    assert excinfo.value.cap == 3
    assert excinfo.value.done_today == 3
    # And nothing was written: a refused item is still pending.
    assert restarted.pending(["item-3"]) == ["item-3"]


def test_yesterdays_entries_do_not_count_against_today(tmp_path: Path) -> None:
    ledger = BatchLedger(tmp_path)
    yesterday = _at("2026-09-02")
    today = _at("2026-09-03")
    for i in range(5):
        ledger.record(f"old-{i}", at=yesterday)

    assert ledger.done_today(now=today) == 0
    assert ledger.remaining_today(3, now=today) == 3
    ledger.record("new", at=today, cap=3)
    assert ledger.done_today(now=today) == 1


def test_the_day_boundary_is_utc(tmp_path: Path) -> None:
    """A batch resumed in another timezone must not get a second day's budget."""
    ledger = BatchLedger(tmp_path)
    ledger.record("late", at=_at("2026-09-03", hour=23))
    ledger.record("early", at=_at("2026-09-03", hour=0))

    assert ledger.done_today(now=_at("2026-09-03", hour=6)) == 2


def test_a_lowered_cap_leaves_no_negative_budget(tmp_path: Path) -> None:
    """Work already recorded cannot be un-done by changing the number."""
    now = _at("2026-09-03")
    ledger = BatchLedger(tmp_path)
    for i in range(5):
        ledger.record(f"item-{i}", at=now)

    assert ledger.remaining_today(2, now=now) == 0
    with pytest.raises(DailyCapReached):
        ledger.record("item-5", at=now, cap=2)


def test_a_zero_cap_is_no_cap_on_record_and_no_budget_on_remaining(tmp_path: Path) -> None:
    ledger = BatchLedger(tmp_path)
    for i in range(10):
        ledger.record(f"item-{i}", cap=0)
    assert len(ledger.entries()) == 10
    assert ledger.remaining_today(0) == 0


def test_concurrent_records_cannot_both_take_the_last_slot(tmp_path: Path) -> None:
    """The check and the append are one section.

    Two threads reading ``cap - 1`` and both writing is the failure a cap
    derived from a log is supposed to make impossible.
    """
    now = _at("2026-09-03")
    ledger = BatchLedger(tmp_path)
    ledger.record("already", at=now)

    accepted: list[str] = []
    refused: list[str] = []

    def _try(entity_id: str) -> None:
        try:
            ledger.record(entity_id, at=now, cap=2)
            accepted.append(entity_id)
        except DailyCapReached:
            refused.append(entity_id)

    threads = [threading.Thread(target=_try, args=(f"item-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 1, f"{len(accepted)} callers took the last slot"
    assert len(refused) == 7
    assert ledger.done_today(now=now) == 2


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def test_the_chain_verifies_and_links_each_entry_to_the_last(tmp_path: Path) -> None:
    ledger = BatchLedger(tmp_path)
    first = ledger.record("a")
    second = ledger.record("b")

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.entry_hash
    assert ledger.head_hash() == second.entry_hash
    ledger.verify()


def test_an_edited_entry_fails_verification(tmp_path: Path) -> None:
    """The point of chaining: a record cannot be quietly rewritten."""
    ledger = BatchLedger(tmp_path)
    ledger.record("a", detail={"outcome": "sent"})
    ledger.record("b")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["detail"] = {"outcome": "not sent"}
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    ledger.path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    with pytest.raises(BatchLedgerError):
        BatchLedger(tmp_path).verify()


def test_a_reordered_ledger_fails_verification(tmp_path: Path) -> None:
    ledger = BatchLedger(tmp_path)
    ledger.record("a")
    ledger.record("b")

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text(f"{lines[1]}\n{lines[0]}\n", encoding="utf-8")

    with pytest.raises(BatchLedgerError):
        BatchLedger(tmp_path).verify()


def test_a_torn_final_line_is_dropped_not_fatal(tmp_path: Path) -> None:
    """A process killed mid-append leaves a partial record.

    Refusing to load the ledger over its last byte would turn a crash into an
    outage -- every completed item would be reprocessed.
    """
    ledger = BatchLedger(tmp_path)
    ledger.record("a")
    ledger.record("b")
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"entity_id": "c", "at": 1')

    resumed = BatchLedger(tmp_path)
    assert resumed.pending(["a", "b", "c"]) == ["c"]
    resumed.verify()


def test_a_break_in_the_middle_is_corruption_and_is_reported(tmp_path: Path) -> None:
    """Only the LAST line may be torn. A hole anywhere else is not a crash."""
    ledger = BatchLedger(tmp_path)
    ledger.record("a")
    ledger.record("b")
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text(f"{lines[0]}\nnot json at all\n{lines[1]}\n", encoding="utf-8")

    with pytest.raises(BatchLedgerError):
        BatchLedger(tmp_path).entries()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_compaction_drops_old_entries_and_re_derives_the_chain(tmp_path: Path) -> None:
    """A chain with a hole in it verifies as tampered.

    That is the right reading of a file somebody edited and the wrong one for a
    retention policy the operator asked for, so the chain is re-derived.
    """
    now = time.time()
    ledger = BatchLedger(tmp_path)
    ledger.record("ancient", at=now - 30 * DAY)
    ledger.record("old", at=now - 10 * DAY)
    ledger.record("recent", at=now)

    assert compact(ledger, keep_days=7, now=now) == 2

    assert [entry.entity_id for entry in ledger.entries()] == ["recent"]
    assert ledger.entries()[0].prev_hash == GENESIS_HASH
    ledger.verify()


def test_compaction_is_a_no_op_when_nothing_is_old_enough(tmp_path: Path) -> None:
    now = time.time()
    ledger = BatchLedger(tmp_path)
    ledger.record("recent", at=now)
    before = ledger.path.read_bytes()

    assert compact(ledger, keep_days=7, now=now) == 0
    assert ledger.path.read_bytes() == before


def test_compaction_leaves_no_scratch_file(tmp_path: Path) -> None:
    now = time.time()
    ledger = BatchLedger(tmp_path)
    ledger.record("ancient", at=now - 30 * DAY)
    ledger.record("recent", at=now)

    compact(ledger, keep_days=1, now=now)
    assert not list(tmp_path.glob("*.tmp"))


def test_compaction_rejects_a_negative_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep_days"):
        compact(BatchLedger(tmp_path), keep_days=-1)


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_the_ledger_filename_cannot_escape_its_directory(tmp_path: Path) -> None:
    """The same barrier the work ledger applies to its bucket."""
    with pytest.raises(Exception, match="(?i)contain|escape|outside|path"):
        BatchLedger(tmp_path, filename="../escaped.jsonl")
