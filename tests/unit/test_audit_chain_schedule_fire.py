"""Tests for the additive ``schedule.fire_projection`` chain helper (#2302).

Each recurring-goal fire is mirrored into the HMAC-chained audit log so
the fire's projection - schedule id, fire time, last-state hash, and the
canonical graph hash - is independently attestable, anchored to the
lineage-spine entry and (for a trigger fire) the trigger input hash. The
event embeds the prior chain digest so the record is chained.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_SCHEDULE_FIRE_PROJECTION,
    AuditChainStore,
    record_schedule_fire_projection,
)


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def test_record_schedule_fire_appends_chained_event(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_schedule_fire_projection(
        chain=chain,
        schedule_id="sched_a",
        fire_time=1_700_000_000,
        last_state_hash="genesis",
        graph_hash="c" * 64,
        journal_entry_hash="sha256:" + "d" * 64,
        recurrence="cron:0 9 * * *",
    )
    assert event.event_type == EVENT_SCHEDULE_FIRE_PROJECTION
    assert event.actor == "schedule_projection"
    assert event.resource_id == "sched_a"
    assert "prev_chain_digest" in event.details
    assert event.details["fire_time"] == 1_700_000_000
    assert event.details["graph_hash"] == "c" * 64
    assert event.details["last_state_hash"] == "genesis"
    assert event.details["journal_entry_hash"] == "sha256:" + "d" * 64
    assert event.details["trigger_input_hash"] == ""
    ok, errors = chain.verify()
    assert ok, errors


def test_trigger_input_hash_recorded(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    event = record_schedule_fire_projection(
        chain=chain,
        schedule_id="sched_hook",
        fire_time=1_700_000_500,
        last_state_hash="genesis",
        graph_hash="e" * 64,
        journal_entry_hash="sha256:" + "f" * 64,
        trigger_input_hash="sha256:" + "a" * 64,
    )
    assert event.details["trigger_input_hash"] == "sha256:" + "a" * 64
    ok, errors = chain.verify()
    assert ok, errors


def test_schedule_fire_events_chain(tmp_path: Path) -> None:
    chain = _store(tmp_path)
    first = record_schedule_fire_projection(
        chain=chain,
        schedule_id="s",
        fire_time=1,
        last_state_hash="genesis",
        graph_hash="1" * 64,
        journal_entry_hash="sha256:" + "1" * 64,
    )
    second = record_schedule_fire_projection(
        chain=chain,
        schedule_id="s",
        fire_time=2,
        last_state_hash="1" * 16,
        graph_hash="2" * 64,
        journal_entry_hash="sha256:" + "2" * 64,
    )
    # The second event's embedded prev digest is the first event's HMAC.
    assert second.details["prev_chain_digest"] == first.hmac
    ok, errors = chain.verify()
    assert ok, errors
