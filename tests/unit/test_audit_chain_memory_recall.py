"""Tests for the additive ``memory.recall`` audit event (issue #2914)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_MEMORY_RECALL,
    AuditChainStore,
    record_memory_recall,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_memory_recall_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_memory_recall(
        chain=chain,
        receipt_id="sha256:" + "11" * 32,
        lineage_entry_hash="sha256:" + "22" * 32,
        scope="user",
        namespace="alex",
        actor="agent:worker",
        run_id="run-1",
        step_id="s1",
        query_hash="sha256:" + "33" * 32,
        fold_head="sha256:" + "44" * 32,
        fold_hash="sha256:" + "55" * 32,
        records_hash="sha256:" + "66" * 32,
        record_count=2,
    )

    assert event.event_type == EVENT_MEMORY_RECALL
    assert event.resource_id == "sha256:" + "11" * 32
    assert event.details["record_count"] == 2
    assert event.details["lineage_entry_hash"] == "sha256:" + "22" * 32
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors


def test_record_memory_recall_does_not_copy_query_or_claim_text(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_memory_recall(
        chain=chain,
        receipt_id="sha256:" + "11" * 32,
        lineage_entry_hash="sha256:" + "22" * 32,
        scope="user",
        namespace="alex",
        actor="agent:worker",
        run_id="run-1",
        step_id="s1",
        query_hash="sha256:" + "33" * 32,
        fold_head="sha256:" + "44" * 32,
        fold_hash="sha256:" + "55" * 32,
        records_hash="sha256:" + "66" * 32,
        record_count=1,
    )

    encoded = json.dumps(event.details, sort_keys=True)
    assert "query" not in event.details
    assert "claim" not in event.details
    assert "prefers dark mode" not in encoded
