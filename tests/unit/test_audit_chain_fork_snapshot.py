"""Tests for the additive ``replay.fork_snapshot`` audit-chain helper (#2295).

Each ``bernstein fork --from-step`` is mirrored into the HMAC-chained
audit log so the fork lineage - parent run, fork step, snapshot commit
sha, child run - is independently attestable. The event records only
identifiers and the content-addressed snapshot sha, and embeds the prior
chain digest so the record is chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_FORK_SNAPSHOT,
    AuditChainStore,
    record_fork_snapshot,
)


def _store(tmp_path: Path) -> AuditChainStore:
    # Audit dir and the HMAC key are both scoped to tmp_path so no state
    # leaks across tests (per-test key/state isolation).
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_fork_snapshot_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    sha = "a" * 40
    event = record_fork_snapshot(
        chain=chain,
        parent_run_id="parent-1",
        fork_step=3,
        snapshot_sha=sha,
        new_run_id="fork-parent-1-s3-abcd",
    )
    assert event.event_type == EVENT_FORK_SNAPSHOT
    assert event.actor == "replay_fork"
    assert event.resource_id == sha
    assert "prev_chain_digest" in event.details
    assert event.details["parent_run_id"] == "parent-1"
    assert event.details["fork_step"] == 3
    assert event.details["snapshot_sha"] == sha
    assert event.details["new_run_id"] == "fork-parent-1-s3-abcd"
    ok, errors = chain.verify()
    assert ok, errors


def test_fork_snapshot_events_chain(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    first = record_fork_snapshot(
        chain=chain,
        parent_run_id="p",
        fork_step=0,
        snapshot_sha="1" * 40,
        new_run_id="c1",
    )
    second = record_fork_snapshot(
        chain=chain,
        parent_run_id="p",
        fork_step=1,
        snapshot_sha="2" * 40,
        new_run_id="c2",
    )
    # The second event embeds the first's chain digest, so the two are linked.
    assert second.details["prev_chain_digest"] != first.details["prev_chain_digest"]
    ok, errors = chain.verify()
    assert ok, errors
