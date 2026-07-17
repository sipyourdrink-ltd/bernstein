"""Offline verification of resolved approval cards (issue #2511, AC4)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.approval.card import build_card
from bernstein.core.approval.card_gate import ApprovalCardGate
from bernstein.core.approval.card_verify import verify_approval_cards
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    AuditChainStore,
)

_KEY = b"deterministic-test-key-2511"


def _card(approval_id: str = "ap-1", *, created_at: float = 1_000.0, ttl: float = 600.0):
    return build_card(
        approval_id=approval_id,
        tool_name="Edit",
        tool_args={"file_path": "src/app.py", "new_string": "x = 1"},
        reasoning="Add a constant.",
        created_at=created_at,
        ttl_seconds=ttl,
    )


def _issue_and_resolve(tmp_path: Path) -> str:
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    gate = ApprovalCardGate(chain)
    issued = gate.issue(_card())
    gate.resolve(card_hash=issued.card_hash, decision="approve", approver="U7", now=1_100.0)
    return issued.card_hash


def test_verify_passes_for_clean_resolved_card(tmp_path: Path) -> None:
    _issue_and_resolve(tmp_path)
    result = verify_approval_cards(tmp_path / "audit", key=_KEY)
    assert result.ok, result.errors
    assert result.issued_count == 1
    assert result.reconstructed_count == 1


def test_verify_noop_when_no_cards(tmp_path: Path) -> None:
    # A chain with unrelated activity but no approval cards must pass silently.
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    chain.log(event_type="unrelated.event", actor="x", resource_type="thing", resource_id="1", details={})
    result = verify_approval_cards(tmp_path / "audit", key=_KEY)
    assert result.ok
    assert result.issued_count == 0
    assert result.resolved_count == 0


def test_verify_detects_post_hoc_envelope_mutation(tmp_path: Path) -> None:
    _issue_and_resolve(tmp_path)
    audit_dir = tmp_path / "audit"
    log_files = sorted(audit_dir.glob("*.jsonl"))
    assert log_files, "expected an audit jsonl file"
    path = log_files[0]

    # Tamper the stored envelope's impact score, leaving card_hash unchanged.
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = False
    out: list[str] = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("event_type") == EVENT_APPROVAL_CARD_ISSUED:
            entry["details"]["envelope"]["impact"]["score"] = 0.999
            tampered = True
        out.append(json.dumps(entry, sort_keys=True))
    assert tampered
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

    result = verify_approval_cards(audit_dir, key=_KEY)
    assert not result.ok
    assert any("mutated" in err for err in result.errors)


def test_verify_detects_resolved_without_issued(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_KEY)
    # A resolved event that references a card_hash never issued must fail.
    chain.log_with_prev_digest(
        event_type="chat.approval_card.resolved",
        actor="U7",
        resource_type="approval_card",
        resource_id="deadbeef",
        details={"card_hash": "deadbeef", "decision": "approve", "resolved_at": 1_100.0},
    )
    result = verify_approval_cards(audit_dir, key=_KEY)
    assert not result.ok
    assert any("no matching issued" in err for err in result.errors)
