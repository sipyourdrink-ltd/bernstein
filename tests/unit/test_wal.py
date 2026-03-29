"""Tests for WAL (Write-Ahead Log) — writer, reader, chain verification, fingerprint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.wal import (
    GENESIS_HASH,
    ExecutionFingerprint,
    WALEntry,
    WALReader,
    WALRecovery,
    WALWriter,
    _compute_entry_hash,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_writer(tmp_path: Path, run_id: str = "test-run") -> WALWriter:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(exist_ok=True)
    return WALWriter(run_id=run_id, sdd_dir=sdd)


def _make_reader(tmp_path: Path, run_id: str = "test-run") -> WALReader:
    sdd = tmp_path / ".sdd"
    return WALReader(run_id=run_id, sdd_dir=sdd)


def _wal_path(tmp_path: Path, run_id: str = "test-run") -> Path:
    return tmp_path / ".sdd" / "runtime" / "wal" / f"{run_id}.wal.jsonl"


# ---------------------------------------------------------------------------
# TestComputeEntryHash
# ---------------------------------------------------------------------------


class TestComputeEntryHash:
    def test_deterministic(self) -> None:
        payload = {"seq": 0, "prev_hash": GENESIS_HASH, "data": "hello"}
        assert _compute_entry_hash(payload) == _compute_entry_hash(payload)

    def test_key_order_irrelevant(self) -> None:
        a = {"z": 1, "a": 2}
        b = {"a": 2, "z": 1}
        assert _compute_entry_hash(a) == _compute_entry_hash(b)

    def test_different_payloads_differ(self) -> None:
        a = {"seq": 0, "data": "hello"}
        b = {"seq": 0, "data": "world"}
        assert _compute_entry_hash(a) != _compute_entry_hash(b)

    def test_returns_64_char_hex(self) -> None:
        h = _compute_entry_hash({"x": 1})
        assert len(h) == 64
        int(h, 16)  # should not raise


# ---------------------------------------------------------------------------
# TestWALWriter
# ---------------------------------------------------------------------------


class TestWALWriter:
    def test_creates_wal_directory(self, tmp_path: Path) -> None:
        _make_writer(tmp_path)
        assert _wal_path(tmp_path).parent.is_dir()

    def test_append_creates_file(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("test_decision", {"key": "val"}, {"result": 1}, "tester")
        assert _wal_path(tmp_path).exists()

    def test_append_returns_wal_entry(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        entry = writer.append("test_decision", {"k": 1}, {"r": 2}, "tester")
        assert isinstance(entry, WALEntry)
        assert entry.seq == 0
        assert entry.decision_type == "test_decision"
        assert entry.inputs == {"k": 1}
        assert entry.output == {"r": 2}
        assert entry.actor == "tester"
        assert entry.committed is True
        assert entry.prev_hash == GENESIS_HASH

    def test_write_entry_alias(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        entry = writer.write_entry("alias_test", {"a": 1}, {"b": 2}, "me")
        assert entry.decision_type == "alias_test"
        assert entry.seq == 0

    def test_sequential_entries_chain(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        e1 = writer.append("d1", {}, {}, "a")
        e2 = writer.append("d2", {}, {}, "a")
        e3 = writer.append("d3", {}, {}, "a")

        assert e1.prev_hash == GENESIS_HASH
        assert e2.prev_hash == e1.entry_hash
        assert e3.prev_hash == e2.entry_hash
        assert e1.seq == 0
        assert e2.seq == 1
        assert e3.seq == 2

    def test_uncommitted_entry(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        entry = writer.append("pre_action", {}, {}, "actor", committed=False)
        assert entry.committed is False

    def test_resume_from_existing_wal(self, tmp_path: Path) -> None:
        writer1 = _make_writer(tmp_path)
        writer1.append("d1", {}, {}, "a")
        e2 = writer1.append("d2", {}, {}, "a")

        writer2 = _make_writer(tmp_path)
        e3 = writer2.append("d3", {}, {}, "a")
        assert e3.seq == 2
        assert e3.prev_hash == e2.entry_hash

    def test_jsonl_format(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("dec", {"x": 1}, {"y": 2}, "actor")
        lines = _wal_path(tmp_path).read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["decision_type"] == "dec"
        assert data["entry_hash"]
        assert data["prev_hash"] == GENESIS_HASH

    def test_multiple_entries_produce_multiple_lines(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(5):
            writer.append(f"d{i}", {}, {}, "a")
        lines = _wal_path(tmp_path).read_text().strip().splitlines()
        assert len(lines) == 5


# ---------------------------------------------------------------------------
# TestWALReader
# ---------------------------------------------------------------------------


class TestWALReader:
    def test_iter_entries_returns_all(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(3):
            writer.append(f"d{i}", {"i": i}, {"r": i * 10}, "a")

        reader = _make_reader(tmp_path)
        entries = list(reader.iter_entries())
        assert len(entries) == 3
        assert entries[0].decision_type == "d0"
        assert entries[1].decision_type == "d1"
        assert entries[2].decision_type == "d2"

    def test_iter_entries_preserves_data(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("test", {"key": "value"}, {"out": 42}, "actor", committed=False)

        reader = _make_reader(tmp_path)
        entries = list(reader.iter_entries())
        assert len(entries) == 1
        e = entries[0]
        assert e.inputs == {"key": "value"}
        assert e.output == {"out": 42}
        assert e.committed is False

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        reader = WALReader(run_id="nonexistent", sdd_dir=sdd)
        with pytest.raises(FileNotFoundError):
            list(reader.iter_entries())

    def test_verify_chain_valid(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(5):
            writer.append(f"d{i}", {"i": i}, {}, "a")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []

    def test_verify_chain_empty_wal(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("only", {}, {}, "a")

        reader = _make_reader(tmp_path)
        ok, _errors = reader.verify_chain()
        assert ok is True

    def test_verify_chain_detects_tampered_hash(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d0", {}, {}, "a")
        writer.append("d1", {}, {}, "a")

        path = _wal_path(tmp_path)
        lines = path.read_text().splitlines()
        data = json.loads(lines[1])
        data["entry_hash"] = "0" * 64
        lines[1] = json.dumps(data, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is False
        assert len(errors) >= 1
        assert "Hash mismatch" in errors[0]

    def test_verify_chain_detects_broken_linkage(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d0", {}, {}, "a")
        writer.append("d1", {}, {}, "a")
        writer.append("d2", {}, {}, "a")

        path = _wal_path(tmp_path)
        lines = path.read_text().splitlines()
        data = json.loads(lines[1])
        data["prev_hash"] = "f" * 64
        # Recompute entry_hash with tampered prev_hash for a "valid internal hash"
        payload = {k: v for k, v in data.items() if k != "entry_hash"}
        data["entry_hash"] = _compute_entry_hash(payload)
        lines[1] = json.dumps(data, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is False
        assert any("Chain broken" in e for e in errors)

    def test_verify_chain_detects_tampered_content(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d0", {"secret": "real"}, {}, "a")

        path = _wal_path(tmp_path)
        lines = path.read_text().splitlines()
        data = json.loads(lines[0])
        data["inputs"]["secret"] = "tampered"
        lines[0] = json.dumps(data, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is False
        assert any("Hash mismatch" in e for e in errors)

    def test_verify_chain_file_not_found(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        reader = WALReader(run_id="missing", sdd_dir=sdd)
        with pytest.raises(FileNotFoundError):
            reader.verify_chain()


# ---------------------------------------------------------------------------
# TestWALRecovery
# ---------------------------------------------------------------------------


class TestWALRecovery:
    def test_no_uncommitted(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d0", {}, {}, "a", committed=True)
        writer.append("d1", {}, {}, "a", committed=True)

        sdd = tmp_path / ".sdd"
        recovery = WALRecovery(run_id="test-run", sdd_dir=sdd)
        assert recovery.get_uncommitted_entries() == []

    def test_finds_uncommitted(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("committed_action", {}, {}, "a", committed=True)
        writer.append("pre_action", {"task": "T-1"}, {}, "a", committed=False)

        sdd = tmp_path / ".sdd"
        recovery = WALRecovery(run_id="test-run", sdd_dir=sdd)
        uncommitted = recovery.get_uncommitted_entries()
        assert len(uncommitted) == 1
        assert uncommitted[0].decision_type == "pre_action"

    def test_no_wal_file_returns_empty(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        recovery = WALRecovery(run_id="ghost", sdd_dir=sdd)
        assert recovery.get_uncommitted_entries() == []


# ---------------------------------------------------------------------------
# TestExecutionFingerprint
# ---------------------------------------------------------------------------


class TestExecutionFingerprint:
    def test_empty_fingerprint(self) -> None:
        fp = ExecutionFingerprint()
        result = fp.compute()
        assert len(result) == 64
        int(result, 16)  # valid hex

    def test_same_decisions_same_fingerprint(self) -> None:
        decisions: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            ("task_claimed", {"task_id": "T-1"}, {"batch_size": 1}),
            ("task_completed", {"task_id": "T-1"}, {"janitor_passed": True}),
        ]

        fp1 = ExecutionFingerprint()
        fp2 = ExecutionFingerprint()
        for dt, inp, out in decisions:
            fp1.record(dt, inp, out)
            fp2.record(dt, inp, out)

        assert fp1.compute() == fp2.compute()

    def test_different_decisions_different_fingerprint(self) -> None:
        fp1 = ExecutionFingerprint()
        fp1.record("task_claimed", {"task_id": "T-1"}, {})

        fp2 = ExecutionFingerprint()
        fp2.record("task_claimed", {"task_id": "T-2"}, {})

        assert fp1.compute() != fp2.compute()

    def test_order_matters(self) -> None:
        fp1 = ExecutionFingerprint()
        fp1.record("a", {}, {})
        fp1.record("b", {}, {})

        fp2 = ExecutionFingerprint()
        fp2.record("b", {}, {})
        fp2.record("a", {}, {})

        assert fp1.compute() != fp2.compute()

    def test_add_decision_alias(self) -> None:
        fp1 = ExecutionFingerprint()
        fp1.record("x", {"k": 1}, {"v": 2})

        fp2 = ExecutionFingerprint()
        fp2.add_decision("x", {"k": 1}, {"v": 2})

        assert fp1.compute() == fp2.compute()

    def test_finalize_alias(self) -> None:
        fp = ExecutionFingerprint()
        fp.record("x", {}, {})
        assert fp.finalize() == fp.compute()

    def test_from_wal(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d1", {"a": 1}, {"b": 2}, "actor")
        writer.append("d2", {"c": 3}, {"d": 4}, "actor")

        reader = _make_reader(tmp_path)
        fp_from_wal = ExecutionFingerprint.from_wal(reader)

        fp_manual = ExecutionFingerprint()
        fp_manual.record("d1", {"a": 1}, {"b": 2})
        fp_manual.record("d2", {"c": 3}, {"d": 4})

        assert fp_from_wal.compute() == fp_manual.compute()

    def test_from_wal_consistency_across_runs(self, tmp_path: Path) -> None:
        """Two writers producing identical decisions yield the same fingerprint."""
        sdd1 = tmp_path / "run1" / ".sdd"
        sdd1.mkdir(parents=True)
        w1 = WALWriter(run_id="r1", sdd_dir=sdd1)
        w1.append("tick_start", {"tick": 1}, {}, "orch")
        w1.append("task_claimed", {"task_id": "T-1"}, {"batch_size": 1}, "lifecycle")

        sdd2 = tmp_path / "run2" / ".sdd"
        sdd2.mkdir(parents=True)
        w2 = WALWriter(run_id="r1", sdd_dir=sdd2)
        w2.append("tick_start", {"tick": 1}, {}, "orch")
        w2.append("task_claimed", {"task_id": "T-1"}, {"batch_size": 1}, "lifecycle")

        r1 = WALReader(run_id="r1", sdd_dir=sdd1)
        r2 = WALReader(run_id="r1", sdd_dir=sdd2)

        assert ExecutionFingerprint.from_wal(r1).compute() == ExecutionFingerprint.from_wal(r2).compute()


# ---------------------------------------------------------------------------
# TestWALIntegration
# ---------------------------------------------------------------------------


class TestWALIntegration:
    """End-to-end: write → read → verify → fingerprint."""

    def test_full_lifecycle(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)

        writer.append("tick_start", {"tick": 1}, {}, "orchestrator")
        writer.append("task_claimed", {"task_id": "T-1", "role": "backend"}, {"batch_size": 1}, "task_lifecycle")
        writer.append("task_completed", {"task_id": "T-1"}, {"janitor_passed": True}, "task_lifecycle")
        writer.append("tick_start", {"tick": 2}, {}, "orchestrator")
        writer.append("task_claimed", {"task_id": "T-2", "role": "qa"}, {"batch_size": 1}, "task_lifecycle")
        writer.append("task_failed", {"task_id": "T-2"}, {"janitor_passed": False}, "task_lifecycle")

        reader = _make_reader(tmp_path)
        entries = list(reader.iter_entries())
        assert len(entries) == 6

        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []

        fp = ExecutionFingerprint.from_wal(reader)
        fingerprint = fp.compute()
        assert len(fingerprint) == 64

    def test_corruption_in_middle(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(5):
            writer.append(f"d{i}", {"i": i}, {}, "a")

        path = _wal_path(tmp_path)
        lines = path.read_text().splitlines()
        data = json.loads(lines[2])
        data["decision_type"] = "CORRUPTED"
        lines[2] = json.dumps(data, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is False
        assert len(errors) >= 1

    def test_deleted_entry_breaks_chain(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(5):
            writer.append(f"d{i}", {}, {}, "a")

        path = _wal_path(tmp_path)
        lines = path.read_text().splitlines()
        del lines[2]
        path.write_text("\n".join(lines) + "\n")

        reader = _make_reader(tmp_path)
        ok, _errors = reader.verify_chain()
        assert ok is False

    def test_appended_forgery_detected(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("legit", {}, {}, "a")

        path = _wal_path(tmp_path)
        forged: dict[str, Any] = {
            "seq": 1,
            "prev_hash": "0" * 64,
            "timestamp": 0.0,
            "decision_type": "forged",
            "inputs": {},
            "output": {},
            "actor": "evil",
            "committed": True,
            "entry_hash": "1" * 64,
        }
        with path.open("a") as f:
            f.write(json.dumps(forged, separators=(",", ":")) + "\n")

        reader = _make_reader(tmp_path)
        ok, _errors = reader.verify_chain()
        assert ok is False
        assert len(_errors) >= 1


# ---------------------------------------------------------------------------
# TestWALWriterEdgeCases
# ---------------------------------------------------------------------------


class TestWALWriterEdgeCases:
    def test_empty_inputs_and_output(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        entry = writer.append("empty", {}, {}, "a")
        assert entry.inputs == {}
        assert entry.output == {}

        reader = _make_reader(tmp_path)
        ok, _ = reader.verify_chain()
        assert ok is True

    def test_nested_inputs(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        nested = {"a": {"b": {"c": [1, 2, {"d": True}]}}}
        entry = writer.append("nested", nested, {}, "a")
        assert entry.inputs == nested

        reader = _make_reader(tmp_path)
        entries = list(reader.iter_entries())
        assert entries[0].inputs == nested

    def test_large_batch(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        for i in range(100):
            writer.append(f"d{i}", {"i": i}, {"r": i}, "a")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []
        entries = list(reader.iter_entries())
        assert len(entries) == 100

    def test_load_tail_corrupted_last_line(self, tmp_path: Path) -> None:
        """Writer recovers gracefully when the last WAL line is corrupted."""
        writer = _make_writer(tmp_path)
        writer.append("d0", {}, {}, "a")

        path = _wal_path(tmp_path)
        with path.open("a") as f:
            f.write("NOT VALID JSON\n")

        # _load_tail sees 2 lines (1 valid + 1 corrupt), returns seq=1
        # so the next entry gets seq=2. The key property is that it
        # doesn't crash and continues appending.
        writer2 = _make_writer(tmp_path)
        entry = writer2.append("d_after_corruption", {}, {}, "a")
        assert entry.seq == 2
        assert entry.decision_type == "d_after_corruption"
