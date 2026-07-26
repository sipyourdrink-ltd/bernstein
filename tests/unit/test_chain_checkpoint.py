"""Signed checkpoint substrate: pinning, extension math, append discipline.

The checkpoint is the durable pin that makes an audit-history shrink sticky:
``compute_seal`` refuses unless the current tree is a consistent extension
of the last checkpoint, and only a chain-resident acknowledgement lets a new
pin be recorded over a divergence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.persistence.chain_checkpoint import (
    CheckpointConsistencyError,
    CheckpointFileError,
    check_extension,
    checkpoints_path,
    compute_origin,
    count_entries,
    load_checkpoints,
    record_checkpoint,
)
from bernstein.core.persistence.merkle import ChainBrokenError, compute_seal
from bernstein.core.security.audit import (
    EVENT_CHAIN_TEAR_ACKNOWLEDGED,
    AuditLog,
    OutstandingTearError,
    RetentionPolicy,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"checkpoint-substrate-test-key-012"


def _seed(tmp_path: Path, count: int = 6) -> Path:
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=_KEY)
    for i in range(count):
        log.log("test.event", "tester", "task", f"t-{i}", {"i": i})
    return audit_dir


def _seal_and_pin(audit_dir: Path) -> dict[str, Any]:
    _tree, seal = compute_seal(audit_dir, key=_KEY)
    return record_checkpoint(audit_dir, seal, key=_KEY)


class TestPinning:
    def test_first_seal_records_a_genesis_linked_checkpoint(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        payload = _seal_and_pin(audit_dir)

        assert payload["entry_count"] == 6
        assert payload["extends_prev"] is True
        assert payload["prev_checkpoint_sha256"] == "0" * 64
        assert payload["origin"] == compute_origin(audit_dir)
        state = load_checkpoints(audit_dir, _KEY)
        assert state.last == payload
        assert state.torn_tail is False

    def test_checkpoint_carries_no_timestamp(self, tmp_path: Path) -> None:
        """Byte-determinism: the payload is a pure function of content + key."""
        audit_dir = _seed(tmp_path)
        payload = _seal_and_pin(audit_dir)
        assert "sealed_at" not in payload
        assert not any("time" in key or key == "at" for key in payload)

    def test_growth_extends_and_links(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "extra", {})
        second = _seal_and_pin(audit_dir)

        assert second["entry_count"] == 7
        assert second["extends_prev"] is True
        assert second["prev_checkpoint_sha256"] != "0" * 64
        assert len(load_checkpoints(audit_dir, _KEY).checkpoints) == 2

    def test_unchanged_tree_does_not_append(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        first = _seal_and_pin(audit_dir)
        again = _seal_and_pin(audit_dir)
        assert again == first
        assert len(load_checkpoints(audit_dir, _KEY).checkpoints) == 1

    def test_archived_segment_still_extends(self, tmp_path: Path) -> None:
        """Retention moves a pinned segment to the archive; the pin still holds."""
        audit_dir = _seed(tmp_path)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        aged = audit_dir / "2020-01-01.jsonl"
        segment.rename(aged)
        _seal_and_pin(audit_dir)

        AuditLog(audit_dir, key=_KEY).archive(RetentionPolicy())
        assert not aged.exists(), "precondition: retention archived the pinned segment"

        payload = load_checkpoints(audit_dir, _KEY).last
        assert payload is not None
        assert check_extension(audit_dir, payload) == []


class TestExtensionConflicts:
    def test_shrunk_segment_conflicts(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        payload = _seal_and_pin(audit_dir)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        lines = [line for line in segment.read_bytes().split(b"\n") if line]
        segment.write_bytes(b"\n".join(lines[:3]) + b"\n")

        conflicts = check_extension(audit_dir, payload)
        kinds = {conflict.kind for conflict in conflicts}
        assert "segment_shrunk" in kinds
        assert "entry_count" in kinds
        shrunk = next(c for c in conflicts if c.kind == "segment_shrunk")
        assert shrunk.offset == segment.stat().st_size

    def test_rewritten_prefix_conflicts(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        payload = _seal_and_pin(audit_dir)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        raw = segment.read_bytes()
        segment.write_bytes(raw.replace(b'"i": 0', b'"i": 7', 1))

        conflicts = check_extension(audit_dir, payload)
        assert any(conflict.kind == "segment_prefix_mismatch" for conflict in conflicts)

    def test_deleted_segment_conflicts(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        aged = audit_dir / "2020-01-01.jsonl"
        segment.rename(aged)
        AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "today", {})
        payload = _seal_and_pin(audit_dir)

        aged.unlink()
        conflicts = check_extension(audit_dir, payload)
        assert any(conflict.kind == "segment_missing" for conflict in conflicts)

    def test_compute_seal_refuses_a_shrunk_history(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        lines = [line for line in segment.read_bytes().split(b"\n") if line]
        segment.write_bytes(b"\n".join(lines[:3]) + b"\n")

        with pytest.raises(CheckpointConsistencyError) as excinfo:
            compute_seal(audit_dir, key=_KEY)
        assert excinfo.value.conflicts
        # Retrying does not change the outcome: self-clear is dead.
        with pytest.raises(CheckpointConsistencyError):
            compute_seal(audit_dir, key=_KEY)

    def test_compute_seal_refuses_unacknowledged_tears(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        with segment.open("ab") as fh:
            fh.write(b"\x80\xffgarbage")

        with pytest.raises(OutstandingTearError):
            compute_seal(audit_dir, key=_KEY)

    def test_compute_seal_still_refuses_hard_corruption(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        raw = segment.read_bytes()
        segment.write_bytes(raw.replace(b'"i": 1', b'"i": 8', 1))

        with pytest.raises(ChainBrokenError):
            compute_seal(audit_dir, key=_KEY)


class TestDivergenceAcknowledgement:
    def _shrink(self, audit_dir: Path) -> tuple[str, int]:
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        lines = [line for line in segment.read_bytes().split(b"\n") if line]
        segment.write_bytes(b"\n".join(lines[:3]) + b"\n")
        return segment.name, segment.stat().st_size

    def test_ack_authorises_a_new_pin_and_keeps_the_old_one(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        first = _seal_and_pin(audit_dir)
        segment_name, offset = self._shrink(audit_dir)

        AuditLog(audit_dir, key=_KEY).log(
            EVENT_CHAIN_TEAR_ACKNOWLEDGED,
            "operator",
            "audit_segment",
            segment_name,
            {
                "segment": segment_name,
                "byte_offset": offset,
                "reason": "investigated",
                "checkpoint_root": first["root_hash"],
            },
        )

        _tree, seal = compute_seal(audit_dir, key=_KEY)
        second = record_checkpoint(audit_dir, seal, key=_KEY)
        assert second["extends_prev"] is False
        assert second["divergence_ack"]["checkpoint_root"] == first["root_hash"]

        stored = load_checkpoints(audit_dir, _KEY).checkpoints
        assert [payload["root_hash"] for payload in stored] == [first["root_hash"], second["root_hash"]]

    def test_ack_for_a_different_checkpoint_does_not_authorise(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        segment_name, offset = self._shrink(audit_dir)

        AuditLog(audit_dir, key=_KEY).log(
            EVENT_CHAIN_TEAR_ACKNOWLEDGED,
            "operator",
            "audit_segment",
            segment_name,
            {
                "segment": segment_name,
                "byte_offset": offset,
                "reason": "names the wrong pin",
                "checkpoint_root": "f" * 64,
            },
        )
        with pytest.raises(CheckpointConsistencyError):
            compute_seal(audit_dir, key=_KEY)


class TestFileDiscipline:
    def test_signature_tamper_is_a_hard_error(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        path = checkpoints_path(audit_dir)
        doc = json.loads(path.read_text())
        doc["payload"]["entry_count"] = 1
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")

        with pytest.raises(CheckpointFileError):
            load_checkpoints(audit_dir, _KEY)

    def test_reordered_or_spliced_file_breaks_linkage(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        _seal_and_pin(audit_dir)
        AuditLog(audit_dir, key=_KEY).log("test.event", "tester", "task", "extra", {})
        _seal_and_pin(audit_dir)

        path = checkpoints_path(audit_dir)
        first_line, second_line = path.read_text().splitlines()
        path.write_text(second_line + "\n" + first_line + "\n")

        with pytest.raises(CheckpointFileError):
            load_checkpoints(audit_dir, _KEY)

    def test_torn_trailing_append_regresses_to_the_previous_pin(self, tmp_path: Path) -> None:
        """A crash mid-append costs freshness, never validity."""
        audit_dir = _seed(tmp_path)
        first = _seal_and_pin(audit_dir)
        path = checkpoints_path(audit_dir)
        with path.open("ab") as fh:
            fh.write(b'{"payload": {"version"')

        state = load_checkpoints(audit_dir, _KEY)
        assert state.torn_tail is True
        assert state.last == first

    def test_missing_file_means_nothing_is_pinned(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        state = load_checkpoints(audit_dir, _KEY)
        assert state.last is None
        assert state.checkpoints == []


class TestChainQuantities:
    def test_origin_is_the_first_record_and_survives_archiving(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path)
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        first_hmac = json.loads(segment.read_bytes().split(b"\n")[0])["hmac"]
        assert compute_origin(audit_dir) == first_hmac

        segment.rename(audit_dir / "2020-01-01.jsonl")
        AuditLog(audit_dir, key=_KEY).archive(RetentionPolicy())
        assert compute_origin(audit_dir) == first_hmac

    def test_count_ignores_garbage_lines(self, tmp_path: Path) -> None:
        audit_dir = _seed(tmp_path, count=3)
        assert count_entries(audit_dir) == 3
        segment = sorted(audit_dir.glob("*.jsonl"))[0]
        with segment.open("ab") as fh:
            fh.write(b"not a record\n")
        assert count_entries(audit_dir) == 3
