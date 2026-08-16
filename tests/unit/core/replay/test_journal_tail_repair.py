"""Repair of a crash-torn journal tail (#3910).

A crash partway through an append leaves a truncated final line with no
trailing newline. ``EventJournal.resume`` refuses such a journal (its
tolerant read discarded the physical line), and with no repair path the
task is unresumable for good. These tests pin the repair contract:

* truncate the torn tail and nothing else (byte-for-byte prefix);
* leave the surviving chain head untouched;
* make ``resume`` succeed again;
* refuse a discard in the middle (that is corruption, not a torn write);
* refuse against a disagreeing seal *before* writing;
* report a no-op on a clean journal.
"""

from __future__ import annotations

import pytest

from bernstein.core.replay.journal import (
    EventJournal,
    JournalSeal,
    load_events,
    repair_journal_tail,
)


def _torn_journal(tmp_path: pytest.TempPathFactory, n: int = 4) -> tuple[object, EventJournal]:
    journal = EventJournal("torn-run", tmp_path / ".sdd")
    for index in range(n):
        journal.record("step", value=index)
    path = journal.path
    # Crash partway through appending: a truncated final line with no
    # trailing newline (exactly the shape the issue reproduces).
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event": "step_started", "n": 3, "prev_ha')
    return path, journal


def test_repair_truncates_only_the_torn_tail(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    original = path.read_bytes()
    tail_start = original.rfind(b"\n") + 1
    prefix = original[:tail_start]

    result = repair_journal_tail(path)

    assert result.repaired
    assert path.read_bytes() == prefix


def test_head_is_unchanged_by_repair(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    events_before = load_events(path).events
    before_head = events_before[-1]["event_hash"]

    repair_journal_tail(path)

    events_after = load_events(path).events
    assert events_after[-1]["event_hash"] == before_head
    assert events_after == events_before


def test_resume_succeeds_after_repair(tmp_path) -> None:
    path, journal = _torn_journal(tmp_path)
    with pytest.raises(ValueError, match=r"discarded physical line"):
        EventJournal.resume("torn-run", tmp_path / ".sdd")

    repair_journal_tail(path)

    resumed = EventJournal.resume("torn-run", tmp_path / ".sdd")
    assert resumed.event_count() == journal.event_count()
    assert resumed.head() == journal.head()


def test_resume_refusal_names_the_repair_path_for_a_torn_tail(tmp_path) -> None:
    _path, _ = _torn_journal(tmp_path)

    with pytest.raises(ValueError, match=r"repair"):
        EventJournal.resume("torn-run", tmp_path / ".sdd")


def test_a_discard_in_the_middle_is_refused(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(2, "not json\n")
    path.write_text("".join(lines), encoding="utf-8")
    poisoned = path.read_bytes()

    with pytest.raises(ValueError, match=r"middle|corruption"):
        repair_journal_tail(path)

    assert path.read_bytes() == poisoned


def test_resume_refusal_does_not_name_repair_for_a_middle_discard(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(2, "not json\n")
    path.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        EventJournal.resume("torn-run", tmp_path / ".sdd")

    assert "repair" not in str(exc_info.value)


def test_repair_against_a_disagreeing_seal_is_refused_before_writing(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    original = path.read_bytes()
    seal = JournalSeal(head="not-the-head", event_count=999)

    with pytest.raises(ValueError, match=r"seal"):
        repair_journal_tail(path, seal=seal)

    # The evidence must survive: the call raised *and* the file is untouched.
    assert path.read_bytes() == original


def test_repair_of_a_clean_journal_reports_no_op(tmp_path) -> None:
    path, _ = _torn_journal(tmp_path)
    repair_journal_tail(path)
    clean = path.read_bytes()

    result = repair_journal_tail(path)

    assert not result.repaired
    assert path.read_bytes() == clean
