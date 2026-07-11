"""Audit-chain anchor for the MCP Tasks extension run handle (issue #2364).

A run handle a stateless MCP client polls is anchored into the HMAC audit
chain: the handle's receipt hash, the run journal head, and the embedded
chain head are recorded so a verifier holding the chain can confirm the
task a client watched corresponds to the audited run.
"""

from __future__ import annotations

from bernstein.core.security.audit_chain import (
    EVENT_MCP_TASK_HANDLE,
    AuditChainStore,
    record_mcp_task_handle,
)


def test_task_handle_anchored_into_audit_chain(tmp_path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    event = record_mcp_task_handle(
        chain=chain,
        task_id="t1",
        run_id="run-2364",
        status="completed",
        journal_head="a" * 64,
        chain_head="b" * 64,
        receipt_hash="c" * 64,
        spec_revision="2026-07-28",
        trace_id="d" * 32,
    )
    assert event.event_type == EVENT_MCP_TASK_HANDLE
    assert event.details["run_id"] == "run-2364"
    assert event.details["receipt_hash"] == "c" * 64
    assert event.details["journal_head"] == "a" * 64
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors


def test_task_handle_tamper_breaks_chain(tmp_path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    record_mcp_task_handle(
        chain=chain,
        task_id="t1",
        run_id="run-2364",
        status="working",
        journal_head="a" * 64,
        chain_head="b" * 64,
        receipt_hash="c" * 64,
        spec_revision="2026-07-28",
        trace_id="",
    )
    log_path = next((tmp_path / "audit").glob("*.jsonl"))
    raw = log_path.read_text(encoding="utf-8")
    tampered = raw.replace("working", "completed")
    assert tampered != raw
    log_path.write_text(tampered, encoding="utf-8")
    chain2 = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    ok, _errors = chain2.verify()
    assert not ok
