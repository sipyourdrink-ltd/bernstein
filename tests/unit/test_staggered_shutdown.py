"""Tests for staggered agent shutdown (AGENT-014)."""

from __future__ import annotations

import asyncio

from bernstein.core.staggered_shutdown import (
    ShutdownReport,
    ShutdownStep,
    ShutdownTarget,
    StaggeredShutdown,
    StaggeredShutdownConfig,
)


class TestShutdownTarget:
    def test_default_priority(self) -> None:
        target = ShutdownTarget(session_id="s-1", pid=100)
        assert target.priority == 2


class TestStaggeredShutdown:
    def test_order_targets_reverse_priority(self) -> None:
        config = StaggeredShutdownConfig(reverse_priority=True)
        shutdown = StaggeredShutdown(config=config)
        targets = [
            ShutdownTarget(session_id="high", pid=1, priority=1),
            ShutdownTarget(session_id="low", pid=2, priority=3),
            ShutdownTarget(session_id="mid", pid=3, priority=2),
        ]
        ordered = shutdown.order_targets(targets)
        # Reverse priority: highest number (lowest priority) first
        assert ordered[0].session_id == "low"
        assert ordered[1].session_id == "mid"
        assert ordered[2].session_id == "high"

    def test_order_targets_normal_priority(self) -> None:
        config = StaggeredShutdownConfig(reverse_priority=False)
        shutdown = StaggeredShutdown(config=config)
        targets = [
            ShutdownTarget(session_id="high", pid=1, priority=1),
            ShutdownTarget(session_id="low", pid=2, priority=3),
        ]
        ordered = shutdown.order_targets(targets)
        assert ordered[0].session_id == "high"
        assert ordered[1].session_id == "low"

    def test_execute_kills_all(self) -> None:
        config = StaggeredShutdownConfig(interval_seconds=0)
        shutdown = StaggeredShutdown(config=config)
        targets = [
            ShutdownTarget(session_id="s-1", pid=100, role="backend", priority=1),
            ShutdownTarget(session_id="s-2", pid=200, role="qa", priority=2),
        ]
        killed_pids: list[int] = []

        def kill_fn(pid: int) -> bool:
            killed_pids.append(pid)
            return True

        report = asyncio.run(shutdown.execute(targets, kill_fn=kill_fn))
        assert report.agents_killed == 2
        assert report.agents_failed == 0
        assert len(killed_pids) == 2

    def test_execute_with_failure(self) -> None:
        config = StaggeredShutdownConfig(interval_seconds=0)
        shutdown = StaggeredShutdown(config=config)
        targets = [
            ShutdownTarget(session_id="s-1", pid=100, priority=2),
        ]

        def fail_fn(pid: int) -> bool:
            raise OSError("kill failed")

        report = asyncio.run(shutdown.execute(targets, kill_fn=fail_fn))
        assert report.agents_killed == 0
        assert report.agents_failed == 1
        assert report.steps[0].error == "kill failed"

    def test_execute_async_kill_fn(self) -> None:
        config = StaggeredShutdownConfig(interval_seconds=0)
        shutdown = StaggeredShutdown(config=config)
        targets = [
            ShutdownTarget(session_id="s-1", pid=100, priority=2),
        ]

        async def async_kill(pid: int) -> bool:
            return True

        report = asyncio.run(shutdown.execute(targets, kill_fn=async_kill))
        assert report.agents_killed == 1

    def test_execute_empty_targets(self) -> None:
        shutdown = StaggeredShutdown()
        report = asyncio.run(shutdown.execute([], kill_fn=lambda pid: True))
        assert report.agents_killed == 0
        assert report.total_seconds >= 0

    def test_config_property(self) -> None:
        config = StaggeredShutdownConfig(interval_seconds=10.0)
        shutdown = StaggeredShutdown(config=config)
        assert shutdown.config.interval_seconds == 10.0


class TestShutdownReport:
    def test_empty_report(self) -> None:
        report = ShutdownReport()
        assert report.agents_killed == 0
        assert report.agents_failed == 0

    def test_report_counts(self) -> None:
        report = ShutdownReport(
            steps=[
                ShutdownStep(session_id="s-1", pid=1, role="be", priority=1, success=True),
                ShutdownStep(session_id="s-2", pid=2, role="qa", priority=2, success=False, error="boom"),
            ]
        )
        assert report.agents_killed == 1
        assert report.agents_failed == 1
