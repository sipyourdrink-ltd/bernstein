"""Tests for task progress estimation (TASK-014)."""

from __future__ import annotations

import time

import pytest

from bernstein.core.models import Complexity, Scope, Task, TaskStatus
from bernstein.core.progress_estimation import ProgressEstimator


def _t(
    id: str = "t1",
    role: str = "backend",
    scope: str = "medium",
    complexity: str = "medium",
    status: str = "in_progress",
    created_at: float | None = None,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role=role,
        scope=Scope(scope),
        complexity=Complexity(complexity),
        status=TaskStatus(status),
        created_at=created_at or time.time(),
        estimated_minutes=estimated_minutes,
    )


class TestProgressEstimator:
    def test_fallback_estimate(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        task = _t(created_at=now - 600, scope="medium", complexity="medium")
        result = est.estimate(task, now=now)
        assert result.task_id == "t1"
        assert result.elapsed_seconds == pytest.approx(600.0, abs=1.0)
        # Fallback for medium/medium is 1200s
        assert result.estimated_total_seconds == 1200.0
        assert result.confidence == 0.3  # Fallback confidence

    def test_historical_estimate(self) -> None:
        est = ProgressEstimator(min_data_points=2)
        # Record some completions
        for i in range(5):
            task = _t(id=f"done-{i}", role="backend", scope="medium")
            est.record_completion(task, duration_seconds=900.0)

        now = time.time()
        task = _t(created_at=now - 450, role="backend", scope="medium")
        result = est.estimate(task, now=now)
        # Should use historical median (900s)
        assert result.estimated_total_seconds == 900.0
        assert result.data_points == 5
        assert result.confidence > 0.3

    def test_overdue_task(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        task = _t(created_at=now - 5000, scope="medium", complexity="medium")
        result = est.estimate(task, now=now)
        assert result.is_overdue
        assert result.estimated_remaining_seconds == 0.0
        assert result.progress_pct == 95.0  # Capped at 95%

    def test_new_task_zero_progress(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        task = _t(created_at=now)
        result = est.estimate(task, now=now)
        assert result.elapsed_seconds == pytest.approx(0.0, abs=1.0)
        assert result.progress_pct == pytest.approx(0.0, abs=1.0)

    def test_halfway_progress(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        # Fallback for medium/medium = 1200s, task started 600s ago
        task = _t(created_at=now - 600, scope="medium", complexity="medium")
        result = est.estimate(task, now=now)
        assert result.progress_pct == pytest.approx(50.0, abs=1.0)

    def test_batch_estimate(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        tasks = [
            _t(id="t1", created_at=now - 300),
            _t(id="t2", created_at=now - 600),
        ]
        results = est.estimate_batch(tasks, now=now)
        assert len(results) == 2
        assert results[0].task_id == "t1"
        assert results[1].task_id == "t2"
        assert results[0].progress_pct < results[1].progress_pct

    def test_overall_progress_all_done(self) -> None:
        est = ProgressEstimator()
        tasks = [
            _t(id="t1", status="done"),
            _t(id="t2", status="done"),
        ]
        assert est.overall_progress(tasks) == 100.0

    def test_overall_progress_none_started(self) -> None:
        est = ProgressEstimator()
        tasks = [
            _t(id="t1", status="open"),
            _t(id="t2", status="open"),
        ]
        assert est.overall_progress(tasks) == 0.0

    def test_overall_progress_mixed(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        tasks = [
            _t(id="t1", status="done"),
            _t(id="t2", status="open"),
        ]
        progress = est.overall_progress(tasks, now=now)
        assert progress == 50.0  # 100 + 0 / 2

    def test_overall_progress_empty(self) -> None:
        est = ProgressEstimator()
        assert est.overall_progress([]) == 0.0

    def test_record_completion_increases_count(self) -> None:
        est = ProgressEstimator()
        task = _t()
        est.record_completion(task, duration_seconds=100.0)
        assert est.history_count == 1
        est.record_completion(task, duration_seconds=200.0)
        assert est.history_count == 2

    def test_different_scope_different_estimate(self) -> None:
        est = ProgressEstimator()
        now = time.time()
        small = _t(id="s", scope="small", complexity="low", created_at=now - 60)
        large = _t(id="l", scope="large", complexity="high", created_at=now - 60)
        est_small = est.estimate(small, now=now)
        est_large = est.estimate(large, now=now)
        assert est_small.estimated_total_seconds < est_large.estimated_total_seconds

    def test_confidence_increases_with_data(self) -> None:
        est = ProgressEstimator(min_data_points=2)
        now = time.time()
        task = _t(role="backend", scope="medium", created_at=now - 100)

        # First check: no historical data
        r1 = est.estimate(task, now=now)
        assert r1.confidence == 0.3

        # Add historical data
        for i in range(10):
            done = _t(id=f"done-{i}", role="backend", scope="medium")
            est.record_completion(done, duration_seconds=600.0)

        r2 = est.estimate(task, now=now)
        assert r2.confidence > r1.confidence
