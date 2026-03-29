"""Tests for the WAL (Write-Ahead Log) module.

Tests cover:
- WALEntry immutability
- WALWriter hash chaining, fsync, persistence
- WALReader chain verification and tamper detection
- ExecutionFingerprint determinism
- WALRecovery uncommitted entry detection
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.wal import (
    GENESIS_HASH,
    ExecutionFingerprint,
    WALEntry,
    WALReader,
    WALRecovery,
    WALWriter,
)


class TestWALEntry:
    def test_entry_is_immutable(self) -> None:
        entry = WALEntry(
            seq=0,
            prev_hash=GENESIS_HASH,
            entry_hash="abc",
            timestamp=1.0,
            decision_type="task_created",
            inputs={},
            output={},
            actor="orc",
        )
        with pytest.raises((AttributeError, TypeError)):
            entry.seq = 1  # type: ignore[misc]

    def test_entry_fields_preserved(self) -> None:
        entry = WALEntry(
            seq=42,
            prev_hash="prev",
            entry_hash="hash",
            timestamp=99.9,
            decision_type="agent_spawned",
            inputs={"id": "a-001"},
            output={"status": "ok"},
            actor="spawner",
            committed=False,
        )
        assert entry.seq == 42
        assert entry.prev_hash == "prev"
        assert entry.entry_hash == "hash"
        assert entry.decision_type == "agent_spawned"
        assert entry.committed is False

    def test_committed_defaults_to_true(self) -> None:
        entry = WALEntry(
            seq=0,
            prev_hash=GENESIS_HASH,
            entry_hash="abc",
            timestamp=1.0,
            decision_type="task_created",
            inputs={},
            output={},
            actor="orc",
        )
        assert entry.committed is True


class TestWALWriter:
    def test_creates_wal_dir_and_file(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append(decision_type="task_created", inputs={}, output={}, actor="orc")
        wal_path = tmp_path / "runtime" / "wal" / "run-001.wal.jsonl"
        assert wal_path.exists()

    def test_first_entry_uses_genesis_hash(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e = writer.append(decision_type="task_created", inputs={}, output={}, actor="orc")
        assert e.prev_hash == GENESIS_HASH
        assert e.seq == 0

    def test_entries_form_hash_chain(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e0 = writer.append(decision_type="task_created", inputs={"id": "t-001"}, output={}, actor="orc")
        e1 = writer.append(decision_type="agent_spawned", inputs={"id": "a-001"}, output={}, actor="orc")
        e2 = writer.append(decision_type="task_completed", inputs={"id": "t-001"}, output={}, actor="a-001")
        assert e1.prev_hash == e0.entry_hash
        assert e2.prev_hash == e1.entry_hash

    def test_entry_hash_matches_payload(self, tmp_path: Path) -> None:
        """entry_hash must be SHA-256 of canonical JSON of payload fields."""
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        entry = writer.append(
            decision_type="task_created",
            inputs={"task_id": "t-001"},
            output={"status": "open"},
            actor="orchestrator",
        )
        payload = {
            "seq": entry.seq,
            "prev_hash": entry.prev_hash,
            "timestamp": entry.timestamp,
            "decision_type": entry.decision_type,
            "inputs": entry.inputs,
            "output": entry.output,
            "actor": entry.actor,
            "committed": entry.committed,
        }
        expected_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert entry.entry_hash == expected_hash

    def test_sequence_numbers_increment(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        entries = [writer.append(decision_type=f"event_{i}", inputs={}, output={}, actor="orc") for i in range(5)]
        assert [e.seq for e in entries] == [0, 1, 2, 3, 4]

    def test_wal_is_jsonl_format(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append(decision_type="task_created", inputs={}, output={}, actor="orc")
        writer.append(decision_type="task_done", inputs={}, output={}, actor="orc")
        wal_path = tmp_path / "runtime" / "wal" / "run-001.wal.jsonl"
        lines = wal_path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "entry_hash" in parsed
            assert "prev_hash" in parsed
            assert "decision_type" in parsed

    def test_persists_across_writer_instances(self, tmp_path: Path) -> None:
        """New writer continues chain from where previous writer left off."""
        w1 = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e0 = w1.append(decision_type="task_created", inputs={"id": "t-001"}, output={}, actor="orc")
        del w1

        w2 = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e1 = w2.append(decision_type="task_done", inputs={"id": "t-001"}, output={}, actor="orc")

        assert e1.seq == 1
        assert e1.prev_hash == e0.entry_hash

    def test_committed_default_is_true(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e = writer.append(decision_type="task_created", inputs={}, output={}, actor="orc")
        assert e.committed is True

    def test_committed_false_preserved(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        e = writer.append(
            decision_type="agent_spawn",
            inputs={},
            output={},
            actor="orc",
            committed=False,
        )
        assert e.committed is False


class TestWALReader:
    def test_reads_all_entries(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        for i in range(10):
            writer.append(decision_type=f"event_{i}", inputs={"i": i}, output={}, actor="orc")
        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        entries = list(reader.iter_entries())
        assert len(entries) == 10

    def test_entries_in_order(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        for i in range(5):
            writer.append(decision_type=f"event_{i}", inputs={}, output={}, actor="orc")
        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        entries = list(reader.iter_entries())
        assert [e.seq for e in entries] == [0, 1, 2, 3, 4]

    def test_verify_chain_passes_for_valid_wal(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        for i in range(5):
            writer.append(decision_type=f"event_{i}", inputs={}, output={}, actor="orc")
        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        is_valid, errors = reader.verify_chain()
        assert is_valid is True
        assert errors == []

    def test_verify_chain_detects_tampered_decision_type(self, tmp_path: Path) -> None:
        """Modifying an entry's content must break the hash chain."""
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append(decision_type="task_created", inputs={"id": "t-001"}, output={}, actor="orc")
        writer.append(decision_type="task_done", inputs={"id": "t-001"}, output={}, actor="orc")

        wal_path = tmp_path / "runtime" / "wal" / "run-001.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        entry_data = json.loads(lines[0])
        entry_data["decision_type"] = "evil_operation"
        lines[0] = json.dumps(entry_data)
        wal_path.write_text("\n".join(lines) + "\n")

        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        is_valid, errors = reader.verify_chain()
        assert is_valid is False
        assert len(errors) > 0

    def test_verify_chain_detects_missing_entry(self, tmp_path: Path) -> None:
        """Removing a middle entry must break the prev_hash chain."""
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        for i in range(3):
            writer.append(decision_type=f"event_{i}", inputs={}, output={}, actor="orc")
        wal_path = tmp_path / "runtime" / "wal" / "run-001.wal.jsonl"
        lines = wal_path.read_text().splitlines()
        lines.pop(1)  # remove middle entry
        wal_path.write_text("\n".join(lines) + "\n")

        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        is_valid, errors = reader.verify_chain()
        assert is_valid is False

    def test_empty_wal_is_valid(self, tmp_path: Path) -> None:
        wal_path = tmp_path / "runtime" / "wal" / "run-001.wal.jsonl"
        wal_path.parent.mkdir(parents=True, exist_ok=True)
        wal_path.touch()
        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        is_valid, errors = reader.verify_chain()
        assert is_valid is True
        assert errors == []

    def test_nonexistent_wal_raises_on_iter(self, tmp_path: Path) -> None:
        reader = WALReader(run_id="nonexistent-run", sdd_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            list(reader.iter_entries())

    def test_nonexistent_wal_raises_on_verify(self, tmp_path: Path) -> None:
        reader = WALReader(run_id="nonexistent-run", sdd_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            reader.verify_chain()


class TestExecutionFingerprint:
    def test_empty_fingerprint_is_deterministic(self) -> None:
        h1 = ExecutionFingerprint().compute()
        h2 = ExecutionFingerprint().compute()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_same_decisions_produce_same_fingerprint(self) -> None:
        fp1 = ExecutionFingerprint()
        fp2 = ExecutionFingerprint()
        for fp in (fp1, fp2):
            fp.record("task_created", {"id": "t-001"}, {"status": "open"})
            fp.record("agent_spawned", {"role": "backend"}, {"id": "a-001"})
        assert fp1.compute() == fp2.compute()

    def test_different_order_produces_different_fingerprint(self) -> None:
        fp1 = ExecutionFingerprint()
        fp1.record("task_created", {"id": "t-001"}, {})
        fp1.record("agent_spawned", {"role": "backend"}, {})

        fp2 = ExecutionFingerprint()
        fp2.record("agent_spawned", {"role": "backend"}, {})
        fp2.record("task_created", {"id": "t-001"}, {})

        assert fp1.compute() != fp2.compute()

    def test_different_inputs_produce_different_fingerprint(self) -> None:
        fp1 = ExecutionFingerprint()
        fp1.record("task_created", {"id": "t-001"}, {})

        fp2 = ExecutionFingerprint()
        fp2.record("task_created", {"id": "t-002"}, {})

        assert fp1.compute() != fp2.compute()

    def test_fingerprint_from_wal_matches_manual(self, tmp_path: Path) -> None:
        """Fingerprint built via from_wal must equal manual record() calls."""
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append("task_created", {"id": "t-001"}, {"status": "open"}, actor="orc")
        writer.append("agent_spawned", {"role": "backend"}, {"id": "a-001"}, actor="orc")

        reader = WALReader(run_id="run-001", sdd_dir=tmp_path)
        fp_from_wal = ExecutionFingerprint.from_wal(reader)

        fp_manual = ExecutionFingerprint()
        for entry in WALReader(run_id="run-001", sdd_dir=tmp_path).iter_entries():
            fp_manual.record(entry.decision_type, entry.inputs, entry.output)

        assert fp_from_wal.compute() == fp_manual.compute()

    def test_fingerprint_from_empty_wal(self, tmp_path: Path) -> None:
        """Empty WAL produces same fingerprint as empty ExecutionFingerprint."""
        wal_path = tmp_path / "runtime" / "wal" / "run-empty.wal.jsonl"
        wal_path.parent.mkdir(parents=True, exist_ok=True)
        wal_path.touch()

        reader = WALReader(run_id="run-empty", sdd_dir=tmp_path)
        fp_from_wal = ExecutionFingerprint.from_wal(reader)
        fp_empty = ExecutionFingerprint()

        assert fp_from_wal.compute() == fp_empty.compute()


class TestWALRecovery:
    def test_uncommitted_entries_found(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append("task_created", {"id": "t-001"}, {}, actor="orc", committed=True)
        writer.append("agent_spawn", {"role": "backend"}, {}, actor="orc", committed=False)

        recovery = WALRecovery(run_id="run-001", sdd_dir=tmp_path)
        in_flight = recovery.get_uncommitted_entries()

        assert len(in_flight) == 1
        assert in_flight[0].decision_type == "agent_spawn"
        assert in_flight[0].committed is False

    def test_multiple_uncommitted_entries_returned(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append("task_created", {}, {}, actor="orc", committed=True)
        writer.append("spawn_a1", {}, {}, actor="orc", committed=False)
        writer.append("spawn_a2", {}, {}, actor="orc", committed=False)

        recovery = WALRecovery(run_id="run-001", sdd_dir=tmp_path)
        in_flight = recovery.get_uncommitted_entries()

        assert len(in_flight) == 2

    def test_all_committed_returns_empty(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="run-001", sdd_dir=tmp_path)
        writer.append("task_created", {}, {}, actor="orc", committed=True)
        writer.append("task_done", {}, {}, actor="orc", committed=True)

        recovery = WALRecovery(run_id="run-001", sdd_dir=tmp_path)
        in_flight = recovery.get_uncommitted_entries()
        assert in_flight == []

    def test_missing_wal_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent WAL = no in-flight entries (fresh start)."""
        recovery = WALRecovery(run_id="nonexistent", sdd_dir=tmp_path)
        assert recovery.get_uncommitted_entries() == []
