"""Tests for the additive ``subagent.delegation`` audit-chain helper (issue #2308).

Each native-subagent leaf of a deterministic outer plan is mirrored into the
HMAC-chained audit log so an operator can confirm, from the chain alone, that a
delegation boundary was crossed for a named plan node and that a specific
result-content hash was anchored to a run journal entry. The event records only
hashes and identifiers -- never the native result payload -- and embeds the
previous chain digest so the record is chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_SUBAGENT_DELEGATION,
    AuditChainStore,
    record_subagent_delegation,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_subagent_delegation_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_subagent_delegation(
        chain=chain,
        run_id="run-1",
        node_name="audit-0",
        target="claude",
        node_hash="ab" * 32,
        result_content_hash="cd" * 32,
        journal_index=4,
        journal_event_hash="ef" * 32,
        tier="batch",
    )
    assert event.event_type == EVENT_SUBAGENT_DELEGATION
    assert event.resource_id == "audit-0"
    assert "prev_chain_digest" in event.details
    assert event.details["run_id"] == "run-1"
    assert event.details["target"] == "claude"
    assert event.details["node_hash"] == "ab" * 32
    assert event.details["result_content_hash"] == "cd" * 32
    assert event.details["journal_index"] == 4
    assert event.details["journal_event_hash"] == "ef" * 32
    assert event.details["tier"] == "batch"
    ok, errors = chain.verify()
    assert ok, errors


def test_record_subagent_delegation_defaults_tier(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_subagent_delegation(
        chain=chain,
        run_id="run-1",
        node_name="audit-0",
        target="codex",
        node_hash="ab" * 32,
        result_content_hash="cd" * 32,
        journal_index=0,
        journal_event_hash="ef" * 32,
    )
    assert event.details["tier"] == "interactive"
    assert chain.verify()[0]
