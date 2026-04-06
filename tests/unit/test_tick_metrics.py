"""Tests for per-tick metrics counters (ORCH-016)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.tick_metrics import CumulativeMetrics, TickMetrics, TickSnapshot


class TestTickSnapshot:
    def test_defaults(self) -> None:
        snap = TickSnapshot(tick_number=1)
        assert snap.tasks_spawned == 0
        assert snap.tasks_completed == 0
        assert snap.errors == 0
        assert snap.tick_duration_ms == 0.0
        assert snap.timestamp > 0

    def test_to_dict(self) -> None:
        snap = TickSnapshot(tick_number=5, tasks_spawned=2, tick_duration_ms=150.5)
        d = snap.to_dict()
        assert d["tick_number"] == 5
        assert d["tasks_spawned"] == 2
        assert d["tick_duration_ms"] == 150.5


class TestCumulativeMetrics:
    def test_defaults(self) -> None:
        cum = CumulativeMetrics()
        assert cum.total_ticks == 0
        assert cum.total_spawned == 0

    def test_to_dict(self) -> None:
        cum = CumulativeMetrics(total_ticks=10, total_completed=5)
        d = cum.to_dict()
        assert d["total_ticks"] == 10
        assert d["total_completed"] == 5


class TestTickMetrics:
    def test_record_tick(self) -> None:
        metrics = TickMetrics()
        snap = metrics.record_tick(
            tick_number=1,
            spawned=2,
            completed=1,
            duration_ms=100.0,
        )
        assert snap.tick_number == 1
        assert snap.tasks_spawned == 2

    def test_latest(self) -> None:
        metrics = TickMetrics()
        assert metrics.latest is None
        metrics.record_tick(tick_number=1, spawned=1)
        metrics.record_tick(tick_number=2, spawned=3)
        assert metrics.latest is not None
        assert metrics.latest.tick_number == 2

    def test_cumulative(self) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, spawned=2, completed=1, errors=1)
        metrics.record_tick(tick_number=2, spawned=3, completed=2, errors=0)
        cum = metrics.cumulative
        assert cum.total_ticks == 2
        assert cum.total_spawned == 5
        assert cum.total_completed == 3
        assert cum.total_errors == 1

    def test_history(self) -> None:
        metrics = TickMetrics()
        for i in range(5):
            metrics.record_tick(tick_number=i + 1, spawned=i)
        assert len(metrics.history) == 5

    def test_history_capped(self) -> None:
        metrics = TickMetrics(max_history=3)
        for i in range(10):
            metrics.record_tick(tick_number=i + 1)
        assert len(metrics.history) == 3
        assert metrics.history[0].tick_number == 8

    def test_avg_tick_ms(self) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, duration_ms=100.0)
        metrics.record_tick(tick_number=2, duration_ms=200.0)
        assert metrics.avg_tick_ms() == 150.0

    def test_avg_tick_ms_window(self) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, duration_ms=100.0)
        metrics.record_tick(tick_number=2, duration_ms=200.0)
        metrics.record_tick(tick_number=3, duration_ms=300.0)
        # Window of 2: average of 200 and 300
        assert metrics.avg_tick_ms(window=2) == 250.0

    def test_avg_tick_ms_empty(self) -> None:
        metrics = TickMetrics()
        assert metrics.avg_tick_ms() == 0.0

    def test_error_rate(self) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, errors=2)
        metrics.record_tick(tick_number=2, errors=0)
        assert metrics.error_rate() == 1.0  # 2 errors / 2 ticks

    def test_error_rate_empty(self) -> None:
        metrics = TickMetrics()
        assert metrics.error_rate() == 0.0

    def test_save(self, tmp_path: Path) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, spawned=1, duration_ms=50.0)
        path = tmp_path / "tick_metrics.json"
        metrics.save(path)
        assert path.exists()
        import json

        data = json.loads(path.read_text())
        assert "cumulative" in data
        assert "recent_ticks" in data

    def test_failed_and_retried(self) -> None:
        metrics = TickMetrics()
        metrics.record_tick(tick_number=1, failed=3, retried=1)
        cum = metrics.cumulative
        assert cum.total_failed == 3
        assert cum.total_retried == 1
