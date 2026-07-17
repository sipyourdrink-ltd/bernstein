"""Collision receipts and supersede handoff (#2546, AC3 + AC4).

- Collision outcomes are a pure, deterministic function of ``(policy,
  running-fire state, cap)`` with a stable receipt hash (AC3).
- A supervisor tick over an unfinished previous fire with ``CANCEL_NEW``
  never dispatches a second task graph (AC3, tick-level regression).
- Under ``SUPERSEDE_WITH_HANDOFF`` the new fire resumes from the checkpoint
  row recorded in the superseded run's journal; a tampered checkpoint row
  produces a cold start, never a warm resume (AC4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.collision import (
    CollisionAction,
    CollisionPolicy,
    RunningFireState,
    decide_collision,
    resolve_handoff_checkpoint,
)
from bernstein.core.orchestration.schedule_supervisor import (
    COLLISION_EVENT_TYPE,
    ScheduleSupervisor,
)
from bernstein.core.planning.schedule_store import Schedule, ScheduleStore
from bernstein.core.tasks.checkpoint_retry import (
    record_task_checkpoint,
    task_run_id,
    workspace_hash,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# AC3: collision outcome is a pure deterministic function
# ---------------------------------------------------------------------------


class TestDecideCollisionPurity:
    def test_no_collision_dispatches(self) -> None:
        idle = RunningFireState(running=False)
        decision = decide_collision(policy=CollisionPolicy.CANCEL_NEW, running=idle)
        assert decision.action is CollisionAction.DISPATCH
        assert decision.dispatch is True

    def test_cancel_new_never_dispatches_over_running(self) -> None:
        running = RunningFireState(running=True, running_count=1, fire_id="f1")
        decision = decide_collision(policy=CollisionPolicy.CANCEL_NEW, running=running)
        assert decision.action is CollisionAction.CANCEL
        assert decision.dispatch is False

    def test_enqueue_defers(self) -> None:
        running = RunningFireState(running=True, running_count=1, fire_id="f1")
        decision = decide_collision(policy=CollisionPolicy.ENQUEUE, running=running)
        assert decision.action is CollisionAction.ENQUEUE
        assert decision.dispatch is False

    def test_receipt_hash_is_stable_across_operators(self) -> None:
        running = RunningFireState(running=True, running_count=1, fire_id="f1", checkpoint_event_hash="sha256:cp")
        d1 = decide_collision(policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF, running=running)
        d2 = decide_collision(policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF, running=running)
        assert d1.receipt_hash == d2.receipt_hash
        assert d1.canonical_bytes == d2.canonical_bytes

    def test_different_running_state_changes_receipt(self) -> None:
        a = decide_collision(
            policy=CollisionPolicy.CANCEL_NEW,
            running=RunningFireState(running=True, running_count=1, fire_id="f1"),
        )
        b = decide_collision(
            policy=CollisionPolicy.CANCEL_NEW,
            running=RunningFireState(running=True, running_count=2, fire_id="f1"),
        )
        assert a.receipt_hash != b.receipt_hash

    def test_under_cap_dispatches_even_when_running(self) -> None:
        running = RunningFireState(running=True, running_count=1, fire_id="f1")
        decision = decide_collision(policy=CollisionPolicy.CANCEL_NEW, running=running, concurrency_cap=2)
        assert decision.dispatch is True


# ---------------------------------------------------------------------------
# AC3: supervisor tick over an unfinished previous fire (regression)
# ---------------------------------------------------------------------------


@dataclass
class _StubAuditEvent:
    event_type: str
    details: dict[str, Any]
    hmac: str


@dataclass
class _StubAuditLog:
    entries: list[_StubAuditEvent] = field(default_factory=list)
    _prev_hmac: str = "0" * 64

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> _StubAuditEvent:
        import hashlib

        payload = self._prev_hmac + json.dumps({"e": event_type, "d": details}, sort_keys=True)
        new_hmac = hashlib.sha256(payload.encode()).hexdigest()
        event = _StubAuditEvent(event_type=event_type, details=details, hmac=new_hmac)
        self.entries.append(event)
        self._prev_hmac = new_hmac
        return event


class TestSupervisorDoubleFire:
    def test_cancel_new_never_dispatches_second_graph(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly", misfire_policy="skip")
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        dispatched: list[Any] = []

        # The previous fire is still running.
        def _probe(_s: Schedule) -> RunningFireState:
            return RunningFireState(running=True, running_count=1, fire_id="prev")

        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            audit,
            running_probe=_probe,
            collision_policy=CollisionPolicy.CANCEL_NEW,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        receipts = supervisor.tick(now=now)

        # No task graph dispatched while a previous fire is in flight...
        assert dispatched == []
        # ...but a collision receipt was recorded (the overlap is not silent).
        collision_entries = [e for e in audit.entries if e.event_type == COLLISION_EVENT_TYPE]
        assert len(collision_entries) == 1
        assert collision_entries[0].details["action"] == str(CollisionAction.CANCEL)
        assert any(not r.dispatched for r in receipts)

    def test_no_probe_preserves_dispatch(self, tmp_path: Path) -> None:
        # Without a running probe the historical dispatch path is unchanged.
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly", misfire_policy="skip")
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        dispatched: list[Any] = []
        supervisor = ScheduleSupervisor(store, dispatched.append, audit)
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)
        assert len(dispatched) == 1

    def test_supersede_still_dispatches_with_receipt(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly", misfire_policy="skip")
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        dispatched: list[Any] = []

        def _probe(_s: Schedule) -> RunningFireState:
            return RunningFireState(running=True, running_count=1, fire_id="prev", checkpoint_event_hash="sha256:cp")

        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            audit,
            running_probe=_probe,
            collision_policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)
        assert len(dispatched) == 1
        collision_entries = [e for e in audit.entries if e.event_type == COLLISION_EVENT_TYPE]
        assert len(collision_entries) == 1
        assert collision_entries[0].details["warm_resume"] is True


# ---------------------------------------------------------------------------
# AC4: supersede handoff resumes from the recorded checkpoint (or cold)
# ---------------------------------------------------------------------------


def _make_worktree(root: Path) -> Path:
    tree = root / "wt"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "file.txt").write_text("content", encoding="utf-8")
    return tree


class TestSupersedeHandoff:
    def test_warm_resume_from_recorded_checkpoint(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        ref = record_task_checkpoint(
            sdd_dir=sdd,
            task_id="running-fire",
            adapter="claude",
            session_id="sess-1",
            workspace_hash=workspace_hash(tree),
            worktree_path=str(tree),
        )
        resume_from = resolve_handoff_checkpoint(sdd, "running-fire")
        assert resume_from == ref.event_hash

        decision = decide_collision(
            policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
            running=RunningFireState(
                running=True,
                running_count=1,
                fire_id="running-fire",
                checkpoint_event_hash=resume_from,
            ),
        )
        assert decision.action is CollisionAction.SUPERSEDE
        assert decision.dispatch is True
        assert decision.warm_resume is True
        assert decision.resume_from_checkpoint == ref.event_hash

    def test_tampered_checkpoint_downgrades_to_cold_start(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        tree = _make_worktree(tmp_path)
        record_task_checkpoint(
            sdd_dir=sdd,
            task_id="running-fire",
            adapter="claude",
            session_id="sess-1",
            workspace_hash=workspace_hash(tree),
            worktree_path=str(tree),
        )
        # Tamper the checkpoint row: the Merkle chain no longer verifies.
        journal = sdd / "runs" / task_run_id("running-fire") / "journal.jsonl"
        row = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
        row["session_id"] = "attacker-session"
        journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

        resume_from = resolve_handoff_checkpoint(sdd, "running-fire")
        assert resume_from == ""

        decision = decide_collision(
            policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
            running=RunningFireState(
                running=True,
                running_count=1,
                fire_id="running-fire",
                checkpoint_event_hash=resume_from,
            ),
        )
        # Supersede still proceeds, but cold: never a warm resume of the
        # attacker-chosen session.
        assert decision.dispatch is True
        assert decision.warm_resume is False
        assert decision.resume_from_checkpoint == ""

    def test_missing_checkpoint_is_cold(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        assert resolve_handoff_checkpoint(sdd, "never-checkpointed") == ""
