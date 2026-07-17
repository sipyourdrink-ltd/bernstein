"""Per-project trigger state isolation under concurrency (#2548).

Covers the acceptance criterion: counter and expectation state lives per project
under ``.sdd/runtime/triggers/``, and two concurrent workers evaluating rules do
not interleave state (tested with concurrent workers).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bernstein.core.events.state import TriggerStateStore


def test_state_lives_under_runtime_triggers(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.increment("k")
    assert store.counters_path == root / "counters.json"
    assert store.counters_path.exists()


def test_concurrent_increments_do_not_lose_updates(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    # Two independent store handles standing in for two workers sharing a root.
    worker_a = TriggerStateStore(root)
    worker_b = TriggerStateStore(root)
    per_worker = 200

    def hammer(store: TriggerStateStore) -> None:
        for _ in range(per_worker):
            store.increment("gate.result")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hammer, worker_a), pool.submit(hammer, worker_b)]
        for future in futures:
            future.result()

    # No lost updates: exactly 2 * per_worker increments landed.
    assert TriggerStateStore(root).get_counter("gate.result") == 2 * per_worker


def test_expectations_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.open_expectation("run_1", {"expect": "run.completed", "after_hmac": "abc"})
    assert store.open_expectations() == {"run_1": {"expect": "run.completed", "after_hmac": "abc"}}
    closed = store.close_expectation("run_1")
    assert closed == {"expect": "run.completed", "after_hmac": "abc"}
    assert store.open_expectations() == {}
