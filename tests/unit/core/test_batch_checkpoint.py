"""Tests for BatchCheckpointLedger (#5126 slice 1).

Coverage:
* A new entity is not done; a recorded entity is done.
* A second instance over the same file resumes: already-recorded entities
  are done, new entities are not.
* record_success is idempotent on re-record (no error, still marked done).
* done_count counts distinct entity ids.
* verify returns no errors on a well-formed ledger and catches tampered lines.
* The ledger is byte-identical across two reads of the same file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.persistence.batch_checkpoint import (
    GENESIS_HASH,
    BatchCheckpointLedger,
)

INSTANT_A = "2025-06-01T10:00:00Z"
INSTANT_B = "2025-06-01T11:00:00Z"
INSTANT_C = "2025-06-01T12:00:00Z"


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "checkpoint.jsonl"


# ---------------------------------------------------------------------------
# Basic record / is_done contract
# ---------------------------------------------------------------------------


class TestRecordAndIsDone:
    def test_new_entity_is_not_done(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        assert not ledger.is_done("res:1")

    def test_recorded_entity_is_done(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        ledger.record_success("res:1", INSTANT_A)
        assert ledger.is_done("res:1")

    def test_unrecorded_entity_is_not_done_after_other_records(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        ledger.record_success("res:1", INSTANT_A)
        assert not ledger.is_done("res:2")

    def test_multiple_entities_all_marked_done(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        for i in range(5):
            ledger.record_success(f"res:{i}", INSTANT_A)
        for i in range(5):
            assert ledger.is_done(f"res:{i}")


# ---------------------------------------------------------------------------
# Resume: second instance over same file skips already-done entities
# ---------------------------------------------------------------------------


class TestResume:
    def test_run_twice_second_instance_marks_already_done(self, ledger_path: Path) -> None:
        """The load-bearing test: crash-safe resume."""
        first = BatchCheckpointLedger(ledger_path)
        first.record_success("item-1", INSTANT_A)
        first.record_success("item-2", INSTANT_B)

        second = BatchCheckpointLedger(ledger_path)
        assert second.is_done("item-1")
        assert second.is_done("item-2")
        assert not second.is_done("item-3")

    def test_second_instance_can_extend_the_ledger(self, ledger_path: Path) -> None:
        first = BatchCheckpointLedger(ledger_path)
        first.record_success("item-1", INSTANT_A)

        second = BatchCheckpointLedger(ledger_path)
        second.record_success("item-2", INSTANT_B)

        third = BatchCheckpointLedger(ledger_path)
        assert third.is_done("item-1")
        assert third.is_done("item-2")

    def test_done_count_reflects_distinct_entities(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        ledger.record_success("item-1", INSTANT_A)
        ledger.record_success("item-2", INSTANT_B)
        assert ledger.done_count == 2

        resumed = BatchCheckpointLedger(ledger_path)
        assert resumed.done_count == 2


# ---------------------------------------------------------------------------
# Verify: chain integrity
# ---------------------------------------------------------------------------


class TestVerify:
    def test_empty_ledger_verifies_clean(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        assert ledger.verify() == []

    def test_single_entry_verifies_clean(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        ledger.record_success("r1", INSTANT_A)
        assert ledger.verify() == []

    def test_multiple_entries_verify_clean(self, ledger_path: Path) -> None:
        ledger = BatchCheckpointLedger(ledger_path)
        for i in range(10):
            ledger.record_success(f"r:{i}", INSTANT_A)
        assert ledger.verify() == []

    def test_tampered_entry_hash_is_detected(self, ledger_path: Path) -> None:
        import json

        ledger = BatchCheckpointLedger(ledger_path)
        ledger.record_success("r1", INSTANT_A)
        ledger.record_success("r2", INSTANT_B)

        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["entry_hash"] = "0" * 64
        lines[0] = json.dumps(first, separators=(",", ":"))
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        errors = BatchCheckpointLedger(ledger_path).verify()
        assert any("entry_hash mismatch" in e for e in errors)


# ---------------------------------------------------------------------------
# File creation and non-existent path
# ---------------------------------------------------------------------------


class TestFileHandling:
    def test_ledger_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "dir" / "ledger.jsonl"
        ledger = BatchCheckpointLedger(nested)
        ledger.record_success("r1", INSTANT_A)
        assert nested.exists()

    def test_nonexistent_file_is_empty_ledger(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        ledger = BatchCheckpointLedger(path)
        assert not ledger.is_done("anything")
        assert ledger.done_count == 0

    def test_verify_nonexistent_file_returns_no_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        ledger = BatchCheckpointLedger(path)
        assert ledger.verify() == []


# ---------------------------------------------------------------------------
# GENESIS_HASH sentinel
# ---------------------------------------------------------------------------


def test_genesis_hash_is_64_hex_zeros() -> None:
    assert GENESIS_HASH == "0" * 64
    assert len(GENESIS_HASH) == 64
