"""Tests for the hash-chained quarantine journal (#5108).

The journal wraps ``WorkLedger``; these tests exercise the wrapper's own
adapter behaviour (mapping ledger kind/payload back to the quarantine event
shape) plus the properties chernistry's review specifically named: the whole
payload is hashed (not four enumerated fields), a corrupted row is reported
by ``verify()`` rather than silently truncating ``read()``, and the public
``path`` property replaces reaching into a private attribute.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.quarantine_journal import (
    GENESIS_HASH,
    QuarantineJournal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def journal(tmp_path: Path) -> QuarantineJournal:
    return QuarantineJournal(tmp_path / "quarantine-journal")


# ---------------------------------------------------------------------------
# Empty / initial state
# ---------------------------------------------------------------------------


class TestEmptyJournal:
    """Behaviour before any entries are appended."""

    def test_read_returns_empty_when_file_absent(self, journal: QuarantineJournal) -> None:
        assert journal.read() == []

    def test_verify_returns_empty_when_file_absent(self, journal: QuarantineJournal) -> None:
        assert journal.verify() == []

    def test_constructing_a_journal_creates_nothing_on_disk(self, journal: QuarantineJournal, tmp_path: Path) -> None:
        """A caller that only ever reads must never create the ledger directory."""
        journal.read()
        journal.verify()
        assert not (tmp_path / "quarantine-journal").exists()


# ---------------------------------------------------------------------------
# Appending entries
# ---------------------------------------------------------------------------


class TestAppend:
    """record_quarantined / record_released / record_expired."""

    def test_record_quarantined_creates_the_ledger_file(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("agent-x", reason="failed 3 times", timestamp="2025-01-01T00:00:00+00:00")
        assert journal.path.exists()

    def test_first_entry_prev_hash_is_genesis(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        entry = journal.read()[0]
        assert entry.prev_hash == GENESIS_HASH

    def test_second_entry_prev_hash_links_to_first(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="first", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="second", timestamp="2025-01-02T00:00:00+00:00")
        entries = journal.read()
        assert entries[1].prev_hash == entries[0].entry_hash

    def test_entries_carry_a_monotonic_sequence_number(self, journal: QuarantineJournal) -> None:
        """The index a gap or truncation would break -- absent from the pre-#5108-fix chain."""
        journal.record_quarantined("t", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        journal.record_expired("t", reason="e", timestamp="2025-01-03T00:00:00+00:00")
        assert [e.seq for e in journal.read()] == [0, 1, 2]

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
        lines = [line for line in journal.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

    def test_auto_timestamp_is_set_when_empty(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="r")
        entry = journal.read()[0]
        assert entry.timestamp

    def test_record_returns_the_appended_entry(self, journal: QuarantineJournal) -> None:
        entry = journal.record_quarantined("t", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        assert entry.task_title == "t"
        assert entry.event_type == "quarantined"
        assert entry == journal.read()[0]


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
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[0])
        data["entry_hash"] = "0" * 64
        journal.path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        assert journal.verify()

    def test_tampered_task_title_detected(self, journal: QuarantineJournal) -> None:
        """The whole payload is hashed now, so a nested field is covered too."""
        journal.record_quarantined("original", reason="r", timestamp="2025-01-01T00:00:00+00:00")
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[0])
        data["payload"]["task_title"] = "tampered"
        journal.path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        assert journal.verify()

    def test_tampered_reason_detected(self, journal: QuarantineJournal) -> None:
        """The exact gap chernistry's review named: `reason` was outside the old hash."""
        journal.record_quarantined("t", reason="failed 3 times", timestamp="2025-01-01T00:00:00+00:00")
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[0])
        data["payload"]["reason"] = "operator reset"
        journal.path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        assert journal.verify()

    def test_broken_link_between_entries_detected(self, journal: QuarantineJournal) -> None:
        journal.record_quarantined("t", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("t", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        data = json.loads(lines[1])
        data["prev_hash"] = "0" * 64
        journal.path.write_text(
            lines[0] + "\n" + json.dumps(data) + "\n",
            encoding="utf-8",
        )
        assert journal.verify()

    def test_a_corrupted_middle_line_is_reported_not_silently_truncated(self, journal: QuarantineJournal) -> None:
        """The blocking gap in the pre-fix chain: read() used to `break` on any bad line.

        A single corrupted byte in the middle of the file used to make
        ``read()`` return only the entries before it and ``verify()`` report
        a clean chain -- a one-byte edit passing as an undetected loss of
        every record after it. ``verify()`` must name this as an error, and
        ``read()`` must still surface the entries that follow.
        """
        journal.record_quarantined("first", reason="q", timestamp="2025-01-01T00:00:00+00:00")
        journal.record_released("first", reason="r", timestamp="2025-01-02T00:00:00+00:00")
        journal.record_expired("first", reason="e", timestamp="2025-01-03T00:00:00+00:00")
        lines = journal.path.read_text(encoding="utf-8").splitlines()
        lines[1] = '{"broken": "not a real ledger row"'  # torn middle line, not JSON
        journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        errors = journal.verify()
        assert errors, "a corrupted middle line must be reported, not silently accepted as a clean chain"

        # The entry after the corruption is still readable -- corruption in
        # one row must not hide the ones that follow it.
        remaining = journal.read()
        assert "expired" in [e.event_type for e in remaining]


# ---------------------------------------------------------------------------
# GENESIS_HASH constant
# ---------------------------------------------------------------------------


def test_genesis_hash_is_64_hex_zeros() -> None:
    assert len(GENESIS_HASH) == 64
    assert all(c == "0" for c in GENESIS_HASH)
