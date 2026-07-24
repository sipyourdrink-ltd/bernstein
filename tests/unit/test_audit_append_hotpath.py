"""Hot-path invariants for :meth:`AuditLog.log`.

The append path runs once per scheduling decision. Two properties matter when
the HMAC chain is on by default (issue #2690): the per-append work is minimal,
and the timestamp written into an event agrees with the daily file the event
lands in. Both follow from deriving ``ts`` and ``day`` from a single
``datetime.now`` reading rather than two.
"""

from __future__ import annotations

from pathlib import Path

import bernstein.core.audit as audit_mod
from bernstein.core.audit import AuditLog


class _FakeInstant:
    """A frozen ``datetime`` reading with a fixed timestamp/day pair."""

    def __init__(self, ts: str, day: str) -> None:
        self._ts = ts
        self._day = day

    def strftime(self, fmt: str) -> str:
        if fmt == "%Y-%m-%dT%H:%M:%S.%fZ":
            return self._ts
        if fmt == "%Y-%m-%d":
            return self._day
        raise AssertionError(f"unexpected strftime format {fmt!r}")


class _FakeDatetime:
    """Hands out queued instants so we can straddle a UTC midnight boundary."""

    def __init__(self, instants: list[_FakeInstant]) -> None:
        self.queue = list(instants)

    def now(self, tz: object = None) -> _FakeInstant:  # tz kept for signature parity
        return self.queue.pop(0)


def test_log_derives_ts_and_day_from_single_now(tmp_path: Path, monkeypatch) -> None:
    """One append consumes exactly one clock reading, so ts and day agree.

    The two instants straddle a UTC midnight: the first is 23:59:59 on day N,
    the second is 00:00:00 on day N+1. Two ``datetime.now`` calls would stamp
    the event with day N's time but file it under day N+1, leaving an event
    whose timestamp disagrees with its containing segment. A single reading
    keeps them consistent.
    """
    before = _FakeInstant("2025-01-01T23:59:59.999999Z", "2025-01-01")
    after = _FakeInstant("2025-01-02T00:00:00.000000Z", "2025-01-02")
    fake = _FakeDatetime([before, after])
    monkeypatch.setattr(audit_mod, "datetime", fake)

    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir, key=b"test-key")
    event = log.log("schedule.decision", "orchestrator", "task", "t1", {"n": 1})

    # Only the first reading is consumed: the second instant is still queued.
    assert len(fake.queue) == 1
    # The event's timestamp date-prefix matches the daily file it landed in.
    live = list(audit_dir.glob("*.jsonl"))
    assert len(live) == 1
    assert live[0].stem == "2025-01-01"
    assert event.timestamp.startswith("2025-01-01")
