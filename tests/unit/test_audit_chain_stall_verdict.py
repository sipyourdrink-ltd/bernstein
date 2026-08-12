"""Audit-chain stall verdicts (issue #3277).

Every automatic worker kill decided by the heartbeat/stall supervisors is
mirrored into the HMAC-chained audit log as a ``stall.verdict`` event before
the kill is issued: which detector fired, the stall reason, and the measured
inputs (heartbeat age, identical-snapshot count, threshold crossed) that were
in scope at decision time. The verdict attests a decision, never an outcome --
the companion ``process.reap_receipt`` event (joined on ``session_id``)
attests the delivered stop.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_STALL_VERDICT,
    AuditChainStore,
    record_stall_verdict,
)


def test_record_stall_verdict_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_stall_verdict(
        chain=chain,
        session_id="agent-1",
        reason="heartbeat_stale",
        detector="heartbeat",
        heartbeat_age_s=100.0,
        threshold=90.0,
    )
    assert event.event_type == EVENT_STALL_VERDICT
    rows = chain.query(event_type=EVENT_STALL_VERDICT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["session_id"] == "agent-1"
    assert details["reason"] == "heartbeat_stale"
    assert details["detector"] == "heartbeat"
    assert details["heartbeat_age_s"] == 100.0
    assert details["identical_snapshot_count"] is None
    assert details["threshold"] == 90.0
    assert "prev_chain_digest" in details


def test_record_stall_verdict_carries_snapshot_detector_inputs(tmp_path: Path) -> None:
    """The stall detectors record only the inputs they measured."""
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_stall_verdict(
        chain=chain,
        session_id="agent-2",
        reason="no_progress",
        detector="stall_profiled",
        identical_snapshot_count=7,
        threshold=5,
    )
    details = chain.query(event_type=EVENT_STALL_VERDICT)[0].details
    assert details["identical_snapshot_count"] == 7
    assert details["threshold"] == 5
    assert details["heartbeat_age_s"] is None


def test_record_stall_verdict_honours_actor(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_stall_verdict(
        chain=chain,
        session_id="agent-3",
        reason="no_progress",
        detector="stall_simple",
        actor="orchestrator",
    )
    rows = chain.query(event_type=EVENT_STALL_VERDICT)
    assert rows[0].actor == "orchestrator"
    assert rows[0].resource_id == "agent-3"
    assert rows[0].resource_type == "stall_verdict"


def test_audit_chain_stays_verifiable_after_stall_verdict(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_stall_verdict(
        chain=chain,
        session_id="agent-1",
        reason="heartbeat_stale",
        detector="heartbeat",
        heartbeat_age_s=100.0,
        threshold=90.0,
    )
    ok, errors = chain.verify()
    assert ok, errors
