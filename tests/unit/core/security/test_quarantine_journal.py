"""Tests for the hash-chained quarantine journal (#5108)."""

from __future__ import annotations

import json

import pytest

from bernstein.core.security.quarantine_journal import (
    GENESIS_HASH,
    QuarantineJournal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def journal(tmp_path: pytest.TempdirFixture) -> QuarantineJournal:
    return QuarantineJournal(tmp_path / "quarantine-journal.jsonl")


# ---------------------------------------------------------------------------
# Empty / initial state
# ---------------------------------------------------------------------------


class TestEmptyJournal:
    """Behaviour before any entries are appended."""

    def test_read_returns_empty_when_file_absent(self, journal: QuarantineJournal) -> None:
        assert journal.read() == []

    def test_verify_returns_empty_when_file_absent(self, journal: QuarantineJournal) -> None:
        assert journal.verify() == []


# ---------------------------------------------------------------------------
# Appending entries
# ---------------------------------------------------------------------------


class TestAppend:
    """record_quarantined / record_released / record_expired."""

    def test_record_quarantined_creates_file(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("agent-x", reason="failed 3 times", timestamp="2025-01-01T00:00:00+00:00")
        assert journal._path.exists()

    def test_first_entry_prev_hash_is_genesis(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        entry = journal.read()[0]
        assert entry.prev_hash == GENESIS_HASH

    def test_second_entry_prev_hash_links_to_first(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="first", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="second", timestamp="2025-01-02T00:00:00+00:00")
        entries = journal.read()
        assert entries[1].prev_hash == entries[0].entry_hash

    def test_three_event_types_appended(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        journal.record_expired("t", reason="e", timestamp="2025-01-03T00:00:00+00:00")
        entries = journal.read()
        assert [e.event_type for e in entries] == ["quarantined", "released", "expired"]

    def test_entry_fields_persisted(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("my-task", reason="threshold exceeded", timestamp="2025-06-01T12:00:00+00:00")
        entry = journal.read()[0]
        assert entry.event_type == "quarantined"
        assert entry.task_title == "my-task"
        assert entry.reason == "threshold exceeded"
        assert entry.timestamp == "2025-06-01T12:00:00+00:00"

    def test_file_is_valid_jsonl(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="ok", timestamp="2025-01-02T00:00:00+00:00")
        lines = [l for l in journal._path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for line in lines:
            json.loads(line)

    def test_auto_timestamp_is_set_when_empty(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r")
        entry = journal.read()[0]
        assert entry.timestamp


# ---------------------------------------------------------------------------
# verify -- hash chain integrity
# ---------------------------------------------------------------------------


class TestVerify:
    """verify() reports chain errors."""

    def test_valid_single_entry_verifies(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        assert journal.verify() == []

    def test_valid_chain_of_three_verifies(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        journal.record_expired("t", reason="e", timestamp="2025-01-03T00:00:00+00:00")
        assert journal.verify() == []

    def test_tampered_entry_hash_detected(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        lines = journal._path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[0])
        data["entry_hash"] = "0" * 64
        journal._path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        errors = journal.verify()
        assert errors

    def test_tampered_task_title_detected(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("original", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        lines = journal._path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[0])
        data["task_title"] = "tampered"
        journal._path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        errors = journal.verify()
        assert errors

    def test_broken_link_between_entries_detected(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        lines = journal._path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[1])
        data["prev_hash"] = "0" * 64
        journal._path.write_text(
            lines[0] + "\n" + json.dumps(data) + "\n",
            encoding="utf-8",
        )
        errors = journal.verify()
        assert errors


# ---------------------------------------------------------------------------
# GENESIS_HASH constant
# ---------------------------------------------------------------------------


def test_genesis_hash_is_64_hex_zeros() -> None:
    assert len(GENESIS_HASH) == 64
    assert all(c == "0" for c in GENESIS_HASH)
