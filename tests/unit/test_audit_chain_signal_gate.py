"""Tests for the additive ``signal.gate_projection`` chain helper (#2556).

Each blocker projection and each clearance / expiry transition is mirrored
into the HMAC-chained audit log so the gate is independently attestable and
tamper-evident. Flipping the blocker content hash or the resolver identity in
a stored entry must break verification at that entry.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_SIGNAL_GATE_PROJECTION,
    AuditChainStore,
    record_signal_gate_projection,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_projection_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "a" * 64,
        clearance_task_id="clearance-deadbeef00000000",
        injected_edges=["task-a", "task-b"],
        graph_delta_hash="c" * 64,
        scope_cell_id="cell-a",
        deadline=0,
        resolution="pending",
        resolver="",
        journal_entry_hash="sha256:" + "d" * 64,
    )
    assert event.event_type == EVENT_SIGNAL_GATE_PROJECTION
    assert event.resource_id == "clearance-deadbeef00000000"
    assert event.details["resolution"] == "pending"
    assert event.details["injected_edges"] == ["task-a", "task-b"]
    assert event.details["blocker_content_hash"] == "sha256:" + "a" * 64
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors


def test_resolution_references_blocker_entry_hash(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    projection = record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "a" * 64,
        clearance_task_id="clearance-1111",
        injected_edges=["t1"],
        graph_delta_hash="c" * 64,
        scope_cell_id="cell-a",
        deadline=0,
        resolution="pending",
        resolver="",
    )
    release = record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "a" * 64,
        clearance_task_id="clearance-1111",
        injected_edges=["t1"],
        graph_delta_hash="c" * 64,
        scope_cell_id="cell-a",
        deadline=0,
        resolution="cleared",
        resolver="operator:alex",
        last_state_hash=projection.hmac,
        blocker_entry_hash=projection.hmac,
    )
    # The release entry references the projection (blocker) entry hash.
    assert release.details["blocker_entry_hash"] == projection.hmac
    assert release.details["resolver"] == "operator:alex"
    assert release.details["resolution"] == "cleared"
    ok, errors = chain.verify()
    assert ok, errors


def test_tamper_blocker_content_hash_breaks_chain(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "a" * 64,
        clearance_task_id="clearance-2222",
        injected_edges=["t1"],
        graph_delta_hash="c" * 64,
        scope_cell_id="cell-a",
        deadline=0,
        resolution="pending",
        resolver="",
    )
    ok, _ = chain.verify()
    assert ok

    # Flip the recorded blocker content hash in the stored JSONL.
    log_files = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert log_files
    log_path = log_files[0]
    lines = log_path.read_text().splitlines()
    entry = json.loads(lines[-1])
    entry["details"]["blocker_content_hash"] = "sha256:" + "f" * 64
    lines[-1] = json.dumps(entry, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n")

    ok_after, errors = chain.verify()
    assert not ok_after
    assert any("HMAC mismatch" in e for e in errors)


def test_tamper_resolver_identity_breaks_chain(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "a" * 64,
        clearance_task_id="clearance-3333",
        injected_edges=["t1"],
        graph_delta_hash="c" * 64,
        scope_cell_id="cell-a",
        deadline=0,
        resolution="cleared",
        resolver="operator:honest",
    )
    log_files = sorted((tmp_path / "audit").glob("*.jsonl"))
    log_path = log_files[0]
    lines = log_path.read_text().splitlines()
    entry = json.loads(lines[-1])
    entry["details"]["resolver"] = "operator:attacker"
    lines[-1] = json.dumps(entry, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n")

    ok_after, errors = chain.verify()
    assert not ok_after
    assert any("HMAC mismatch" in e for e in errors)
