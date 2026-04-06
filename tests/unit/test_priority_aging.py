"""Tests for task priority aging (TASK-007)."""

from __future__ import annotations

import time

from bernstein.core.models import Complexity, Scope, Task, TaskStatus
from bernstein.core.priority_aging import AgingConfig, apply_aging, compute_aged_priority


def _t(
    id: str,
    priority: int = 3,
    status: str = "open",
    created_at: float | None = None,
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role="backend",
        priority=priority,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus(status),
        created_at=created_at or time.time(),
    )


class TestComputeAgedPriority:
    def test_no_aging_below_threshold(self) -> None:
        config = AgingConfig(threshold_seconds=300)
        new_pri, boosts = compute_aged_priority(3, 200, config)
        assert new_pri == 3
        assert boosts == 0

    def test_one_boost_at_threshold(self) -> None:
        config = AgingConfig(threshold_seconds=300, boost_per_interval=1)
        new_pri, boosts = compute_aged_priority(3, 300, config)
        assert new_pri == 2
        assert boosts == 1

    def test_multiple_boosts(self) -> None:
        config = AgingConfig(threshold_seconds=300, boost_per_interval=1)
        new_pri, boosts = compute_aged_priority(3, 900, config)
        assert new_pri == 1
        assert boosts == 3

    def test_clamped_at_min_priority(self) -> None:
        config = AgingConfig(threshold_seconds=100, boost_per_interval=1, min_priority=1)
        new_pri, _boosts = compute_aged_priority(3, 10000, config)
        assert new_pri == 1

    def test_max_boosts_limit(self) -> None:
        config = AgingConfig(threshold_seconds=100, boost_per_interval=1, max_boosts=2)
        new_pri, boosts = compute_aged_priority(5, 10000, config)
        assert boosts == 2
        assert new_pri == 3

    def test_custom_boost_amount(self) -> None:
        config = AgingConfig(threshold_seconds=300, boost_per_interval=2)
        new_pri, boosts = compute_aged_priority(5, 600, config)
        assert boosts == 2
        assert new_pri == 1  # 5 - (2*2) = 1

    def test_zero_threshold_no_boost(self) -> None:
        config = AgingConfig(threshold_seconds=0)
        new_pri, boosts = compute_aged_priority(3, 10000, config)
        assert new_pri == 3
        assert boosts == 0


class TestApplyAging:
    def test_boosts_old_open_task(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, created_at=now - 600)
        config = AgingConfig(threshold_seconds=300, boost_per_interval=1)
        results = apply_aging([task], config, now=now)
        assert len(results) == 1
        assert results[0].task_id == "t1"
        assert results[0].original_priority == 3
        assert results[0].new_priority == 1
        assert task.priority == 1  # mutated in-place

    def test_skips_done_tasks(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, status="done", created_at=now - 600)
        config = AgingConfig(threshold_seconds=300)
        results = apply_aging([task], config, now=now)
        assert len(results) == 0
        assert task.priority == 3

    def test_skips_in_progress_tasks(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, status="in_progress", created_at=now - 600)
        config = AgingConfig(threshold_seconds=300)
        results = apply_aging([task], config, now=now)
        assert len(results) == 0

    def test_ages_blocked_tasks(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, status="blocked", created_at=now - 600)
        config = AgingConfig(threshold_seconds=300, boost_per_interval=1)
        results = apply_aging([task], config, now=now)
        assert len(results) == 1
        assert task.priority == 1

    def test_no_boost_when_too_young(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, created_at=now - 100)
        config = AgingConfig(threshold_seconds=300)
        results = apply_aging([task], config, now=now)
        assert len(results) == 0

    def test_default_config(self) -> None:
        now = time.time()
        task = _t("t1", priority=3, created_at=now - 600)
        results = apply_aging([task], now=now)
        assert len(results) == 1

    def test_already_at_min_priority(self) -> None:
        now = time.time()
        task = _t("t1", priority=1, created_at=now - 600)
        config = AgingConfig(threshold_seconds=300, boost_per_interval=1, min_priority=1)
        results = apply_aging([task], config, now=now)
        assert len(results) == 0  # No change needed
