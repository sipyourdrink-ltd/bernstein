"""Unit tests for freshness-gated artifact reads (#5130).

Proves that:
1. Fresh artifacts are served immediately without producer invocation.
2. Reads of stale/missing artifacts trigger the producer and block until terminal state.
3. Concurrent stale reads collapse into exactly one producer run (stampede prevention).
4. `--no-wait` returns the stale artifact immediately with an explicit `is_stale=True` flag in the body.
5. Progress updates do NOT unblock waiting readers early.
6. Failed producers unblock waiting readers, serving stale fallback when available.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bernstein.core.govern.freshness_gate import (
    FreshnessGate,
    FreshnessResult,
    ProducerState,
    freshness_gated_read,
)


def test_fresh_artefact_served_without_triggering_producer() -> None:
    """A read inside the TTL window never invokes the producer."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    generated_at = now - timedelta(seconds=10)
    artifact = {"inventory": ["node-1", "node-2"]}

    producer_called = 0

    def reader() -> tuple[dict[str, Any], datetime]:
        return artifact, generated_at

    def producer() -> dict[str, Any]:
        nonlocal producer_called
        producer_called += 1
        return {"inventory": ["node-1", "node-2", "node-3"]}

    gate = FreshnessGate[dict[str, Any]]()
    result = gate.read(
        key="cluster_inventory",
        reader=reader,
        producer=producer,
        ttl_seconds=60,
        now=now,
    )

    assert producer_called == 0
    assert not result.is_stale
    assert result.stale_reason is None
    assert result.data == artifact
    assert result.generated_at == generated_at
    assert result.age_seconds == 10.0
    assert result.producer_state == ProducerState.SUCCESS
    assert result.unwrap() == artifact


def test_stale_read_triggers_exactly_one_producer_run_under_concurrent_readers() -> None:
    """A stale artifact under concurrent readers triggers exactly one producer run."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(seconds=300)

    # Thread-safe mock storage
    storage: dict[str, Any] = {"version": 1}
    storage_ts: datetime = old_time
    storage_lock = threading.Lock()

    producer_calls = 0

    def reader() -> tuple[dict[str, Any], datetime]:
        with storage_lock:
            return storage, storage_ts

    def slow_producer() -> dict[str, Any]:
        nonlocal producer_calls, storage, storage_ts
        producer_calls += 1
        time.sleep(0.05)  # Simulate expensive work
        with storage_lock:
            storage = {"version": 2, "refreshed": True}
            storage_ts = now
            return storage

    gate = FreshnessGate[dict[str, Any]]()
    num_threads = 10
    results: list[FreshnessResult[dict[str, Any]]] = [None] * num_threads  # type: ignore[list-item]
    threads: list[threading.Thread] = []

    def worker(index: int) -> None:
        results[index] = gate.read(
            key="audit_report",
            reader=reader,
            producer=slow_producer,
            ttl_seconds=60,
            now=now,
        )

    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=5.0)

    assert producer_calls == 1
    for r in results:
        assert r is not None
        assert not r.is_stale
        assert r.data == {"version": 2, "refreshed": True}
        assert r.producer_state == ProducerState.SUCCESS


def test_no_wait_returns_stale_marked_in_body() -> None:
    """The no_wait option immediately returns the stale artifact with is_stale=True."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(seconds=120)
    stale_artifact = {"findings": [1, 2, 3]}

    producer_invoked = threading.Event()

    def reader() -> tuple[dict[str, Any], datetime]:
        return stale_artifact, old_time

    def background_producer() -> dict[str, Any]:
        producer_invoked.set()
        return {"findings": [1, 2, 3, 4]}

    gate = FreshnessGate[dict[str, Any]]()
    result = gate.read(
        key="findings_doc",
        reader=reader,
        producer=background_producer,
        ttl_seconds=30,
        no_wait=True,
        now=now,
    )

    assert result.is_stale
    assert result.stale_reason is not None
    assert "exceeds TTL" in result.stale_reason
    assert result.data == stale_artifact
    assert result.age_seconds == 120.0
    assert result.generated_at == old_time
    assert result.unwrap() == stale_artifact

    # Verify background producer was dispatched
    assert producer_invoked.wait(timeout=2.0)


