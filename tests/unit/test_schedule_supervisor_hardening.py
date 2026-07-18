"""Schedule supervisor and DST resolution hardening regressions (#2654).

One test per hardened finding:

- A spring-forward (imaginary) local time resolves to the real UTC
  transition instant rather than to the naive wall time minus the gap.
- The warm-resume checkpoint survives into the dispatched supersede fire.
- A ``CANCEL_NEW``-canceled fire window is not re-dispatched on a later tick.
- A collision receipt is linked to its audit-chain event and records a
  reproducible hash of the decision inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from bernstein.core.orchestration.collision import (
    CollisionAction,
    CollisionPolicy,
    RunningFireState,
)
from bernstein.core.orchestration.schedule_kinds import (
    DstPolicy,
    resolve_local_instant,
)
from bernstein.core.orchestration.schedule_supervisor import (
    COLLISION_EVENT_TYPE,
    ScheduleSupervisor,
    collision_inputs_hash,
)
from bernstein.core.planning.schedule_store import Schedule, ScheduleStore

if TYPE_CHECKING:
    from pathlib import Path

_TZ = "America/New_York"
#: 2026 spring-forward in America/New_York: 02:00 EST jumps to 03:00 EDT, so
#: the whole 02:00-02:59 wall-clock hour never happens.
_GAP = datetime(2026, 3, 8, 2, 30, 0)
#: The UTC instant the clock jumps at: 2026-03-08 07:00:00Z.
_TRANSITION_EPOCH = int(datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------
# spring-forward resolves to the real transition instant
# ---------------------------------------------------------------------------


class TestSpringForwardResolution:
    def test_post_transition_is_the_transition_instant(self) -> None:
        got = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.POST_TRANSITION)
        assert got == _TRANSITION_EPOCH
        # The clock reads 03:00 local the moment the gap closes.
        local = datetime.fromtimestamp(got, tz=ZoneInfo(_TZ))
        assert (local.hour, local.minute, local.second) == (3, 0, 0)

    def test_pre_transition_is_one_second_before_the_gap_opens(self) -> None:
        got = resolve_local_instant(tz_name=_TZ, naive_local=_GAP, dst_policy=DstPolicy.PRE_TRANSITION)
        assert got == _TRANSITION_EPOCH - 1
        local = datetime.fromtimestamp(got, tz=ZoneInfo(_TZ))
        assert (local.hour, local.minute, local.second) == (1, 59, 59)

    def test_resolution_is_independent_of_the_supplied_gap_offset(self) -> None:
        # Every imaginary wall time inside the same gap names the same
        # transition, so the resolved instant must not drift with it.
        early = resolve_local_instant(
            tz_name=_TZ,
            naive_local=datetime(2026, 3, 8, 2, 1, 0),
            dst_policy=DstPolicy.POST_TRANSITION,
        )
        late = resolve_local_instant(
            tz_name=_TZ,
            naive_local=datetime(2026, 3, 8, 2, 59, 0),
            dst_policy=DstPolicy.POST_TRANSITION,
        )
        assert early == late == _TRANSITION_EPOCH

    def test_half_hour_gap_zone_resolves_to_its_own_transition(self) -> None:
        # Lord Howe shifts by 30 minutes; the resolution must not assume an
        # hour-wide gap.
        tz = "Australia/Lord_Howe"
        gap = datetime(2026, 10, 4, 2, 15, 0)
        post = resolve_local_instant(tz_name=tz, naive_local=gap, dst_policy=DstPolicy.POST_TRANSITION)
        pre = resolve_local_instant(tz_name=tz, naive_local=gap, dst_policy=DstPolicy.PRE_TRANSITION)
        assert post - pre == 1
        local = datetime.fromtimestamp(post, tz=ZoneInfo(tz))
        assert (local.hour, local.minute) == (2, 30)


# ---------------------------------------------------------------------------
# supervisor fixtures
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
        payload = self._prev_hmac + json.dumps({"e": event_type, "d": details}, sort_keys=True)
        new_hmac = hashlib.sha256(payload.encode()).hexdigest()
        event = _StubAuditEvent(event_type=event_type, details=details, hmac=new_hmac)
        self.entries.append(event)
        self._prev_hmac = new_hmac
        return event


def _store_with_schedule(tmp_path: Path) -> tuple[ScheduleStore, Schedule]:
    store = ScheduleStore(tmp_path)
    schedule = store.add(cron="* * * * *", goal="Nightly", misfire_policy="skip")
    last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
    store.update_last_fire(schedule.id, float(last_fire))
    return store, store.get(schedule.id) or schedule


def _running(**kwargs: Any) -> Any:
    def _probe(_s: Schedule) -> RunningFireState:
        return RunningFireState(running=True, running_count=1, fire_id="prev", **kwargs)

    return _probe


# ---------------------------------------------------------------------------
# warm-resume checkpoint survives into the dispatched fire
# ---------------------------------------------------------------------------


class TestSupersedeCheckpointHandoff:
    def test_dispatched_supersede_fire_carries_the_checkpoint(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        dispatched: list[Any] = []
        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            _StubAuditLog(),
            running_probe=_running(checkpoint_event_hash="sha256:cp"),
            collision_policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)

        assert len(dispatched) == 1
        metadata = dispatched[0].metadata
        assert metadata["resume_from_checkpoint"] == "sha256:cp"
        assert metadata["warm_resume"] is True

    def test_cold_supersede_fire_reports_no_checkpoint(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        dispatched: list[Any] = []
        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            _StubAuditLog(),
            running_probe=_running(checkpoint_event_hash=""),
            collision_policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)

        assert len(dispatched) == 1
        assert dispatched[0].metadata["resume_from_checkpoint"] == ""
        assert dispatched[0].metadata["warm_resume"] is False

    def test_ordinary_fire_carries_no_checkpoint_keys(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        dispatched: list[Any] = []
        supervisor = ScheduleSupervisor(store, dispatched.append, _StubAuditLog())
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)

        assert len(dispatched) == 1
        assert "resume_from_checkpoint" not in dispatched[0].metadata


# ---------------------------------------------------------------------------
# a canceled window is not re-dispatched
# ---------------------------------------------------------------------------


class TestCanceledWindowIsProcessed:
    def test_canceled_window_is_not_redispatched_when_idle(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        dispatched: list[Any] = []
        blocked = {"value": True}

        def _probe(_s: Schedule) -> RunningFireState:
            if blocked["value"]:
                return RunningFireState(running=True, running_count=1, fire_id="prev")
            return RunningFireState(running=False)

        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            _StubAuditLog(),
            running_probe=_probe,
            collision_policy=CollisionPolicy.CANCEL_NEW,
        )
        first = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=first)
        assert dispatched == []

        # The previous fire finished; a later tick must not resurrect the
        # window that CANCEL_NEW already dropped.
        blocked["value"] = False
        supervisor.tick(now=first + 1)
        canceled_windows = [e.raw_payload["fire_time"] for e in dispatched]
        assert first not in canceled_windows
        assert all(t > first for t in canceled_windows)

    def test_enqueue_window_is_retried_when_idle(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        dispatched: list[Any] = []
        blocked = {"value": True}

        def _probe(_s: Schedule) -> RunningFireState:
            if blocked["value"]:
                return RunningFireState(running=True, running_count=1, fire_id="prev")
            return RunningFireState(running=False)

        supervisor = ScheduleSupervisor(
            store,
            dispatched.append,
            _StubAuditLog(),
            running_probe=_probe,
            collision_policy=CollisionPolicy.ENQUEUE,
        )
        first = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=first)
        assert dispatched == []

        # ENQUEUE defers rather than drops: the deferred window still fires.
        blocked["value"] = False
        supervisor.tick(now=first)
        assert len(dispatched) >= 1

    def test_canceled_window_advances_the_stored_last_fire(self, tmp_path: Path) -> None:
        store, schedule = _store_with_schedule(tmp_path)
        supervisor = ScheduleSupervisor(
            store,
            lambda _e: None,
            _StubAuditLog(),
            running_probe=_running(),
            collision_policy=CollisionPolicy.CANCEL_NEW,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)
        refreshed = store.get(schedule.id)
        assert refreshed is not None
        assert refreshed.last_fire_at >= now - 60

    def test_enqueue_window_does_not_advance_the_stored_last_fire(self, tmp_path: Path) -> None:
        store, schedule = _store_with_schedule(tmp_path)
        before = store.get(schedule.id)
        assert before is not None
        supervisor = ScheduleSupervisor(
            store,
            lambda _e: None,
            _StubAuditLog(),
            running_probe=_running(),
            collision_policy=CollisionPolicy.ENQUEUE,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)
        after = store.get(schedule.id)
        assert after is not None
        assert after.last_fire_at == before.last_fire_at


# ---------------------------------------------------------------------------
# collision receipts are chain-linked and reproducible
# ---------------------------------------------------------------------------


class TestCollisionReceiptLinkage:
    def test_receipt_is_linked_to_its_chain_event(self, tmp_path: Path) -> None:
        store, _schedule = _store_with_schedule(tmp_path)
        audit = _StubAuditLog()
        supervisor = ScheduleSupervisor(
            store,
            lambda _e: None,
            audit,
            running_probe=_running(),
            collision_policy=CollisionPolicy.CANCEL_NEW,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        receipts = supervisor.tick(now=now)

        collisions = [e for e in audit.entries if e.event_type == COLLISION_EVENT_TYPE]
        assert len(collisions) == 1
        collision_receipts = [r for r in receipts if not r.dispatched and not r.counterfactual]
        assert collision_receipts
        receipt = collision_receipts[0]
        # The receipt names the chain event it rode on, with its real
        # predecessor - not an empty prev and a bare decision hash.
        assert receipt.chain_digest == collisions[0].hmac
        assert receipt.prev_chain_digest == "0" * 64
        assert receipt.prev_chain_digest != receipt.chain_digest

    def test_decision_inputs_hash_is_reproducible(self, tmp_path: Path) -> None:
        store, schedule = _store_with_schedule(tmp_path)
        audit = _StubAuditLog()
        supervisor = ScheduleSupervisor(
            store,
            lambda _e: None,
            audit,
            running_probe=_running(checkpoint_event_hash="sha256:cp"),
            collision_policy=CollisionPolicy.SUPERSEDE_WITH_HANDOFF,
            concurrency_cap=1,
        )
        now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
        supervisor.tick(now=now)

        collisions = [e for e in audit.entries if e.event_type == COLLISION_EVENT_TYPE]
        assert len(collisions) == 1
        details = collisions[0].details
        assert details["action"] == str(CollisionAction.SUPERSEDE)

        recomputed = collision_inputs_hash(
            schedule_id=schedule.id,
            fire_time=int(details["fire_time"]),
            running_count=1,
            concurrency_cap=1,
            fire_id="prev",
            resume_from_checkpoint="sha256:cp",
        )
        assert details["decision_inputs_hash"] == recomputed

    def test_different_inputs_produce_different_hashes(self) -> None:
        base = {
            "schedule_id": "sched_a",
            "fire_time": 1_800_000_000,
            "running_count": 1,
            "concurrency_cap": 1,
            "fire_id": "prev",
            "resume_from_checkpoint": "",
        }
        assert collision_inputs_hash(**base) == collision_inputs_hash(**base)
        for key, value in (
            ("running_count", 2),
            ("concurrency_cap", 3),
            ("fire_id", "other"),
            ("resume_from_checkpoint", "sha256:cp"),
        ):
            variant = {**base, key: value}
            assert collision_inputs_hash(**variant) != collision_inputs_hash(**base), key


@pytest.mark.parametrize("policy", [CollisionPolicy.CANCEL_NEW, CollisionPolicy.ENQUEUE])
def test_no_second_graph_is_ever_dispatched_under_a_blocking_policy(
    tmp_path: Path,
    policy: CollisionPolicy,
) -> None:
    store, _schedule = _store_with_schedule(tmp_path)
    dispatched: list[Any] = []
    supervisor = ScheduleSupervisor(
        store,
        dispatched.append,
        _StubAuditLog(),
        running_probe=_running(),
        collision_policy=policy,
    )
    now = int(datetime(2030, 1, 1, 12, 3, 0, tzinfo=UTC).timestamp())
    supervisor.tick(now=now)
    assert dispatched == []
