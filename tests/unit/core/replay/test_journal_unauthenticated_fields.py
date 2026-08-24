"""Tests for JournalVerifyResult.unauthenticated_fields field."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.replay.journal import (
    _NON_DETERMINISTIC_FIELDS,
    EventJournal,
    JournalVerifyResult,
    load_events,
    verify_events,
    verify_journal,
)


def _expected_unauthenticated_fields() -> tuple[str, ...]:
    """Return the expected sorted tuple of unauthenticated field names."""
    return tuple(sorted(_NON_DETERMINISTIC_FIELDS))


def test_journal_verify_result_default_unauthenticated_fields() -> None:
    """JournalVerifyResult constructed with defaults has correct unauthenticated_fields."""
    result = JournalVerifyResult(
        chain_consistent=True,
        coverage="complete",
        identity="unverifiable",
        count=0,
    )
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_journal_intact_chain_has_unauthenticated_fields(tmp_path: Path) -> None:
    """verify_journal on consistent chain includes unauthenticated_fields."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-1", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-1")
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id="run-1")

    result = verify_journal(journal.path)

    assert result.chain_consistent is True
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_journal_divergent_chain_has_unauthenticated_fields(tmp_path: Path) -> None:
    """verify_journal on divergent chain includes unauthenticated_fields."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-2", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-2")
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id="run-2")

    # Tamper with the journal
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[2])
    row["task_id"] = "T-INJECTED"
    lines[2] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_journal(journal.path)

    assert result.chain_consistent is False
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_journal_missing_file_has_unauthenticated_fields(tmp_path: Path) -> None:
    """verify_journal on missing file includes unauthenticated_fields.

    A missing file loads as an empty event list, which verify_events treats
    as a consistent (empty) chain with complete coverage.
    """
    result = verify_journal(tmp_path / "missing" / "journal.jsonl")

    assert result.chain_consistent is True
    assert result.coverage.value == "complete"
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_events_empty_list_has_unauthenticated_fields() -> None:
    """verify_events on empty list includes unauthenticated_fields."""
    result = verify_events([])

    assert result.count == 0
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_events_consistent_list_has_unauthenticated_fields(tmp_path: Path) -> None:
    """verify_events on consistent list includes unauthenticated_fields."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-3", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-3")
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id="run-3")

    loaded = load_events(journal.path)
    result = verify_events(loaded.events)

    assert result.chain_consistent is True
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)


def test_verify_events_divergent_list_has_unauthenticated_fields(tmp_path: Path) -> None:
    """verify_events on divergent list includes unauthenticated_fields."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-4", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-4")
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1")
    journal.record("run_completed", run_id="run-4")

    loaded = load_events(journal.path)
    # Tamper with the events list directly
    events = loaded.events
    events[2]["task_id"] = "T-INJECTED"

    result = verify_events(events)

    assert result.chain_consistent is False
    assert result.unauthenticated_fields == _expected_unauthenticated_fields()
    assert isinstance(result.unauthenticated_fields, tuple)
