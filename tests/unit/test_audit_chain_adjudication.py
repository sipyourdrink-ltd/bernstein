"""Tests for the additive ``gate.adjudication`` audit-chain helper (issue #2294).

Each signed adjudication record produced by a maker-checker or judge-panel gate
is mirrored into the HMAC-chained audit log so an operator can confirm, from
the chain alone, that a gate verdict bound the claimed inputs, rubric, and
panel. The event records only hashes and the journal anchor -- never the raw
diff or rubric content -- and embeds the previous chain digest so the record is
chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_GATE_ADJUDICATION,
    AuditChainStore,
    record_gate_adjudication,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_gate_adjudication_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_gate_adjudication(
        chain=chain,
        run_id="run-1",
        inputs_hash="sha256:" + "ab" * 32,
        rubric_hash="sha256:" + "cd" * 32,
        panel_config_hash="sha256:" + "ef" * 32,
        final_verdict="pass",
        journal_entry_hash="sha256:" + "12" * 32,
    )
    assert event.event_type == EVENT_GATE_ADJUDICATION
    assert event.resource_id == "run-1"
    assert "prev_chain_digest" in event.details
    assert event.details["inputs_hash"] == "sha256:" + "ab" * 32
    assert event.details["rubric_hash"] == "sha256:" + "cd" * 32
    assert event.details["panel_config_hash"] == "sha256:" + "ef" * 32
    assert event.details["final_verdict"] == "pass"
    assert event.details["journal_entry_hash"] == "sha256:" + "12" * 32
    ok, errors = chain.verify()
    assert ok, errors
