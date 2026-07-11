"""Unit tests for the schedule supervisor (#1798).

Covers:

- Cron iteration math (``_next_fire_after``).
- Supervisor tick: skip vs catch_up misfire policy.
- Trigger source ``normalize_schedule_fire``.
- Audit-chain integration shape (event_type, payload keys).
- Doctor status snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.orchestration.schedule_projection import project_schedule_fire
from bernstein.core.orchestration.schedule_supervisor import (
    AUDIT_EVENT_TYPE,
    DEFAULT_CATCH_UP_LIMIT,
    ScheduleSupervisor,
    _matches_day,
    _next_fire_after,
    load_receipts,
)
from bernstein.core.planning.schedule_store import ScheduleStore, parse_cron
from bernstein.core.trigger_sources.schedule import normalize_schedule_fire

# ---------------------------------------------------------------------------
# Cron iteration math
# ---------------------------------------------------------------------------


class TestNextFireAfter:
    def test_every_minute_advances_one_minute(self) -> None:
        parsed = parse_cron("* * * * *")
        anchor = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        next_fire = _next_fire_after(parsed, anchor)
        assert next_fire == anchor + 60

    def test_daily_at_9am(self) -> None:
        parsed = parse_cron("0 9 * * *")
        # Just past 9am UTC on a Mon
        anchor = int(datetime(2030, 1, 7, 9, 0, 1, tzinfo=UTC).timestamp())
        next_fire = _next_fire_after(parsed, anchor)
        expected = int(datetime(2030, 1, 8, 9, 0, 0, tzinfo=UTC).timestamp())
        assert next_fire == expected

    def test_weekday_only(self) -> None:
        parsed = parse_cron("0 9 * * mon-fri")
        # Friday 10am UTC -> next fire = Monday 9am
        anchor = int(datetime(2030, 1, 4, 10, 0, 0, tzinfo=UTC).timestamp())
        next_fire = _next_fire_after(parsed, anchor)
        next_dt = datetime.fromtimestamp(next_fire, tz=UTC)
        assert next_dt.weekday() == 0  # Monday
        assert next_dt.hour == 9

    def test_step_minutes(self) -> None:
        parsed = parse_cron("*/15 * * * *")
        anchor = int(datetime(2030, 1, 1, 12, 7, 30, tzinfo=UTC).timestamp())
        next_fire = _next_fire_after(parsed, anchor)
        next_dt = datetime.fromtimestamp(next_fire, tz=UTC)
        assert next_dt.minute == 15

    def test_strictly_after_anchor(self) -> None:
        """If anchor lands exactly on a fire instant, the next fire must
        be strictly after (otherwise the supervisor would re-fire on
        every tick).
        """
        parsed = parse_cron("0 * * * *")
        anchor = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        next_fire = _next_fire_after(parsed, anchor)
        assert next_fire > anchor


class TestMatchesDay:
    def test_unrestricted_returns_true(self) -> None:
        parsed = parse_cron("* * * * *")
        # Pick an arbitrary day.
        assert _matches_day(parsed, datetime(2030, 6, 15, tzinfo=UTC))

    def test_day_restricted_only(self) -> None:
        parsed = parse_cron("0 0 15 * *")  # day 15 only
        assert _matches_day(parsed, datetime(2030, 6, 15, tzinfo=UTC))
        assert not _matches_day(parsed, datetime(2030, 6, 16, tzinfo=UTC))

    def test_dow_restricted_only(self) -> None:
        parsed = parse_cron("0 0 * * mon")
        # 2030-01-07 is Monday
        assert _matches_day(parsed, datetime(2030, 1, 7, tzinfo=UTC))
        assert not _matches_day(parsed, datetime(2030, 1, 8, tzinfo=UTC))

    def test_union_when_both_restricted(self) -> None:
        # day 1 OR Friday
        parsed = parse_cron("0 0 1 * fri")
        # Friday but not day 1
        # 2030-01-04 is Friday
        assert _matches_day(parsed, datetime(2030, 1, 4, tzinfo=UTC))
        # Day 1 but not Friday (2030-01-01 is Tuesday)
        assert _matches_day(parsed, datetime(2030, 1, 1, tzinfo=UTC))
        # Neither (2030-01-02 is Wednesday)
        assert not _matches_day(parsed, datetime(2030, 1, 2, tzinfo=UTC))


# ---------------------------------------------------------------------------
# normalize_schedule_fire
# ---------------------------------------------------------------------------


class TestNormalizeScheduleFire:
    def test_basic_event(self) -> None:
        event = normalize_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            goal="Send daily digest",
            projection_hash="deadbeef",
        )
        assert event.source == "schedule"
        assert event.timestamp == pytest.approx(1_700_000_000)
        assert event.message == "Send daily digest"
        assert event.metadata["source_type"] == "schedule"
        assert event.metadata["schedule_id"] == "sched_alpha"
        assert event.metadata["projection_hash"] == "deadbeef"

    def test_scenario_only(self) -> None:
        event = normalize_schedule_fire(
            schedule_id="sched_beta",
            fire_time=1_700_000_000,
            scenario_id="security-pentest",
        )
        assert event.metadata["scenario_id"] == "security-pentest"
        assert event.message == "scenario:security-pentest"

    def test_extras_cannot_clobber_canonical_keys(self) -> None:
        event = normalize_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            goal="g",
            extra={"schedule_id": "WRONG", "custom": "ok"},
        )
        # Our own key wins; custom passes through.
        assert event.metadata["schedule_id"] == "sched_alpha"
        assert event.metadata["custom"] == "ok"

    def test_message_truncated_to_500(self) -> None:
        event = normalize_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            goal="x" * 1000,
        )
        assert len(event.message) == 500


# ---------------------------------------------------------------------------
# Stub audit writer + dispatch
# ---------------------------------------------------------------------------


@dataclass
class _StubAuditEvent:
    timestamp: str
    event_type: str
    actor: str
    resource_type: str
    resource_id: str
    details: dict[str, Any]
    prev_hmac: str
    hmac: str


@dataclass
class _StubAuditLog:
    """In-memory audit chain for tests.

    Mimics ``AuditLog.log`` and exposes a ``_prev_hmac`` attribute so the
    supervisor adapter reads the current tail.
    """

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
        # Mimic the HMAC chain by hashing the previous + a marker.
        import hashlib
        import json as _json

        payload = self._prev_hmac + _json.dumps(
            {
                "event_type": event_type,
                "actor": actor,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details,
            },
            sort_keys=True,
        )
        new_hmac = hashlib.sha256(payload.encode()).hexdigest()
        event = _StubAuditEvent(
            timestamp="t",
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            prev_hmac=self._prev_hmac,
            hmac=new_hmac,
        )
        self.entries.append(event)
        self._prev_hmac = new_hmac
        return event


# ---------------------------------------------------------------------------
# Supervisor tick
# ---------------------------------------------------------------------------


class TestSupervisorTickSkipPolicy:
    def test_no_schedules_no_receipts(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit)
        assert sup.tick() == []

    def test_fires_when_window_due(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute")
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit)
        # Anchor at minute boundary + 70s so one fire is due
        now = int(datetime(2030, 1, 1, 12, 1, 10, tzinfo=UTC).timestamp())
        receipts = sup.tick(now=now)
        dispatched = [r for r in receipts if r.dispatched]
        assert len(dispatched) >= 1
        assert dispatched[0].schedule_id == schedule.id
        assert dispatched[0].projection_hash
        assert len(fired) == len(dispatched)

    def test_skip_policy_collapses_missed_windows(self, tmp_path: Path) -> None:
        """Multiple missed windows under skip policy -> one fire +
        counterfactual receipt for the rest.

        Simulates a restart by seeding ``last_fire_at`` 10 minutes in the
        past so the supervisor sees 10 missed minute windows.
        """
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute", misfire_policy="skip")
        # Seed the previous fire 10 minutes ago.
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit)
        now = int(datetime(2030, 1, 1, 12, 10, 30, tzinfo=UTC).timestamp())
        receipts = sup.tick(now=now)
        dispatched = [r for r in receipts if r.dispatched]
        counterfactuals = [r for r in receipts if r.counterfactual]
        # skip policy fires only once
        assert len(dispatched) == 1
        # counterfactual receipt captures the skipped windows
        assert len(counterfactuals) == 1
        assert len(counterfactuals[0].skipped_windows) >= 1


class TestSupervisorTickCatchUpPolicy:
    def test_catch_up_fires_each_missed_window(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute", misfire_policy="catch_up")
        # Seed last_fire_at five minutes before "now" to simulate a downtime.
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit)
        now = int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp())
        receipts = sup.tick(now=now)
        dispatched = [r for r in receipts if r.dispatched]
        assert len(dispatched) >= 4  # at least four windows in 5 minutes
        # Each fire has its own projection_hash.
        hashes = {r.projection_hash for r in dispatched}
        assert len(hashes) == len(dispatched)

    def test_catch_up_cap_enforced(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute", misfire_policy="catch_up")
        # Seed last_fire_at well before now so many windows accrue.
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit, catch_up_limit=3)
        # Far more than the cap of 3 missed windows
        now = int(datetime(2030, 1, 1, 12, 30, 0, tzinfo=UTC).timestamp())
        receipts = sup.tick(now=now)
        dispatched = [r for r in receipts if r.dispatched]
        assert len(dispatched) == 3  # capped
        counterfactuals = [r for r in receipts if r.counterfactual]
        assert len(counterfactuals) == 1
        assert len(counterfactuals[0].skipped_windows) > 0


class TestSupervisorAuditChain:
    def test_each_fire_chains(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute", misfire_policy="catch_up")
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit, catch_up_limit=5)
        now = int(datetime(2030, 1, 1, 12, 5, 30, tzinfo=UTC).timestamp())
        sup.tick(now=now)
        # AC: event_type=schedule.fire, payload carries projection_hash + prev_chain_digest
        assert all(e.event_type == AUDIT_EVENT_TYPE for e in audit.entries)
        first = audit.entries[0]
        assert "projection_hash" in first.details
        assert "prev_chain_digest" in first.details
        assert "schedule_id" in first.details
        assert "fire_time" in first.details
        # Chain links: each entry's prev_hmac matches the previous entry's hmac.
        for prev, curr in zip(audit.entries, audit.entries[1:], strict=False):
            assert curr.prev_hmac == prev.hmac

    def test_counterfactual_not_chained(self, tmp_path: Path) -> None:
        """Counterfactual receipts must NOT add audit chain entries.

        The chain captures fires that actually happened; including
        counterfactuals would defeat the byte-identical-sequence guarantee
        between two operators with the same fire history.
        """
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute", misfire_policy="skip")
        last_fire = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())
        store.update_last_fire(schedule.id, float(last_fire))
        audit = _StubAuditLog()
        fired: list[Any] = []
        sup = ScheduleSupervisor(store, fired.append, audit)
        now = int(datetime(2030, 1, 1, 12, 10, 30, tzinfo=UTC).timestamp())
        sup.tick(now=now)
        # Skip policy: exactly one fire even with 10 missed windows
        assert len(audit.entries) == 1


class TestSupervisorStatus:
    def test_status_reports_supervisor_alive_after_tick(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        store.add(cron="0 9 * * *", goal="Daily")
        audit = _StubAuditLog()
        sup = ScheduleSupervisor(store, lambda _e: None, audit)
        # Cold supervisor is not alive
        assert sup.status().alive is False
        sup.tick(now=int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()))
        # After tick, alive within liveness window
        assert sup.status(liveness_window_s=600).alive is True

    def test_status_reports_next_fire(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="0 9 * * *", goal="Daily")
        audit = _StubAuditLog()
        sup = ScheduleSupervisor(store, lambda _e: None, audit)
        status = sup.status()
        assert status.next_fire_at > 0
        assert status.next_fire_schedule_id == schedule.id

    def test_status_total_count(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        store.add(cron="0 9 * * *", goal="A")
        store.add(cron="0 10 * * *", goal="B")
        audit = _StubAuditLog()
        sup = ScheduleSupervisor(store, lambda _e: None, audit)
        assert sup.status().schedules_total == 2


class TestSupervisorReceiptPersistence:
    def test_receipt_persisted_and_loadable(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        store.add(cron="* * * * *", goal="Every minute")
        audit = _StubAuditLog()
        sup = ScheduleSupervisor(store, lambda _e: None, audit)
        now = int(datetime(2030, 1, 1, 12, 1, 30, tzinfo=UTC).timestamp())
        receipts = sup.tick(now=now)
        assert receipts
        loaded = load_receipts(tmp_path)
        assert any(r.dispatched for r in loaded)


class TestProjectionPersistenceAlignment:
    """The supervisor must drive the projection with the integer fire_time
    that ends up baked into the audit chain - if the supervisor passed a
    float we would silently violate the AC.
    """

    def test_projection_hash_matches_supervisor_chain(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Every minute")
        audit = _StubAuditLog()
        sup = ScheduleSupervisor(store, lambda _e: None, audit)
        now = int(datetime(2030, 1, 1, 12, 1, 30, tzinfo=UTC).timestamp())
        sup.tick(now=now)
        # Recompute the projection the same way the supervisor did.
        fire_time = int(audit.entries[0].details["fire_time"])
        recomputed = project_schedule_fire(
            schedule_id=schedule.id,
            fire_time=fire_time,
            last_state=None,
            goal="Every minute",
            scenario_id="",
        )
        assert audit.entries[0].details["projection_hash"] == recomputed.projection_hash


# ---------------------------------------------------------------------------
# Defaults sanity
# ---------------------------------------------------------------------------


def test_default_catch_up_limit_is_positive() -> None:
    assert DEFAULT_CATCH_UP_LIMIT > 0


class TestResponseProfileProjectionFold:
    """A schedule-declared response profile forks the projected task identity."""

    def test_declared_profile_changes_projection_hash(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly triage")
        sup = ScheduleSupervisor(store, lambda _e: None, _StubAuditLog())
        epoch = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())

        plain = sup._fire(schedule, epoch, counterfactual=True)
        schedule.extra["response_profile"] = "terse"
        profiled = sup._fire(schedule, epoch, counterfactual=True)

        assert plain.projection_hash != profiled.projection_hash

    def test_unknown_profile_is_ignored(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly triage")
        sup = ScheduleSupervisor(store, lambda _e: None, _StubAuditLog())
        epoch = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())

        plain = sup._fire(schedule, epoch, counterfactual=True)
        schedule.extra["response_profile"] = "shouty"
        ignored = sup._fire(schedule, epoch, counterfactual=True)

        assert plain.projection_hash == ignored.projection_hash

    def test_balanced_profile_still_folds_declared_identity(self, tmp_path: Path) -> None:
        # An EXPLICIT balanced profile is still a declared input: it folds
        # (with the empty-addendum hash), unlike an absent profile.
        store = ScheduleStore(tmp_path)
        schedule = store.add(cron="* * * * *", goal="Nightly triage")
        sup = ScheduleSupervisor(store, lambda _e: None, _StubAuditLog())
        epoch = int(datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp())

        plain = sup._fire(schedule, epoch, counterfactual=True)
        schedule.extra["response_profile"] = "balanced"
        profiled = sup._fire(schedule, epoch, counterfactual=True)

        assert plain.projection_hash != profiled.projection_hash
