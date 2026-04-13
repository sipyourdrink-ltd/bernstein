"""Tests for bernstein.core.retrospective."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bernstein.core.metrics import MetricsCollector
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.retrospective import _build_recommendations, _fmt_seconds, generate_retrospective

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    title: str = "Do something",
    role: str = "backend",
    complexity: str = "medium",
    status: str = "done",
) -> Task:
    return Task(
        id=id,
        title=title,
        description="desc",
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity(complexity),
        status=TaskStatus(status),
        task_type=TaskType.STANDARD,
    )


def _collector_with_tasks(
    tmp_path: Path,
    *,
    tasks: list[tuple[str, str, str, bool, float]],  # (task_id, role, model, success, duration_s)
) -> MetricsCollector:
    """Build a MetricsCollector with pre-populated TaskMetrics."""
    collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
    time.time()
    for task_id, role, model, success, dur in tasks:
        m = collector.start_task(task_id, role, model, "claude")
        m.end_time = m.start_time + dur
        m.success = success
        m.cost_usd = dur * 0.001  # fake cost proportional to duration
    return collector


# ---------------------------------------------------------------------------
# _fmt_seconds
# ---------------------------------------------------------------------------


class TestFmtSeconds:
    def test_sub_minute(self) -> None:
        assert _fmt_seconds(45.3) == "45.3s"

    def test_minutes(self) -> None:
        assert _fmt_seconds(125.0) == "2m 5s"

    def test_hours(self) -> None:
        assert _fmt_seconds(3723.0) == "1h 2m 3s"


# ---------------------------------------------------------------------------
# _build_recommendations
# ---------------------------------------------------------------------------


class TestBuildRecommendations:
    def _call(self, **kwargs: object) -> list[str]:
        defaults = {
            "n_done": 10,
            "n_failed": 0,
            "role_failed": {},
            "role_done": {"backend": 10},
            "cx_failed": {},
            "total_cost": 0.5,
            "wall_clock_s": 300.0,
        }
        defaults.update(kwargs)
        return _build_recommendations(**defaults)  # type: ignore[arg-type]

    def test_no_issues_returns_empty(self) -> None:
        assert self._call() == []

    def test_high_overall_failure_rate(self) -> None:
        recs = self._call(n_done=3, n_failed=7)
        assert any("failure rate" in r for r in recs)

    def test_high_role_failure_rate(self) -> None:
        recs = self._call(role_failed={"qa": 5}, role_done={"qa": 3})
        assert any("qa" in r for r in recs)

    def test_no_recommendation_for_single_task_role(self) -> None:
        # Only 1 task for the role → don't flag it
        recs = self._call(role_failed={"qa": 1}, role_done={"qa": 0})
        assert not any("qa" in r for r in recs)

    def test_high_complexity_failures(self) -> None:
        recs = self._call(cx_failed={"high": 5})
        assert any("high" in r for r in recs)

    def test_high_cost_warning(self) -> None:
        recs = self._call(total_cost=10.0)
        assert any("Cost" in r for r in recs)

    def test_long_run_warning(self) -> None:
        recs = self._call(wall_clock_s=8000.0)
        assert any("2 hours" in r for r in recs)

    def test_zero_tasks_returns_empty(self) -> None:
        recs = self._call(n_done=0, n_failed=0)
        assert recs == []


# ---------------------------------------------------------------------------
# generate_retrospective — file output
# ---------------------------------------------------------------------------


class TestGenerateRetrospective:
    def test_creates_file(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [_make_task(id="T-1", role="backend")]
        failed: list[Task] = []
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 60,
        )
        retro = tmp_path / "runtime" / "retrospective.md"
        assert retro.exists()

    def test_header_present(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[_make_task()],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 30,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "# Run Retrospective" in content
        assert "## Overview" in content

    def test_completion_rate_100_percent(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [_make_task(id=f"T-{i}") for i in range(5)]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "100%" in content

    def test_failed_tasks_listed(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [_make_task(id="T-1", title="Good task")]
        failed = [_make_task(id="T-2", title="Bad task", status="failed")]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "Bad task" in content
        assert "## Failure Analysis" in content

    def test_role_failure_table_present(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [_make_task(id="T-1", role="backend")]
        failed = [_make_task(id="T-2", role="qa", status="failed")]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "### By role" in content
        assert "qa" in content

    def test_duration_stats_from_metrics(self, tmp_path: Path) -> None:
        collector = _collector_with_tasks(
            tmp_path,
            tasks=[
                ("T-1", "backend", "sonnet", True, 120.0),
                ("T-2", "backend", "sonnet", True, 60.0),
                ("T-3", "qa", "haiku", False, 30.0),
            ],
        )
        done = [_make_task(id="T-1", role="backend"), _make_task(id="T-2", role="backend")]
        failed = [_make_task(id="T-3", role="qa", status="failed")]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 200,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "## Performance" in content
        assert "backend" in content

    def test_cost_breakdown_present(self, tmp_path: Path) -> None:
        collector = _collector_with_tasks(
            tmp_path,
            tasks=[
                ("T-1", "backend", "sonnet", True, 90.0),
                ("T-2", "qa", "haiku", True, 30.0),
            ],
        )
        done = [_make_task(id="T-1"), _make_task(id="T-2")]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 120,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "## Cost Breakdown" in content
        assert "sonnet" in content
        assert "haiku" in content

    def test_recommendations_section_present(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[_make_task()],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "## Recommendations" in content

    def test_recommendation_for_high_failure_rate(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [_make_task(id="T-1")]
        failed = [_make_task(id=f"T-{i}", status="failed") for i in range(2, 10)]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 60,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "failure rate" in content.lower()

    def test_runtime_dir_created_if_missing(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        target = tmp_path / "deeply" / "nested" / "runtime"
        assert not target.exists()
        generate_retrospective(
            done_tasks=[_make_task()],
            failed_tasks=[],
            collector=collector,
            runtime_dir=target,
            run_start_ts=time.time() - 5,
        )
        assert (target / "retrospective.md").exists()

    def test_empty_run(self, tmp_path: Path) -> None:
        """No done or failed tasks should still produce a valid file."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 5,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "# Run Retrospective" in content
        assert "0%" in content or "0 done" in content
