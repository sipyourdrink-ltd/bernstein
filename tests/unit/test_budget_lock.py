"""Tests for COST-001: atomic budget enforcement with thread-safe locking."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from bernstein.core.cost_tracker import CostTracker


class TestCostTrackerLock:
    """Verify that CostTracker.record() is thread-safe."""

    def test_concurrent_records_no_data_loss(self) -> None:
        """Multiple threads recording simultaneously should not lose data."""
        tracker = CostTracker(run_id="test-lock", budget_usd=100.0)
        n_threads = 10
        n_records_per_thread = 50

        def record_batch(thread_id: int) -> None:
            for i in range(n_records_per_thread):
                tracker.record(
                    agent_id=f"agent-{thread_id}",
                    task_id=f"task-{thread_id}-{i}",
                    model="sonnet",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.01,
                )

        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = [executor.submit(record_batch, tid) for tid in range(n_threads)]
            for f in futures:
                f.result()

        expected_count = n_threads * n_records_per_thread
        assert len(tracker.usages) == expected_count
        # Verify total cost (0.01 * 500 = 5.0)
        assert abs(tracker.spent_usd - expected_count * 0.01) < 1e-6

    def test_concurrent_records_budget_not_exceeded(self) -> None:
        """Budget enforcement should hold under concurrent recording."""
        tracker = CostTracker(run_id="test-budget", budget_usd=1.0)

        # Each record costs 0.01, budget is 1.0 => 100 records max before should_stop
        barrier = threading.Barrier(4)

        def record_batch(thread_id: int) -> list[bool]:
            barrier.wait()
            statuses: list[bool] = []
            for i in range(50):
                status = tracker.record(
                    agent_id=f"agent-{thread_id}",
                    task_id=f"task-{thread_id}-{i}",
                    model="sonnet",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.01,
                )
                statuses.append(status.should_stop)
            return statuses

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(record_batch, tid) for tid in range(4)]
            all_statuses = [f.result() for f in futures]

        # Total: 4 * 50 * 0.01 = 2.0 USD > budget of 1.0
        assert tracker.spent_usd > 1.0
        # should_stop should be True for records after budget exceeded
        any_stopped = any(s for batch in all_statuses for s in batch)
        assert any_stopped


class TestCanSpawn:
    """Verify CostTracker.can_spawn() atomic budget check."""

    def test_can_spawn_under_budget(self) -> None:
        tracker = CostTracker(run_id="test-spawn", budget_usd=10.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1.0,
        )
        assert tracker.can_spawn() is True

    def test_cannot_spawn_over_budget(self) -> None:
        tracker = CostTracker(run_id="test-spawn", budget_usd=1.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1.5,
        )
        assert tracker.can_spawn() is False

    def test_can_spawn_unlimited_budget(self) -> None:
        tracker = CostTracker(run_id="test-spawn", budget_usd=0.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1000.0,
        )
        assert tracker.can_spawn() is True

    def test_can_spawn_at_exactly_budget(self) -> None:
        """At exactly 100% the hard_stop_threshold is met -> cannot spawn."""
        tracker = CostTracker(run_id="test-spawn", budget_usd=1.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1.0,
        )
        assert tracker.can_spawn() is False

    def test_can_spawn_concurrent(self) -> None:
        """can_spawn should be safe to call from multiple threads."""
        tracker = CostTracker(run_id="test-spawn", budget_usd=10.0)
        tracker.record(
            agent_id="a1",
            task_id="t1",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=5.0,
        )

        results: list[bool] = []
        lock = threading.Lock()

        def check() -> None:
            result = tracker.can_spawn()
            with lock:
                results.append(result)

        threads = [threading.Thread(target=check) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is True for r in results)
