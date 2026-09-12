"""Tests for WAL (Write-Ahead Log) - writer, reader, chain verification, fingerprint."""

from __future__ import annotations

import contextlib
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.wal import (
    GENESIS_HASH,
    ExecutionFingerprint,
    UncommittedIndex,
    WALEntry,
    WALEntryDigest,
    WALReader,
    WALRecovery,
    WALWriter,
    _compute_entry_hash,
    first_divergence,
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

        # _load_tail resumes from the last valid entry (seq=0), so the
        # next entry continues the chain at seq=1. The corrupt line is
        # not a WAL entry and does not consume a sequence number.
        writer2 = _make_writer(tmp_path)
        entry = writer2.append("d_after_corruption", {}, {}, "a")
        assert entry.seq == 1
        assert entry.decision_type == "d_after_corruption"


# ---------------------------------------------------------------------------
# TestWALRecoveryScanAll
# ---------------------------------------------------------------------------


class TestWALRecoveryScanAll:
    """Tests for WALRecovery.scan_all_uncommitted across multiple WAL files."""

    def test_no_wal_directory_returns_empty(self, tmp_path: Path) -> None:
        """Fresh project with no .sdd/runtime/wal/ returns empty list."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        result = WALRecovery.scan_all_uncommitted(sdd)
        assert result == []

    def test_empty_wal_directory_returns_empty(self, tmp_path: Path) -> None:
        """WAL directory exists but has no files."""
        wal_dir = tmp_path / ".sdd" / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        result = WALRecovery.scan_all_uncommitted(tmp_path / ".sdd")
        assert result == []

    def test_all_committed_returns_empty(self, tmp_path: Path) -> None:
        """All entries in all WAL files are committed."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w1 = WALWriter(run_id="run-1", sdd_dir=sdd)
        w1.append("d0", {}, {}, "a", committed=True)
        w1.append("d1", {}, {}, "a", committed=True)

        w2 = WALWriter(run_id="run-2", sdd_dir=sdd)
        w2.append("d0", {}, {}, "a", committed=True)

        result = WALRecovery.scan_all_uncommitted(sdd)
        assert result == []

    def test_finds_uncommitted_across_runs(self, tmp_path: Path) -> None:
        """Uncommitted entries from multiple WAL files are found."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        w1 = WALWriter(run_id="run-1", sdd_dir=sdd)
        w1.append("committed_action", {}, {}, "a", committed=True)
        w1.append("task_claimed", {"task_id": "T-1"}, {}, "a", committed=False)

        w2 = WALWriter(run_id="run-2", sdd_dir=sdd)
        w2.append("task_claimed", {"task_id": "T-2"}, {}, "a", committed=False)
        w2.append("task_spawn_confirmed", {"task_id": "T-2"}, {}, "a", committed=True)
        w2.append("task_claimed", {"task_id": "T-3"}, {}, "a", committed=False)

        result = WALRecovery.scan_all_uncommitted(sdd)
        # T-1: uncommitted; T-2: claim is uncommitted (committed=False), but
        # has a matching spawn_confirmed; T-3: uncommitted with no match.
        # scan_all_uncommitted returns ALL entries with committed=False.
        assert len(result) == 3
        run_ids = [r for r, _ in result]
        assert "run-1" in run_ids
        assert "run-2" in run_ids
        # Verify the actual uncommitted entries
        entries_by_task = {e.inputs.get("task_id"): (r, e) for r, e in result}
        assert "T-1" in entries_by_task
        assert "T-2" in entries_by_task
        assert "T-3" in entries_by_task

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        """The exclude_run_id parameter skips the in-progress run."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        # Old run with uncommitted entries
        w_old = WALWriter(run_id="old-run", sdd_dir=sdd)
        w_old.append("task_claimed", {"task_id": "T-1"}, {}, "a", committed=False)

        # Current run with uncommitted entries (should be skipped)
        w_current = WALWriter(run_id="current-run", sdd_dir=sdd)
        w_current.append("task_claimed", {"task_id": "T-2"}, {}, "a", committed=False)

        result = WALRecovery.scan_all_uncommitted(sdd, exclude_run_id="current-run")
        assert len(result) == 1
        assert result[0][0] == "old-run"
        assert result[0][1].inputs["task_id"] == "T-1"

    def test_returns_sorted_by_wal_file(self, tmp_path: Path) -> None:
        """WAL files are processed in sorted order for determinism."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        # Create WAL files in reverse alphabetical order
        for name in ["run-c", "run-a", "run-b"]:
            w = WALWriter(run_id=name, sdd_dir=sdd)
            w.append("task_claimed", {"task_id": f"T-{name}"}, {}, "a", committed=False)

        result = WALRecovery.scan_all_uncommitted(sdd)
        assert len(result) == 3
        run_ids = [r for r, _ in result]
        assert run_ids == ["run-a", "run-b", "run-c"]

    def test_pre_execution_intent_pattern(self, tmp_path: Path) -> None:
        """Simulates crash between claim and spawn: only uncommitted claims appear."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        # Tick 1: successful claim+spawn cycle
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)
        # Tick 2: crash after claim, before spawn
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)
        # (process crashes here - no committed=True follow-up)

        result = WALRecovery.scan_all_uncommitted(sdd)
        # T-1's claim is uncommitted but has a matching commit; T-2 has no commit.
        # scan_all_uncommitted returns ALL entries with committed=False,
        # not just those without a matching commit. The orchestrator decides
        # what to do with each entry.
        assert len(result) == 2  # both task_claimed entries have committed=False
        task_ids = [e.inputs["task_id"] for _, e in result]
        assert "T-1" in task_ids
        assert "T-2" in task_ids