def test_read_blocks_until_terminal_state_not_first_progress() -> None:
    """A producer reporting intermediate progress does not unblock waiting readers early."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    progress_updates: list[str] = []
    reader_unblocked_at_progress = False
    finished_event = threading.Event()

    def reader() -> tuple[dict[str, str] | None, datetime | None]:
        if finished_event.is_set():
            return {"status": "complete"}, now
        return None, None

    def multi_step_producer(progress_callback=None) -> dict[str, str]:
        if progress_callback:
            progress_callback("25% scanned")
            time.sleep(0.02)
            progress_callback("50% scanned")
            time.sleep(0.02)
            progress_callback("75% scanned")
            time.sleep(0.02)
        finished_event.set()
        return {"status": "complete"}

    gate = FreshnessGate[dict[str, str]]()

    reader_result: list[FreshnessResult[dict[str, str]]] = []

    def progress_tracker(update: Any) -> None:
        progress_updates.append(str(update))
        # Ensure that during intermediate progress, reader is not yet done
        if reader_result:
            nonlocal reader_unblocked_at_progress
            reader_unblocked_at_progress = True

    def reader_thread() -> None:
        res = gate.read(
            key="multi_step",
            reader=reader,
            producer=multi_step_producer,
            ttl_seconds=10,
            now=now,
            progress_callback=progress_tracker,
        )
        reader_result.append(res)

    t = threading.Thread(target=reader_thread)
    t.start()
    t.join(timeout=5.0)

    assert not reader_unblocked_at_progress
    assert len(reader_result) == 1
    assert reader_result[0].data == {"status": "complete"}
    assert reader_result[0].producer_state == ProducerState.SUCCESS
    assert "25% scanned" in progress_updates
    assert "75% scanned" in progress_updates


def test_failed_producer_unblocks_readers_and_marks_stale() -> None:
    """A failed producer unblocks waiting readers and serves stale fallback when present."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(seconds=200)
    stale_artifact = {"cached_hosts": ["h1", "h2"]}

    def reader() -> tuple[dict[str, Any], datetime]:
        return stale_artifact, old_time

    def failing_producer() -> None:
        time.sleep(0.02)
        raise ConnectionError("Network unreachable")

    gate = FreshnessGate[dict[str, Any]]()
    result = gate.read(
        key="hosts_scan",
        reader=reader,
        producer=failing_producer,
        ttl_seconds=60,
        now=now,
    )

    assert result.is_stale
    assert result.producer_state == ProducerState.FAILED
    assert "Network unreachable" in str(result.stale_reason)
    assert result.data == stale_artifact
    assert result.unwrap() == stale_artifact


def test_failed_producer_with_no_prior_artifact_raises() -> None:
    """A failed producer with no prior artifact raises the exception."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)

    def reader() -> tuple[None, None]:
        return None, None

    def failing_producer() -> None:
        raise RuntimeError("Fatal collector crash")

    gate = FreshnessGate[dict[str, Any]]()
    with pytest.raises(RuntimeError, match="Fatal collector crash"):
        gate.read(
            key="empty_target",
            reader=reader,
            producer=failing_producer,
            ttl_seconds=60,
            now=now,
        )


def test_convenience_freshness_gated_read() -> None:
    """The freshness_gated_read top-level function works seamlessly."""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
    val = {"hello": "world"}

    result = freshness_gated_read(
        key="simple_key",
        reader=lambda: (val, now),
        producer=lambda: val,
        ttl_seconds=300,
        now=now,
    )

    assert not result.is_stale
    assert result.data == val
    assert result.unwrap() == val
