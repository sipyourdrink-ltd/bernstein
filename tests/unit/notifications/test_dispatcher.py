"""Tests for retry/backoff/dedup/dead-letter behaviour in the dispatcher."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.notifications.bridge import (
    DeadLetter,
    DedupCache,
    NotificationDispatcher,
    RetryPolicy,
)
from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationOutcome,
    NotificationPermanentError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def _make_event(event_id: str = "e-1") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        kind=NotificationEventKind.POST_TASK,
        title="t",
        timestamp=1234.0,
    )


class _Sink:
    """Minimal recording sink with scriptable failures."""

    kind = "stub"

    def __init__(self, sink_id: str, *, raises: Iterable[Exception] | None = None) -> None:
        self.sink_id = sink_id
        self.deliveries: list[NotificationEvent] = []
        self._raises = list(raises or [])
        self.closed = False

    async def deliver(self, event: NotificationEvent) -> None:
        self.deliveries.append(event)
        if self._raises:
            raise self._raises.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def sleeper_calls() -> list[float]:
    """Return a list capturing every sleep request."""
    return []


@pytest.fixture
def fake_sleeper(sleeper_calls: list[float]) -> Any:
    async def _sleep(seconds: float) -> None:
        sleeper_calls.append(seconds)

    return _sleep


def _make_dispatcher(
    tmp_path: Path,
    *,
    retry: RetryPolicy | None = None,
    sleeper: Any | None = None,
) -> tuple[NotificationDispatcher, list[tuple[str, str, str, dict[str, object]]]]:
    audit_calls: list[tuple[str, str, str, dict[str, object]]] = []

    def _audit_hook(actor: str, rt: str, rid: str, details: dict[str, object]) -> None:
        audit_calls.append((actor, rt, rid, dict(details)))

    dispatcher = NotificationDispatcher(
        tmp_path,
        retry=retry,
        audit_hook=_audit_hook,
        sleeper=sleeper,
    )
    return dispatcher, audit_calls


@pytest.mark.asyncio
async def test_successful_delivery_writes_audit(tmp_path: Path) -> None:
    dispatcher, audit = _make_dispatcher(tmp_path)
    sink = _Sink("alpha")
    outcomes = await dispatcher.dispatch(_make_event(), [sink])
    assert outcomes == {"alpha": NotificationOutcome.DELIVERED}
    assert len(sink.deliveries) == 1
    assert audit[-1][3]["outcome"] == "delivered"
    assert audit[-1][3]["sink_id"] == "alpha"


@pytest.mark.asyncio
async def test_transient_failure_retries_until_success(
    tmp_path: Path,
    fake_sleeper: Any,
    sleeper_calls: list[float],
) -> None:
    dispatcher, audit = _make_dispatcher(
        tmp_path,
        retry=RetryPolicy(max_attempts=3, initial_delay_ms=10),
        sleeper=fake_sleeper,
    )
    sink = _Sink(
        "beta",
        raises=[
            NotificationDeliveryError("blip-1"),
            NotificationDeliveryError("blip-2"),
        ],
    )
    outcomes = await dispatcher.dispatch(_make_event("e-2"), [sink])
    assert outcomes == {"beta": NotificationOutcome.DELIVERED}
    assert len(sink.deliveries) == 3
    # Two retries means two sleeps.
    assert len(sleeper_calls) == 2
    assert sleeper_calls[1] > sleeper_calls[0]  # exponential growth
    # Two retry-audit lines plus one final delivered.
    outcomes_seen = [a[3]["outcome"] for a in audit]
    assert outcomes_seen.count("failed_retrying") == 2
    assert outcomes_seen[-1] == "delivered"


@pytest.mark.asyncio
async def test_permanent_error_short_circuits_to_dead_letter(tmp_path: Path) -> None:
    dispatcher, audit = _make_dispatcher(tmp_path)
    sink = _Sink("gamma", raises=[NotificationPermanentError("nope")])
    outcomes = await dispatcher.dispatch(_make_event("e-3"), [sink])
    assert outcomes == {"gamma": NotificationOutcome.FAILED_PERMANENT}
    # Only one attempt — permanent skips retry.
    assert len(sink.deliveries) == 1
    dl_path = tmp_path / "notifications" / "dead_letter.jsonl"
    assert dl_path.exists()
    contents = [json.loads(ln) for ln in dl_path.read_text().splitlines() if ln.strip()]
    assert any("permanent" in rec["reason"] for rec in contents)
    assert audit[-1][3]["outcome"] == "failed_permanent"


@pytest.mark.asyncio
async def test_retries_exhausted_writes_dead_letter(tmp_path: Path, fake_sleeper: Any) -> None:
    dispatcher, _ = _make_dispatcher(
        tmp_path,
        retry=RetryPolicy(max_attempts=2, initial_delay_ms=1),
        sleeper=fake_sleeper,
    )
    sink = _Sink(
        "delta",
        raises=[
            NotificationDeliveryError("a"),
            NotificationDeliveryError("b"),
        ],
    )
    outcomes = await dispatcher.dispatch(_make_event("e-4"), [sink])
    assert outcomes == {"delta": NotificationOutcome.FAILED_PERMANENT}
    dl_path = tmp_path / "notifications" / "dead_letter.jsonl"
    contents = [json.loads(ln) for ln in dl_path.read_text().splitlines() if ln.strip()]
    assert any("retries_exhausted" in rec["reason"] for rec in contents)


@pytest.mark.asyncio
async def test_dedup_skips_second_delivery(tmp_path: Path) -> None:
    dispatcher, _ = _make_dispatcher(tmp_path)
    sink = _Sink("epsilon")
    event = _make_event("dup-1")
    first = await dispatcher.dispatch(event, [sink])
    second = await dispatcher.dispatch(event, [sink])
    assert first == {"epsilon": NotificationOutcome.DELIVERED}
    assert second == {"epsilon": NotificationOutcome.DEDUPLICATED}
    assert len(sink.deliveries) == 1


@pytest.mark.asyncio
async def test_unexpected_exception_is_treated_as_transient(tmp_path: Path, fake_sleeper: Any) -> None:
    dispatcher, _ = _make_dispatcher(
        tmp_path,
        retry=RetryPolicy(max_attempts=2, initial_delay_ms=1),
        sleeper=fake_sleeper,
    )

    class _Boom:
        sink_id = "zeta"
        kind = "stub"

        def __init__(self) -> None:
            self.calls = 0

        async def deliver(self, event: NotificationEvent) -> None:
            self.calls += 1
            raise RuntimeError("ka-boom")

        async def close(self) -> None:  # pragma: no cover
            return None

    sink = _Boom()
    outcomes = await dispatcher.dispatch(_make_event("e-5"), [sink])
    assert outcomes == {"zeta": NotificationOutcome.FAILED_PERMANENT}
    assert sink.calls == 2  # max_attempts


def test_dead_letter_rotation(tmp_path: Path) -> None:
    dl = DeadLetter(tmp_path / "dl.jsonl", max_bytes=64)
    event = _make_event("e-rot")
    for i in range(20):
        dl.append(f"sink-{i}", event, "boom")
    rotated = list(tmp_path.glob("dl.jsonl.*"))
    assert rotated, "expected at least one rotated dead-letter file"


def test_dedup_window_expires(tmp_path: Path) -> None:
    cache = DedupCache(tmp_path / "dedup.jsonl", lru_size=8, window_seconds=10)
    cache.remember("evt-1", now=1000.0)
    assert cache.seen("evt-1", now=1005.0)
    # Outside the window — must miss.
    assert not cache.seen("evt-1", now=1100.0)


def test_dedup_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "dedup.jsonl"
    first = DedupCache(path, window_seconds=600)
    first.remember("evt-shared", now=time.time())
    second = DedupCache(path, window_seconds=600)
    assert second.seen("evt-shared", now=time.time())


def test_retry_policy_delay_is_capped() -> None:
    policy = RetryPolicy(initial_delay_ms=1000, backoff_factor=10.0, max_delay_ms=5000)
    # 1000 * 10^2 = 100000ms — must be capped.
    assert policy.delay_seconds(3) == 5.0