# ---------------------------------------------------------------------------
# TestWALRecoveryClosedMarker (audit-072)
# ---------------------------------------------------------------------------


class TestUncommittedIndexFastPath:
    """The sidecar index decides which WALs recovery has to open at all."""

    @staticmethod
    def _parse_counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Count calls to the single line-parse path behind every reader."""
        calls = {"n": 0}
        real = WALReader._iter_parsed

        def counting(self: WALReader, *args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(self, *args, **kwargs)

        monkeypatch.setattr(WALReader, "_iter_parsed", counting)
        return calls

    def test_a_run_the_index_does_not_name_is_never_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for i in range(3):
            writer = WALWriter(run_id=f"r{i}", sdd_dir=tmp_path)
            for _ in range(20):
                writer.append("noise", {}, {}, "actor", committed=True)

        calls = self._parse_counter(monkeypatch)
        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="current") == []
        assert calls["n"] == 0

    def test_an_uncommitted_entry_is_still_found_on_the_fast_path(self, tmp_path: Path) -> None:
        """Speed is worth nothing if recovery stops finding what it is for."""
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)

        found = WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")
        assert [(run_id, entry.inputs["task_id"]) for run_id, entry in found] == [("r1", "t-1")]

    def test_mark_committed_takes_the_run_back_off_the_fast_path(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        entry = writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")

        writer.mark_committed(entry.seq)
        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other") == []

    def test_a_wal_written_before_the_index_existed_is_still_recovered(self, tmp_path: Path) -> None:
        """The upgrade case: uncommitted entries exist and no index names them.

        Seeding the index from a scan rather than writing an empty file is
        what keeps this safe. An empty seed would tell every later scan
        there is nothing to recover.
        """
        writer = WALWriter(run_id="legacy", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-legacy"}, {}, "actor", committed=False)
        UncommittedIndex(tmp_path).path.unlink()

        # A new run starts, which is when the index gets seeded.
        WALWriter(run_id="current", sdd_dir=tmp_path)

        found = WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="current")
        assert [run_id for run_id, _entry in found] == ["legacy"]

    def test_a_missing_index_falls_back_and_rebuilds(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        index = UncommittedIndex(tmp_path)
        index.path.unlink()

        assert len(WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")) == 1
        assert [row[0] for row in index.load()] == ["r1"]

    def test_an_unparseable_index_falls_back_and_rebuilds(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        index = UncommittedIndex(tmp_path)
        index.path.write_text("this is not json\n", encoding="utf-8")

        assert len(WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")) == 1
        assert [row[0] for row in index.load()] == ["r1"]

    def test_a_rebuild_keeps_the_excluded_run_in_the_index(self, tmp_path: Path) -> None:
        """The current run's own uncommitted rows belong in the index.

        They are filtered out of the caller's result, not out of the
        rebuild, or the next scan would inherit a short index.
        """
        writer = WALWriter(run_id="current", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        index = UncommittedIndex(tmp_path)
        index.path.unlink()

        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="current") == []
        assert [row[0] for row in index.load()] == ["current"]

    def test_a_closed_wal_is_still_skipped_on_the_fast_path(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        WALRecovery.close_wal("r1", tmp_path, reason="recovered")

        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other") == []

    def test_orphaned_claims_still_found_through_the_index(self, tmp_path: Path) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)

        orphans = WALRecovery.find_orphaned_claims(tmp_path, exclude_run_id="other")
        assert [entry.inputs["task_id"] for _run_id, entry in orphans] == ["t-1"]

    def test_orphan_scan_skips_a_run_the_index_does_not_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for i in range(3):
            writer = WALWriter(run_id=f"r{i}", sdd_dir=tmp_path)
            writer.append("task_claimed", {"task_id": f"t-{i}"}, {}, "actor", committed=True)

        calls = self._parse_counter(monkeypatch)
        assert WALRecovery.find_orphaned_claims(tmp_path, exclude_run_id="current") == []
        assert calls["n"] == 0


class TestUncommittedIndexInvalidation:
    """An index that cannot record a row must stop being believed."""

    def test_a_failed_add_invalidates_the_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise the index is well-formed, short, and trusted.

        The append succeeds either way; the entry is on disk and a full
        scan can always find it. What must not survive is an index that
        parses cleanly while naming fewer runs than exist, because the
        fast path would walk straight past this one.
        """
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)
        index = UncommittedIndex(tmp_path)
        assert index.path.exists()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(UncommittedIndex, "add", boom)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        monkeypatch.undo()

        assert not index.path.exists(), "a short index survived a failed add"
        found = WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")
        assert [entry.inputs["task_id"] for _run_id, entry in found] == ["t-1"]

    def test_a_failed_add_does_not_fail_the_append(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        writer = WALWriter(run_id="r1", sdd_dir=tmp_path)

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(UncommittedIndex, "add", boom)
        entry = writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        assert entry.committed is False
        assert entry.inputs["task_id"] == "t-1"

    def test_a_later_append_does_not_resurrect_the_invalidated_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Append mode creates what it opens, so "absent" has to be defended.

        The failing add invalidates the index. The *next* successful
        uncommitted append is the step that used to undo that: an append to a
        missing file re-created it holding one row, short and parseable and
        therefore trusted, and every other run's uncommitted entry became
        invisible to recovery.
        """
        stranded = WALWriter(run_id="stranded", sdd_dir=tmp_path)
        stranded.append("task_claimed", {"task_id": "t-stranded"}, {}, "actor", committed=False)

        live = WALWriter(run_id="live", sdd_dir=tmp_path)
        index = UncommittedIndex(tmp_path)

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(UncommittedIndex, "add", boom)
        live.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)
        monkeypatch.undo()

        assert not index.path.exists(), "a short index survived a failed add"

        # The step that used to resurrect it.
        live.append("task_claimed", {"task_id": "t-2"}, {}, "actor", committed=False)

        found = WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="none")
        assert "stranded" in {run_id for run_id, _entry in found}, (
            "a later append re-created a one-row index and recovery walked past the other run"
        )

    def test_add_is_a_no_op_when_the_index_is_absent(self, tmp_path: Path) -> None:
        """Only rebuild may create the file."""
        index = UncommittedIndex(tmp_path)
        assert index.add("r1", 1, "a" * 64) is False
        assert not index.path.exists()

    def test_add_writes_when_the_index_is_present(self, tmp_path: Path) -> None:
        """The guard must not turn the ordinary path into a no-op."""
        index = UncommittedIndex(tmp_path)
        index.rebuild([])
        assert index.add("r1", 1, "a" * 64) is True
        assert [row[0] for row in index.load()] == ["r1"]

    def test_seeding_survives_a_log_that_is_not_utf8(self, tmp_path: Path) -> None:
        """A torn write mid-multibyte must not stop the writer constructing.

        The seed scan reads as UTF-8, and this path runs in
        ``WALWriter.__init__`` ahead of the guard that wraps replay, so a
        UnicodeDecodeError here would block the orchestrator over one
        garbled log from a prior run.
        """
        wal_dir = tmp_path / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        (wal_dir / "garbled.wal.jsonl").write_bytes(b'{"seq": 1, "actor": "\xff\xfe"}\n')

        writer = WALWriter(run_id="fresh", sdd_dir=tmp_path)
        entry = writer.append("noise", {}, {}, "actor", committed=True)
        assert entry.decision_type == "noise"

    def test_invalidate_reports_whether_the_file_is_gone(self, tmp_path: Path) -> None:
        index = UncommittedIndex(tmp_path)
        index.rebuild([("r1", 1, "a" * 64)])
        assert index.invalidate() is True
        assert not index.path.exists()
        # Idempotent: nothing there is also "gone".
        assert index.invalidate() is True


class TestWALRecoveryClosedMarker:
    """Tests for the ``.closed`` sidecar marker (audit-072).

    Regression: without a close step, every previous run's uncommitted
    entries are re-returned on every boot, growing unboundedly.  After
    recovery the orchestrator MUST write a ``.closed`` marker so that
    subsequent scans skip the WAL.
    """

    def test_is_wal_closed_false_without_marker(self, tmp_path: Path) -> None:
        """Fresh WAL with no sidecar is not closed."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="r-1", sdd_dir=sdd)
        w.append("d", {}, {}, "a", committed=False)
        assert WALRecovery.is_wal_closed("r-1", sdd) is False

    def test_close_wal_writes_marker_file(self, tmp_path: Path) -> None:
        """close_wal writes a JSON sidecar next to the WAL file."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        WALWriter(run_id="r-1", sdd_dir=sdd).append("d", {}, {}, "a", committed=False)

        marker = WALRecovery.close_wal(
            "r-1",
            sdd,
            reason="unit_test",
            uncommitted_count=1,
            orphaned_count=0,
        )
        assert marker.exists()
        assert marker.name == "r-1.wal.closed"
        payload = json.loads(marker.read_text())
        assert payload["run_id"] == "r-1"
        assert payload["reason"] == "unit_test"
        assert payload["uncommitted_count"] == 1
        assert payload["orphaned_count"] == 0
        assert "closed_at" in payload
        assert WALRecovery.is_wal_closed("r-1", sdd) is True

    def test_scan_skips_closed_wals(self, tmp_path: Path) -> None:
        """scan_all_uncommitted returns nothing for a closed WAL."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="old", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)

        # Before closing: uncommitted entries are visible.
        assert len(WALRecovery.scan_all_uncommitted(sdd)) == 2

        WALRecovery.close_wal("old", sdd)

        # After closing: the WAL is skipped entirely.
        assert WALRecovery.scan_all_uncommitted(sdd) == []

    def test_find_orphaned_claims_skips_closed_wals(self, tmp_path: Path) -> None:
        """find_orphaned_claims also respects the .closed marker."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="old", sdd_dir=sdd)
        # Orphan: claim with no matching spawn_confirmed.
        w.append("task_claimed", {"task_id": "T-orphan"}, {}, "lifecycle", committed=False)

        assert len(WALRecovery.find_orphaned_claims(sdd)) == 1
        WALRecovery.close_wal("old", sdd)
        assert WALRecovery.find_orphaned_claims(sdd) == []

    def test_recovery_then_close_is_noop_on_next_scan(self, tmp_path: Path) -> None:
        """Fresh WAL (2 committed + 3 uncommitted): recover, close, re-scan is empty.

        This is the core audit-072 regression test: after a recovery
        cycle the next scan on the same WAL must return zero entries,
        so the orchestrator does not re-process the same uncommitted
        entries on every boot.
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        run_id = "audit-072-run"
        w = WALWriter(run_id=run_id, sdd_dir=sdd)
        # 2 committed entries
        w.append("tick_started", {"tick": 0}, {}, "orchestrator", committed=True)
        w.append("task_spawn_confirmed", {"task_id": "T-ok"}, {}, "lifecycle", committed=True)
        # 3 uncommitted entries (simulated crashes between claim and spawn)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)
        w.append("task_claimed", {"task_id": "T-3"}, {}, "lifecycle", committed=False)

        # First recovery pass: scan sees all 3 uncommitted entries.
        first_pass = WALRecovery.scan_all_uncommitted(sdd)
        assert len(first_pass) == 3
        task_ids = sorted(e.inputs["task_id"] for _, e in first_pass)
        assert task_ids == ["T-1", "T-2", "T-3"]

        # Orchestrator finishes recovery by closing the WAL.
        marker = WALRecovery.close_wal(
            run_id,
            sdd,
            reason="recovered_by_orchestrator",
            uncommitted_count=len(first_pass),
            orphaned_count=len(first_pass),
        )
        assert marker.exists()
        assert WALRecovery.is_wal_closed(run_id, sdd) is True

        # Second recovery pass on the SAME state: the WAL is skipped,
        # so no entries are returned -- the unbounded re-scan is fixed.
        second_pass = WALRecovery.scan_all_uncommitted(sdd)
        assert second_pass == []
        assert WALRecovery.find_orphaned_claims(sdd) == []

        # And the WAL file itself is untouched -- close is non-destructive.
        wal_file = sdd / "runtime" / "wal" / f"{run_id}.wal.jsonl"
        assert wal_file.exists()
        lines = [ln for ln in wal_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 5  # 2 committed + 3 uncommitted, preserved

    def test_close_wal_does_not_affect_other_runs(self, tmp_path: Path) -> None:
        """Closing one run's WAL leaves other runs visible to the scan."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w1 = WALWriter(run_id="run-a", sdd_dir=sdd)
        w1.append("task_claimed", {"task_id": "T-a"}, {}, "lifecycle", committed=False)
        w2 = WALWriter(run_id="run-b", sdd_dir=sdd)
        w2.append("task_claimed", {"task_id": "T-b"}, {}, "lifecycle", committed=False)

        WALRecovery.close_wal("run-a", sdd)

        result = WALRecovery.scan_all_uncommitted(sdd)
        run_ids = [r for r, _ in result]
        assert run_ids == ["run-b"]


# ---------------------------------------------------------------------------
# TestWALStreaming (audit-082)
# ---------------------------------------------------------------------------


class TestWALStreaming:
    """Streaming reads: _load_tail, verify_chain, iter_entries are O(1) memory."""

    def test_load_tail_fast_on_large_wal(self, tmp_path: Path) -> None:
        """_load_tail on a sizeable WAL resumes without reading the whole file.

        We build a WAL with 5k entries (keeps unit-test wall-time low),
        close and re-open a writer, and assert that appending a new
        entry picks up the correct seq/prev_hash chain.
        """
        import time as _time

        writer = _make_writer(tmp_path)
        for i in range(5000):
            writer.append(f"d{i}", {"i": i}, {"r": i}, "a")

        last = writer.append("last", {}, {}, "a")

        # Re-open; _load_tail must return the final entry's seq & hash
        # without re-reading every line.
        start = _time.monotonic()
        writer2 = _make_writer(tmp_path)
        elapsed = _time.monotonic() - start

        next_entry = writer2.append("after_reopen", {}, {}, "a")
        assert next_entry.seq == last.seq + 1
        assert next_entry.prev_hash == last.entry_hash
        # Generous bound - streaming backward read should be < 100ms even
        # on slow CI.  Old implementation materialized ~5k lines into a
        # list on every construction.
        assert elapsed < 0.5, f"_load_tail too slow: {elapsed:.3f}s"

    def test_iter_entries_skips_malformed_trailing_line(self, tmp_path: Path) -> None:
        """iter_entries logs+skips a malformed trailing line instead of raising."""
        writer = _make_writer(tmp_path)
        writer.append("d0", {"i": 0}, {}, "a")
        writer.append("d1", {"i": 1}, {}, "a")

        path = _wal_path(tmp_path)
        with path.open("a") as f:
            f.write("THIS IS NOT JSON\n")

        reader = _make_reader(tmp_path)
        entries = list(reader.iter_entries())
        # Only the two valid entries come back; malformed line is skipped.
        assert [e.decision_type for e in entries] == ["d0", "d1"]

    def test_verify_chain_streaming_on_large_wal(self, tmp_path: Path) -> None:
        """verify_chain streams through a large WAL without OOM."""
        writer = _make_writer(tmp_path)
        for i in range(2000):
            writer.append(f"d{i}", {"i": i}, {}, "a")

        reader = _make_reader(tmp_path)
        ok, errors = reader.verify_chain()
        assert ok is True
        assert errors == []

    def test_load_tail_no_trailing_newline(self, tmp_path: Path) -> None:
        """_load_tail handles a WAL whose last line lacks a trailing newline."""
        writer = _make_writer(tmp_path)
        writer.append("d0", {}, {}, "a")

        # Simulate a torn write: strip the trailing newline on the last
        # (only) line to prove backward-seek still recovers it.
        path = _wal_path(tmp_path)
        data = path.read_bytes()
        assert data.endswith(b"\n")
        path.write_bytes(data.rstrip(b"\n"))

        writer2 = _make_writer(tmp_path)
        entry = writer2.append("d1", {}, {}, "a")
        # Successfully resumed from the torn-but-valid final line.
        assert entry.seq == 1
        assert entry.prev_hash != GENESIS_HASH


# ---------------------------------------------------------------------------
# TestFingerprintDivergence
# ---------------------------------------------------------------------------


class TestFingerprintDivergence:
    """Per-entry cumulative digests + first-divergence pinpoint.

    These exercise the substrate that lets ``verify --determinism --expect``
    report *where* two runs forked, reusing the existing rolling-hash
    fingerprint rather than a second scheme.
    """

    def test_entry_digests_one_per_entry(self, tmp_path: Path) -> None:
        writer = _make_writer(tmp_path)
        writer.append("d0", {"i": 0}, {}, "a")
        writer.append("d1", {"i": 1}, {}, "a")
        writer.append("d2", {"i": 2}, {}, "a")

        digests = ExecutionFingerprint.entry_digests(_make_reader(tmp_path))
        assert [d.seq for d in digests] == [0, 1, 2]
        assert [d.decision_type for d in digests] == ["d0", "d1", "d2"]
        for d in digests:
            assert len(d.digest) == 64
            int(d.digest, 16)  # valid hex

    def test_last_entry_digest_equals_run_fingerprint(self, tmp_path: Path) -> None:
        """The final cumulative digest is exactly the run's fingerprint.

        This keeps the gate honest: the per-entry stream and the headline
        digest come from the same rolling hash, not a parallel computation.
        """
        writer = _make_writer(tmp_path)
        writer.append("d0", {"a": 1}, {"b": 2}, "a")
        writer.append("d1", {"c": 3}, {"d": 4}, "a")

        digests = ExecutionFingerprint.entry_digests(_make_reader(tmp_path))
        headline = ExecutionFingerprint.from_wal(_make_reader(tmp_path)).compute()
        assert digests[-1].digest == headline

    def test_identical_wals_equal_digests_no_divergence(self, tmp_path: Path) -> None:
        sdd1 = tmp_path / "a" / ".sdd"
        sdd1.mkdir(parents=True)
        w1 = WALWriter(run_id="r", sdd_dir=sdd1)
        w1.append("tick_start", {"tick": 1}, {}, "orch")
        w1.append("task_claimed", {"task_id": "T-1"}, {"batch_size": 1}, "lifecycle")

        sdd2 = tmp_path / "b" / ".sdd"
        sdd2.mkdir(parents=True)
        w2 = WALWriter(run_id="r", sdd_dir=sdd2)
        w2.append("tick_start", {"tick": 1}, {}, "orch")
        w2.append("task_claimed", {"task_id": "T-1"}, {"batch_size": 1}, "lifecycle")

        da = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd1))
        db = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd2))
        assert [d.digest for d in da] == [d.digest for d in db]
        assert first_divergence(da, db) is None

    def test_mutated_entry_divergence_index_points_at_it(self, tmp_path: Path) -> None:
        """Mutating entry 2 (seq=2) must make first divergence land on index 2."""
        sdd1 = tmp_path / "a" / ".sdd"
        sdd1.mkdir(parents=True)
        w1 = WALWriter(run_id="r", sdd_dir=sdd1)
        for i in range(5):
            w1.append(f"d{i}", {"i": i}, {}, "a")

        sdd2 = tmp_path / "b" / ".sdd"
        sdd2.mkdir(parents=True)
        w2 = WALWriter(run_id="r", sdd_dir=sdd2)
        for i in range(5):
            # Entry index 2 carries a different input -> first fork at index 2.
            payload = {"i": 999} if i == 2 else {"i": i}
            w2.append(f"d{i}", payload, {}, "a")

        da = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd1))
        db = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd2))

        idx = first_divergence(da, db)
        assert idx == 2
        # Everything before the fork is identical; the fork entry differs.
        assert da[1].digest == db[1].digest
        assert da[2].digest != db[2].digest
        assert da[idx].seq == 2

    def test_divergence_on_length_mismatch(self, tmp_path: Path) -> None:
        """A run that stops early diverges at the first missing entry index."""
        sdd1 = tmp_path / "a" / ".sdd"
        sdd1.mkdir(parents=True)
        w1 = WALWriter(run_id="r", sdd_dir=sdd1)
        for i in range(4):
            w1.append(f"d{i}", {"i": i}, {}, "a")

        sdd2 = tmp_path / "b" / ".sdd"
        sdd2.mkdir(parents=True)
        w2 = WALWriter(run_id="r", sdd_dir=sdd2)
        for i in range(2):
            w2.append(f"d{i}", {"i": i}, {}, "a")

        da = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd1))
        db = ExecutionFingerprint.entry_digests(WALReader(run_id="r", sdd_dir=sdd2))
        assert first_divergence(da, db) == 2
        assert first_divergence(db, da) == 2

    def test_empty_wals_have_no_divergence(self, tmp_path: Path) -> None:
        assert first_divergence([], []) is None

    def test_entry_digest_is_frozen(self) -> None:
        d = WALEntryDigest(seq=0, decision_type="x", digest="a" * 64)
        with pytest.raises((AttributeError, TypeError)):
            d.seq = 1  # type: ignore[misc]


class TestUncommittedIndexLocking:
    """The lock is what makes load-modify-save safe against a bare append.

    ``add`` appends. ``remove`` / ``remove_run`` / ``rebuild`` load, filter and
    write the whole file back. Interleave them without a lock and the
    write-back is computed from rows that predate the append, so the appended
    row is silently dropped.

    That cost nothing while recovery read every WAL - the row was a hint. It
    costs a record now: an index that exists is authoritative, so a run missing
    from it never has its WAL opened, and its uncommitted ``task_claimed`` is
    never failed.

    Both writers here are separate ``UncommittedIndex`` handles, which is what
    two orchestrators on one ``.sdd`` are. ``flock`` and ``msvcrt.locking`` are
    both per-descriptor, so two handles contend even inside one process.
    """

    @staticmethod
    def _park_write_all(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
        """Hold every ``_write_all`` open until told to proceed.

        This is the load-modify-save window, widened until it is reliable to
        interleave with. The window exists on every call; the test only makes
        it long enough to aim at.

        Returns ``(entered, release)``. ``entered`` is set from inside the
        patched method, so a caller waits on the writer actually being parked
        rather than on a sleep that assumes it.
        """
        entered = threading.Event()
        release = threading.Event()
        real_write_all = UncommittedIndex._write_all

        def blocking_write_all(self: UncommittedIndex, rows: list[tuple[str, int, str]]) -> None:
            entered.set()
            release.wait(timeout=10)
            real_write_all(self, rows)

        monkeypatch.setattr(UncommittedIndex, "_write_all", blocking_write_all)
        return entered, release

    def _interleave_append_into_a_removal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
        """Append to the index while a removal is parked mid write-back.

        Returns whether ``add`` reported success. The removal is started in a
        thread and blocked inside ``_write_all`` - it has loaded its rows and,
        when the lock is in place, still holds it.
        """
        entered, release = self._park_write_all(monkeypatch)
        remover = threading.Thread(target=lambda: UncommittedIndex(tmp_path).remove("stranded", 7))
        remover.start()
        # Released from a timer, not after the append: with the lock in place
        # the append *blocks* on the removal, so releasing afterwards would
        # deadlock the test against the very mechanism it is checking. The
        # delay only has to outlast the append's attempt to acquire, and it
        # is far inside the index's own 5s lock timeout.
        unparker = threading.Timer(0.2, release.set)
        unparker.start()
        try:
            assert entered.wait(timeout=10), "the removal never reached its write-back"
            return UncommittedIndex(tmp_path).add("live", 1, "b" * 64)
        finally:
            unparker.cancel()
            release.set()
            remover.join(timeout=10)

    def test_an_append_during_a_removal_is_not_lost(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The assertion that goes red the moment ``_locked`` stops locking.

        Replace ``_locked`` with ``nullcontext`` and ``remove`` writes back the
        rows it loaded before the append, dropping ``live`` - the run whose WAL
        recovery would then never open.
        """
        UncommittedIndex(tmp_path).rebuild([("stranded", 7, "a" * 64), ("keep", 1, "c" * 64)])

        appended = self._interleave_append_into_a_removal(tmp_path, monkeypatch)

        assert appended, "the append itself failed, so this proves nothing about the lock"
        rows = UncommittedIndex(tmp_path).load()
        assert ("live", 1, "b" * 64) in rows, "the appended row was overwritten by the removal's write-back"
        assert ("stranded", 7, "a" * 64) not in rows, "the removal did not take effect"
        assert ("keep", 1, "c" * 64) in rows

    def test_the_lock_is_what_holds_it_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same interleaving with the lock removed, asserted to lose the row.

        Without this, the test above would keep passing if the lock were
        deleted *and* the timing happened to stop overlapping - green for the
        wrong reason. Here the loss is the expected result, so the pair
        separates "the lock works" from "the threads did not race".
        """
        monkeypatch.setattr(UncommittedIndex, "_locked", lambda _self: contextlib.nullcontext())
        UncommittedIndex(tmp_path).rebuild([("stranded", 7, "a" * 64)])

        self._interleave_append_into_a_removal(tmp_path, monkeypatch)

        assert ("live", 1, "b" * 64) not in UncommittedIndex(tmp_path).load()

    def test_a_row_missing_from_the_index_costs_a_recovered_record(self, tmp_path: Path) -> None:
        """Why the dropped row matters, at the level the fast path acts on.

        A run absent from an index that exists is a run whose WAL is never
        opened, so its uncommitted entry is not in ``scan_all_uncommitted``.
        This is the consequence the two tests above exist to prevent, and it
        is what makes a lost update cost a record rather than a WAL read.
        """
        writer = WALWriter(run_id="live", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-1"}, {}, "actor", committed=False)

        index = UncommittedIndex(tmp_path)
        rows = index.load()
        assert any(r[0] == "live" for r in rows), "precondition: the writer indexed its uncommitted entry"
        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")

        # Exactly what a lost update leaves behind: a well-formed index that
        # simply does not name this run.
        index.rebuild([r for r in rows if r[0] != "live"])

        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other") == []

    def test_an_append_during_a_seeding_scan_is_not_lost(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The seed's scan has to be inside the lock, not just its write.

        While the index is absent ``add`` is a no-op by design, so a row
        appended between the scan reading a run's WAL and the rebuild landing
        would be in neither - and the index then exists, so it is believed.

        Here the scan is parked mid-walk and a second handle appends. With the
        lock around the whole operation the append waits and its row survives;
        with the lock around the write alone it lands in the gap and the
        rebuild publishes an index that does not name it.
        """
        writer = WALWriter(run_id="early", sdd_dir=tmp_path)
        writer.append("task_claimed", {"task_id": "t-0"}, {}, "actor", committed=False)
        index = UncommittedIndex(tmp_path)
        assert index.invalidate(), "precondition: the seed only runs when the index is absent"

        entered, release = self._park_write_all(monkeypatch)
        unparker = threading.Timer(0.2, release.set)
        unparker.start()
        seeder = threading.Thread(target=lambda: WALWriter(run_id="seeder", sdd_dir=tmp_path))
        seeder.start()
        try:
            assert entered.wait(timeout=10), "the seed never reached its write"
            # add() is a no-op while the index is absent, so this is the
            # rebuild's own row set that has to include the appended entry.
            UncommittedIndex(tmp_path).add("late", 1, "d" * 64)
        finally:
            unparker.cancel()
            release.set()
            seeder.join(timeout=10)

        assert any(r[0] == "early" for r in UncommittedIndex(tmp_path).load())
        assert WALRecovery.scan_all_uncommitted(tmp_path, exclude_run_id="other")

    def test_the_seed_does_not_overwrite_an_index_that_already_exists(self, tmp_path: Path) -> None:
        """``only_if_absent`` is checked under the lock, not before taking it."""
        index = UncommittedIndex(tmp_path)
        index.rebuild([("kept", 3, "e" * 64)])

        def scan() -> list[tuple[str, int, str]]:  # pragma: no cover - must not run
            raise AssertionError("scanned an index that already existed")

        assert index.rebuild_from_scan(scan, only_if_absent=True) is False
        assert index.load() == [("kept", 3, "e" * 64)]

    def test_a_rebuild_from_scan_replaces_an_unusable_index(self, tmp_path: Path) -> None:
        """The fallback path: whatever is there is replaced by the scan."""
        index = UncommittedIndex(tmp_path)
        index.path.write_text("{ not json\n")

        assert index.rebuild_from_scan(lambda: [("fresh", 1, "f" * 64)], only_if_absent=False) is True
        assert index.load() == [("fresh", 1, "f" * 64)]
