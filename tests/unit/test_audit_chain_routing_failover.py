"""Audit-chain routing receipts for provider failover decisions (issue #2355).

Every failover decision is mirrored into the HMAC-chained audit log as a
routing receipt: the chain considered, the recorded probe outcomes, the chosen
position, and the deterministic decision hash. A verifier holding the same
recorded probe set can recompute the decision byte-identically and check it
against the chain.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_ROUTING_FAILOVER_RECEIPT,
    AuditChainStore,
    record_routing_failover_receipt,
)

_CHAIN = [
    {"adapter": "claude", "model": "opus", "conformance": "expert"},
    {"adapter": "codex", "model": "gpt-5.2", "conformance": "advanced"},
]
_PROBES = [
    {"adapter": "claude", "healthy": False, "probe_kind": "binary_path"},
    {"adapter": "codex", "healthy": True, "probe_kind": "binary_path"},
]


def test_record_routing_failover_receipt_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_routing_failover_receipt(
        chain=chain,
        role="developer",
        task_id="task-7",
        decision_hash="sha256:ab12",
        chosen_index=1,
        reason="failover",
        chain_considered=_CHAIN,
        probe_results=_PROBES,
    )
    assert event.event_type == EVENT_ROUTING_FAILOVER_RECEIPT
    rows = chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["role"] == "developer"
    assert details["task_id"] == "task-7"
    assert details["decision_hash"] == "sha256:ab12"
    assert details["chosen_index"] == 1
    assert details["reason"] == "failover"
    assert details["chain_considered"] == _CHAIN
    assert details["probe_results"] == _PROBES
    assert details["kind"] == "dispatch"
    assert "prev_chain_digest" in details


def test_record_routing_failover_receipt_drill_kind(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_routing_failover_receipt(
        chain=chain,
        role="developer",
        task_id="",
        decision_hash="sha256:cd34",
        chosen_index=0,
        reason="primary_healthy",
        chain_considered=_CHAIN,
        probe_results=_PROBES,
        kind="drill",
        actor="doctor.failover_drill",
    )
    rows = chain.query(event_type=EVENT_ROUTING_FAILOVER_RECEIPT)
    assert rows[0].details["kind"] == "drill"
    assert rows[0].actor == "doctor.failover_drill"


def test_audit_chain_stays_verifiable_after_routing_receipt(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_routing_failover_receipt(
        chain=chain,
        role="developer",
        task_id="task-7",
        decision_hash="sha256:ab12",
        chosen_index=1,
        reason="failover",
        chain_considered=_CHAIN,
        probe_results=_PROBES,
    )
    ok, errors = chain.verify()
    assert ok, errors
