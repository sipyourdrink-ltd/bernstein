"""Tests for the additive ``thread.approval`` audit-chain helper (issue #2297).

An approval issued over the live event stream is itself a signed record:
it anchors the operator's decision to the exact journal entry hash the
stream projected at decision time, so a verifier can prove the approval
was made against the executed thread and not a divergent view (AC4). The
event embeds the previous chain digest, so it is HMAC-chained like every
other audit record.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_THREAD_APPROVAL,
    AuditChainStore,
    record_thread_approval,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_thread_approval_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_thread_approval(
        chain=chain,
        run_id="run-1",
        journal_index=7,
        event_hash="a" * 64,
        decision="approve",
        operator_install_id_sig="sig-abc",
        worktree_id="wt-1",
    )
    assert event.event_type == EVENT_THREAD_APPROVAL
    assert event.actor == "sig-abc"
    assert event.resource_id == "run-1"
    assert event.details["journal_index"] == 7
    assert event.details["event_hash"] == "a" * 64
    assert event.details["decision"] == "approve"
    assert event.details["worktree_id"] == "wt-1"
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors


def test_record_thread_approval_chains_prev_digest(tmp_path: Path) -> None:
    """A second approval embeds the first event's HMAC as prev digest."""
    chain = _store(tmp_path)
    first = record_thread_approval(
        chain=chain,
        run_id="run-1",
        journal_index=1,
        event_hash="b" * 64,
        decision="approve",
        operator_install_id_sig="sig-1",
        worktree_id="wt-1",
    )
    second = record_thread_approval(
        chain=chain,
        run_id="run-1",
        journal_index=2,
        event_hash="c" * 64,
        decision="reject",
        operator_install_id_sig="sig-1",
        worktree_id="wt-1",
    )
    assert first.details["prev_chain_digest"] != second.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
