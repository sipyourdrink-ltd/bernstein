"""Audit-chain event tests for governance decisions (issue #2309)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_GOVERNANCE_DECISION,
    AuditChainStore,
    record_governance_decision,
)

_KEY = b"0" * 32


def test_record_governance_decision_embeds_binding_and_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_governance_decision(
        chain=chain,
        subject="alice",
        action="tasks:write",
        verdict="allow",
        inputs_hash="sha256:" + "a" * 64,
        journal_entry_hash="sha256:" + "b" * 64,
        run_id="run-1",
    )
    assert ev.event_type == EVENT_GOVERNANCE_DECISION
    assert ev.details["subject"] == "alice"
    assert ev.details["action"] == "tasks:write"
    assert ev.details["verdict"] == "allow"
    assert ev.details["inputs_hash"] == "sha256:" + "a" * 64
    assert ev.details["journal_entry_hash"] == "sha256:" + "b" * 64
    assert ev.details["prev_chain_digest"]  # chain head embedded
    ok, errors = chain.verify()
    assert ok, errors


def test_record_refusal_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_governance_decision(
        chain=chain,
        subject="alice",
        action="budget",
        verdict="refuse",
        inputs_hash="sha256:" + "c" * 64,
        journal_entry_hash="sha256:" + "d" * 64,
        run_id="run-1",
    )
    assert ev.details["verdict"] == "refuse"
    ok, _ = chain.verify()
    assert ok
