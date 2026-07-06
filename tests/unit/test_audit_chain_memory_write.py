"""Tests for the additive ``memory.write`` audit-chain helper (issue #2298).

Each cross-session memory write is mirrored into the HMAC-chained audit
log so an operator can reconstruct, from the audit chain alone, that a
fact was written by an actor at a time and anchored to a lineage-spine
entry. The event records only hashes and identifiers -- never the claim
content -- and embeds the previous chain digest so the record is chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_MEMORY_WRITE,
    AuditChainStore,
    record_memory_write,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_memory_write_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_memory_write(
        chain=chain,
        entry_hash="sha256:" + "ab" * 32,
        source_hash="sha256:" + "cd" * 32,
        scope="user",
        namespace="alex",
        actor="agent:worker",
        run_id="run-1",
        step_id="s1",
        kind="write",
    )
    assert event.event_type == EVENT_MEMORY_WRITE
    assert event.actor == "agent:worker"
    assert event.resource_id == "sha256:" + "ab" * 32
    assert "prev_chain_digest" in event.details
    assert event.details["source_hash"] == "sha256:" + "cd" * 32
    assert event.details["scope"] == "user"
    ok, errors = chain.verify()
    assert ok, errors


def test_record_memory_write_records_no_claim_content(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_memory_write(
        chain=chain,
        entry_hash="sha256:" + "ab" * 32,
        source_hash="sha256:" + "cd" * 32,
        scope="user",
        namespace="alex",
        actor="agent:worker",
        run_id="run-1",
        step_id="s1",
        kind="write",
    )
    # Only hashes and identifiers -- never the remembered claim text.
    assert "claim" not in event.details


def test_record_memory_write_tombstone_kind(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_memory_write(
        chain=chain,
        entry_hash="sha256:" + "ef" * 32,
        source_hash="sha256:" + "cd" * 32,
        scope="user",
        namespace="alex",
        actor="agent:worker",
        run_id="run-1",
        step_id="s2",
        kind="tombstone",
    )
    assert event.details["kind"] == "tombstone"
    ok, _ = chain.verify()
    assert ok
