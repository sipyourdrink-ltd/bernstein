"""Tests for the run-journal SSE thread projection (issue #2297).

The thread projection turns the canonical per-run event journal into an
ordered stream of hash-anchored SSE events. Each projected event carries
the journal entry's ``event_hash`` (AC2) and its monotonic index as the
SSE ``id`` so a dropped-and-reconnected client resumes from
``Last-Event-ID`` without missing or duplicating rows (AC5). The
projection is a pure function of the journal, so two independent
projections of the same journal are byte-identical (determinism).
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.replay.thread_projection import (
    ThreadStreamEvent,
    project_journal,
    verify_thread_against_journal,
)


def _journal(tmp_path: Path, run_id: str = "run-proj") -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=tmp_path)
    journal.record("tick_start", tick=0)
    journal.record("task_claimed", task_id="T-1", agent_id="A-1")
    journal.record("task_completed", task_id="T-1")
    return journal


def test_projected_event_carries_journal_entry_hash(tmp_path: Path) -> None:
    """Each streamed event carries its journal entry hash (AC2)."""
    journal = _journal(tmp_path)
    rows = load_events(journal.path)

    events = project_journal(journal.path)

    assert len(events) == len(rows)
    for row, event in zip(rows, events, strict=True):
        assert isinstance(event, ThreadStreamEvent)
        assert event.event_hash == row["event_hash"]
        assert event.journal_index == row["index"]
        assert event.journal_event == row["event"]
        # the SSE id is the monotonic journal index, enabling Last-Event-ID
        assert event.sse_id == str(row["index"])


def test_projection_is_deterministic(tmp_path: Path) -> None:
    """Two projections of the same journal are byte-identical (determinism)."""
    journal = _journal(tmp_path)

    first = [e.to_sse() for e in project_journal(journal.path)]
    second = [e.to_sse() for e in project_journal(journal.path)]

    assert first == second


def test_projection_after_index_resumes_without_gap(tmp_path: Path) -> None:
    """Projecting with ``after_index`` yields only newer rows (AC5).

    A client that saw up to index N reconnects with Last-Event-ID=N and
    must receive N+1.. with no gap and no duplicate.
    """
    journal = _journal(tmp_path)
    full = project_journal(journal.path)

    resumed = project_journal(journal.path, after_index=0)

    assert [e.journal_index for e in resumed] == [1, 2]
    # resumed rows are byte-identical to their counterparts in the full run
    assert [e.to_sse() for e in resumed] == [e.to_sse() for e in full[1:]]


def test_verify_thread_matches_journal(tmp_path: Path) -> None:
    """thread verify proves the projected thread equals the journal (AC3)."""
    journal = _journal(tmp_path)

    result = verify_thread_against_journal(journal.path)

    assert result.ok
    assert result.count == 3
    assert result.divergent_index is None


def test_verify_thread_detects_tamper(tmp_path: Path) -> None:
    """A tampered journal row surfaces as a thread-verify divergence (AC3)."""
    journal = _journal(tmp_path)
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    # Corrupt the payload of the middle row without fixing its hash.
    tampered = lines[1].replace("T-1", "T-9")
    lines[1] = tampered
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_thread_against_journal(journal.path)

    assert not result.ok
    assert result.divergent_index == 1


def test_empty_journal_projects_empty(tmp_path: Path) -> None:
    """A missing or empty journal projects no events and verifies ok."""
    missing = tmp_path / "nope" / "journal.jsonl"

    assert project_journal(missing) == []
    result = verify_thread_against_journal(missing)
    assert result.ok
    assert result.count == 0
