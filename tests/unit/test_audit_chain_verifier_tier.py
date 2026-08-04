"""Audit-chain mirror for verifier-ladder tier records (issue #2927).

Each verifier tier that runs is sealed into the ``verifier-ladder`` lineage
spine; its identity is also mirrored into the HMAC-chained audit log so an
operator can prove, from the chain alone, which tiers executed against which
evidence. Only hashes and the verdict are recorded -- never the raw diff,
rubric, or model output.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_VERIFIER_TIER,
    AuditChainStore,
    record_verifier_tier,
)


def _record(chain: AuditChainStore) -> None:
    record_verifier_tier(
        chain=chain,
        receipt_hash="sha256:" + "a" * 64,
        task_id="T-001",
        tier="judge",
        config_hash="sha256:" + "b" * 64,
        inputs_hash="sha256:" + "c" * 64,
        evidence_hash="sha256:" + "d" * 64,
        verdict="pass",
        spine_entry_hash="sha256:" + "e" * 64,
    )


def test_record_verifier_tier_appends_event_with_prev_digest(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    _record(chain)
    rows = chain.query(event_type=EVENT_VERIFIER_TIER)
    assert len(rows) == 1
    details = rows[0].details
    assert details["tier"] == "judge"
    assert details["verdict"] == "pass"
    assert details["receipt_hash"] == "sha256:" + "a" * 64
    assert details["spine_entry_hash"] == "sha256:" + "e" * 64
    assert "prev_chain_digest" in details
    ok, errors = chain.verify()
    assert ok, errors


def test_record_verifier_tier_records_only_hashes(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    _record(chain)
    rows = chain.query(event_type=EVENT_VERIFIER_TIER)
    # A closed key set: hashes, identifiers, and the verdict. Never a raw
    # diff, rubric body, or model output.
    assert set(rows[0].details) <= {
        "receipt_hash",
        "task_id",
        "tier",
        "config_hash",
        "inputs_hash",
        "evidence_hash",
        "verdict",
        "spine_entry_hash",
        "prev_chain_digest",
    }
