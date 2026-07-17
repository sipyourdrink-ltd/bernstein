"""Mission phase receipt audit-chain event tests (#2509).

The mission phase receipt mirrors a phase advancement (the gate verdict, the
evidence bundle hashes it verified, the ledger position, and the envelope spend
at gate time) into the HMAC-chained audit log, so a phase pass is provable
offline from the chain alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import (
    EVENT_MISSION_PHASE_RECEIPT,
    AuditChainStore,
    record_mission_phase_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_record_mission_phase_receipt_chains_and_verifies(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit")
    event = record_mission_phase_receipt(
        chain=chain,
        mission_id="m-1",
        phase_id="p1",
        gate_passed=True,
        receipt_hash="sha256:" + "c" * 64,
        evidence_bundle_hashes=("sha256:" + "a" * 64,),
        ledger_seq=3,
        envelope="mission-m-1-p1",
        spend_usd=12.5,
        journal_entry_hash="deadbeef",
    )
    assert event.event_type == EVENT_MISSION_PHASE_RECEIPT
    assert event.details["mission_id"] == "m-1"
    assert event.details["gate_passed"] is True
    assert event.details["evidence_bundle_hashes"] == ["sha256:" + "a" * 64]
    assert event.details["ledger_seq"] == 3
    assert "prev_chain_digest" in event.details

    ok, errors = chain.verify()
    assert ok, errors


def test_halted_phase_receipt_records_reason(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit")
    event = record_mission_phase_receipt(
        chain=chain,
        mission_id="m-1",
        phase_id="p2",
        gate_passed=False,
        receipt_hash="sha256:" + "d" * 64,
        evidence_bundle_hashes=(),
        ledger_seq=7,
        envelope="mission-m-1-p2",
        spend_usd=25.0,
        journal_entry_hash="cafef00d",
        reason="envelope_exhausted",
    )
    assert event.details["reason"] == "envelope_exhausted"
    assert event.details["gate_passed"] is False
