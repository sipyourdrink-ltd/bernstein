"""Mission digest receipt audit-chain event tests (#2510).

The mission digest receipt anchors one recurring digest fire (the digest hash,
the mission status hash, counts, and spend) into the HMAC-chained audit log, so
a posted digest is provable offline: a recipient recomputes the digest from the
ledger and confirms it matches the chain-attested receipt, and a tampered
receipt fails chain verification at its exact position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import (
    EVENT_MISSION_DIGEST_RECEIPT,
    AuditChainStore,
    record_mission_digest_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path


def _record(chain: AuditChainStore) -> None:
    record_mission_digest_receipt(
        chain=chain,
        mission_id="m-1",
        fire_time=1_700_000_000,
        digest_hash="a" * 64,
        receipt_id="missiondigest-abc123",
        mission_status_hash="b" * 64,
        ledger_head="c" * 64,
        phases_passed=2,
        gates_passed=2,
        gates_failed=0,
        total_spend_usd=21.0,
        schedule_id="mission-digest:m-1",
        recurrence="cron:0 8 * * *",
        fire_graph_hash="d" * 64,
    )


def test_record_mission_digest_receipt_chains_and_verifies(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit")
    _record(chain)
    events = chain.query(event_type=EVENT_MISSION_DIGEST_RECEIPT)
    assert len(events) == 1
    details = events[0].details
    assert details["mission_id"] == "m-1"
    assert details["digest_hash"] == "a" * 64
    assert details["receipt_id"] == "missiondigest-abc123"
    assert details["total_spend_usd"] == 21.0
    assert details["fire_graph_hash"] == "d" * 64
    assert "prev_chain_digest" in details

    ok, errors = chain.verify()
    assert ok, errors


def test_tampered_digest_receipt_fails_verification_at_position(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir)
    _record(chain)
    assert chain.verify()[0] is True

    # Tamper: flip the recorded digest hash in the on-disk chain entry.
    (log_file,) = list(audit_dir.glob("*.jsonl"))
    raw = log_file.read_text(encoding="utf-8")
    tampered = raw.replace("a" * 64, "e" * 64)
    assert tampered != raw
    log_file.write_text(tampered, encoding="utf-8")

    ok, errors = AuditChainStore(audit_dir).verify()
    assert ok is False
    assert errors
