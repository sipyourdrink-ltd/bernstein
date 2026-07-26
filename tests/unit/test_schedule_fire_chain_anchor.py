"""A schedule fire must record the chain head it was actually written with (#3131).

``ScheduleSupervisor`` captured the head and appended the entry as two steps.
The capture read the writer's per-instance cache, which does not see another
process's appends, while the append re-syncs the tail from disk under the
cross-process lock. Every fire entry and every fire receipt written while
another writer was active therefore named a chain position the record does not
occupy - and because entry and receipt were built from the same stale read they
agreed with each other, so ``verify_fire_chain`` passed.

No thread race is needed to show it: a second ``AuditLog`` on the same audit
directory is exactly what "another process" means to the writer's cache.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bernstein.core.orchestration.schedule_supervisor import (
    AUDIT_EVENT_TYPE,
    COLLISION_EVENT_TYPE,
    RunningFireState,
    ScheduleSupervisor,
    load_receipts,
)
from bernstein.core.planning.schedule_store import ScheduleStore
from bernstein.core.security.audit import AuditLog

KEY = b"s" * 32


def _audit(tmp_path: Path) -> AuditLog:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return AuditLog(audit_dir=audit_dir, key=KEY)


def _seed(sdd_dir: Path, *, cron: str = "* * * * *") -> str:
    store = ScheduleStore(sdd_dir)
    schedule = store.add(cron=cron, goal="Send nightly digest", misfire_policy="catch_up")
    store.update_last_fire(schedule.id, float(int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())))
    return schedule.id


def _entries(tmp_path: Path, event_type: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        for line in path.read_bytes().split(b"\n"):
            if not line:
                continue
            record = json.loads(line)
            if record.get("event_type") == event_type:
                out.append(record)
    return out


def test_fire_entry_embeds_the_head_it_chains_onto(tmp_path: Path) -> None:
    """AC 2 and 3: read the head from disk, and record the one actually used."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    audit = _audit(tmp_path)
    supervisor = ScheduleSupervisor(ScheduleStore(sdd_dir), lambda _event: None, audit, catch_up_limit=10)

    # Another writer on the same audit directory moves the head on disk. The
    # supervisor's writer still holds its own cached value.
    AuditLog(audit_dir=tmp_path / "audit", key=KEY).log("other.process", "x", "r", "r0", {})

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    entries = _entries(tmp_path, AUDIT_EVENT_TYPE)
    assert entries, "the tick must have fired"
    for entry in entries:
        assert entry["details"]["prev_chain_digest"] == entry["prev_hmac"]


def test_fire_receipt_anchor_matches_the_entry_it_rode_on(tmp_path: Path) -> None:
    """The receipt is a projection of the record, so it must name the same head."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    audit = _audit(tmp_path)
    supervisor = ScheduleSupervisor(ScheduleStore(sdd_dir), lambda _event: None, audit, catch_up_limit=10)
    AuditLog(audit_dir=tmp_path / "audit", key=KEY).log("other.process", "x", "r", "r0", {})

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    entries = _entries(tmp_path, AUDIT_EVENT_TYPE)
    by_digest = {entry["hmac"]: entry for entry in entries}
    receipts = [r for r in load_receipts(sdd_dir) if r.dispatched]
    assert receipts
    for receipt in receipts:
        entry = by_digest[receipt.chain_digest]
        assert receipt.prev_chain_digest == entry["prev_hmac"]


def test_two_fires_in_one_tick_each_carry_their_own_head(tmp_path: Path) -> None:
    """AC 4: a catch-up burst must not sign one head for every fire."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    audit = _audit(tmp_path)
    supervisor = ScheduleSupervisor(ScheduleStore(sdd_dir), lambda _event: None, audit, catch_up_limit=10)

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    entries = _entries(tmp_path, AUDIT_EVENT_TYPE)
    assert len(entries) >= 2, "the fixture must produce a catch-up burst"
    anchors = [entry["details"]["prev_chain_digest"] for entry in entries]
    assert len(set(anchors)) == len(anchors), "each fire must carry its own head"
    for entry in entries:
        assert entry["details"]["prev_chain_digest"] == entry["prev_hmac"]


def test_collision_receipt_embeds_the_head_the_entry_chains_onto(tmp_path: Path) -> None:
    """The collision path captures the head the same way and must be fixed too."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    audit = _audit(tmp_path)

    def _probe(_schedule: Any) -> RunningFireState:
        return RunningFireState(running=True, running_count=4, fire_id="fire-1")

    supervisor = ScheduleSupervisor(
        ScheduleStore(sdd_dir),
        lambda _event: None,
        audit,
        catch_up_limit=10,
        running_probe=_probe,
        concurrency_cap=1,
        collision_policy="enqueue",
    )
    AuditLog(audit_dir=tmp_path / "audit", key=KEY).log("other.process", "x", "r", "r0", {})

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    entries = _entries(tmp_path, COLLISION_EVENT_TYPE)
    assert entries, "the fixture must produce a collision entry"
    by_digest = {entry["hmac"]: entry for entry in entries}
    receipts = [r for r in load_receipts(sdd_dir) if r.chain_digest in by_digest]
    assert receipts
    for receipt in receipts:
        assert receipt.prev_chain_digest == by_digest[receipt.chain_digest]["prev_hmac"]


def test_fire_entries_pass_chain_verification(tmp_path: Path) -> None:
    """A fire entry naming a false predecessor must not verify (ties to #3062)."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    audit = _audit(tmp_path)
    supervisor = ScheduleSupervisor(ScheduleStore(sdd_dir), lambda _event: None, audit, catch_up_limit=10)
    AuditLog(audit_dir=tmp_path / "audit", key=KEY).log("other.process", "x", "r", "r0", {})

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    ok, errors = AuditLog(audit_dir=tmp_path / "audit", key=KEY).verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# duck-typed writers
# ---------------------------------------------------------------------------


class _StubWriter:
    """An in-memory chain with no transaction support, as tests inject."""

    def __init__(self) -> None:
        self._prev_hmac = ""
        self.entries: list[dict[str, Any]] = []

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> Any:
        digest = f"stub-{len(self.entries)}"
        self.entries.append(
            {
                "event_type": event_type,
                "actor": actor,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details,
                "prev_hmac": self._prev_hmac,
                "hmac": digest,
            }
        )
        self._prev_hmac = digest
        return type("_Event", (), {"hmac": digest})()


def test_a_stub_writer_without_transaction_support_still_fires(tmp_path: Path) -> None:
    """AC 5: degrade, and say so, rather than crash or pretend."""
    sdd_dir = tmp_path / "sdd"
    sdd_dir.mkdir(parents=True)
    _seed(sdd_dir)
    writer = _StubWriter()
    supervisor = ScheduleSupervisor(ScheduleStore(sdd_dir), lambda _event: None, writer, catch_up_limit=10)

    supervisor.tick(now=int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp()))

    assert writer.entries, "the stub writer must still receive the fire entries"
    for entry in writer.entries:
        assert entry["details"]["prev_chain_digest"] == entry["prev_hmac"]


def test_degradation_to_a_cache_read_is_explicit(tmp_path: Path) -> None:
    """The adapter must report whether it can hold a section, not hide it."""
    from bernstein.core.orchestration.schedule_supervisor import _AuditChainAdapter

    assert not _AuditChainAdapter(_StubWriter()).supports_transaction
    assert _AuditChainAdapter(_audit(tmp_path)).supports_transaction
