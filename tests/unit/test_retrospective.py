"""Tests for bernstein.core.retrospective."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from bernstein.core.metrics import MetricsCollector
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.retrospective import (
    EXIT_RUN_HEALTHY,
    EXIT_RUN_UNHEALTHY,
    TERMINATOR_AGENT_COMPLETED,
    TERMINATOR_AGENT_REPORTED_FAILURE,
    TERMINATOR_AUTO_COMPLETED_AFTER_DEATH,
    TERMINATOR_INCOMPLETE_DECLARED,
    TERMINATOR_JANITOR_REJECTED,
    TERMINATOR_WATCHDOG_KILLED,
    _build_recommendations,
    _fmt_seconds,
    classify_task_terminator,
    compute_run_health,
    count_incomplete_declared,
    count_never_terminal,
    generate_retrospective,
    run_health_exit_code,
    run_healthy_from_status_counts,
)

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
    result_summary: str | None = None,
    terminal_reason: str | None = None,
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
        result_summary=result_summary,
        terminal_reason=terminal_reason,
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
# generate_retrospective - file output
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

    def test_incomplete_declared_task_reports_unhealthy(self, tmp_path: Path) -> None:
        """Issue #3010 end-to-end: an empty done/failed run whose only declared
        task is still open/claimed at shutdown must render UNHEALTHY, not the
        old '0/0 ... HEALTHY'. The full task-status histogram carries the
        stuck task even when done_tasks/failed_tasks are empty."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 30,
            full_status_counts={"open": 1},
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "- **Verdict:** UNHEALTHY" in content
        assert "- **Verdict:** HEALTHY" not in content

        assert "Declared task unfinished" in content

    def test_stuck_task_is_not_double_counted_across_sections(self, tmp_path: Path) -> None:
        """One reaped no-output task must be counted ONCE.

        The collector sees it as unresolved (started, never completed) and the
        status histogram sees it as open, so summing the two signals reported
        "2/2 task terminations were NOT genuine" while the Overview said
        "0 done / 1 total" -- the same task counted twice, and the two sections
        contradicting each other.
        """
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        # Task started via the collector but never completed -> unresolved.
        collector.start_task("T-stuck", "manager", "sonnet", "qwen")
        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 30,
            # ...and still open server-side -> incomplete-declared. Same task.
            full_status_counts={"open": 1},
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()

        assert "- **Verdict:** UNHEALTHY" in content
        # Overview and Run Health must agree on the denominator: 1 task, not 2.
        assert "0 done / 1 total" in content
        assert "1/1 task terminations were NOT genuine" in content
        assert "2/2 task terminations were NOT genuine" not in content

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


# ---------------------------------------------------------------------------
# classify_task_terminator / compute_run_health - run-health honesty
# ---------------------------------------------------------------------------


class TestClassifyTaskTerminator:
    def test_genuine_completion_with_no_result_summary(self) -> None:
        task = _make_task(status="done")
        assert classify_task_terminator(task) == TERMINATOR_AGENT_COMPLETED

    def test_watchdog_heartbeat_kill(self) -> None:
        task = _make_task(
            status="failed",
            result_summary="Retried: Agent sess-1 reaped (heartbeat timeout)",
        )
        assert classify_task_terminator(task) == TERMINATOR_WATCHDOG_KILLED

    def test_janitor_rejection(self) -> None:
        task = _make_task(
            status="failed",
            result_summary="Agent sess-1 died; janitor failed: ['test_passes: ...']",
        )
        assert classify_task_terminator(task) == TERMINATOR_JANITOR_REJECTED

    def test_auto_completed_after_death(self) -> None:
        task = _make_task(
            status="done",
            result_summary="Auto-completed: agent sess-1 made git commits on branch (no signals to verify)",
        )
        assert classify_task_terminator(task) == TERMINATOR_AUTO_COMPLETED_AFTER_DEATH

    def test_agent_completed_ignores_unrelated_summary_text(self) -> None:
        task = _make_task(status="done", result_summary="Implemented the widget factory as requested.")
        assert classify_task_terminator(task) == TERMINATOR_AGENT_COMPLETED

    def test_failed_task_with_no_matching_marker_is_agent_reported_failure_not_completed(self) -> None:
        """A FAILED task must never be classified as TERMINATOR_AGENT_COMPLETED,
        even when its result_summary text doesn't match any forced-kill
        marker. Ground truth: D2 openrouter leg -- a raw provider
        BadRequestError (deepseek max_tokens > context cap) had none of the
        watchdog/janitor/timeout/other-forced/auto-completed keywords and
        fell through to the "genuine completion" default."""
        task = _make_task(
            status="failed",
            result_summary="BadRequestError: maximum context length is 163840 tokens",
        )
        assert classify_task_terminator(task) == TERMINATOR_AGENT_REPORTED_FAILURE


class TestComputeRunHealth:
    def test_empty_is_vacuously_healthy(self) -> None:
        healthy, counts = compute_run_health([])
        assert healthy is True
        assert counts == {}

    def test_all_agent_completed_is_healthy(self) -> None:
        tasks = [_make_task(id=f"T-{i}", status="done") for i in range(5)]
        healthy, counts = compute_run_health(tasks)
        assert healthy is True
        assert counts[TERMINATOR_AGENT_COMPLETED] == 5

    def test_majority_watchdog_killed_is_unhealthy(self) -> None:
        """Ground truth: run-9 attempt-9 had 21 spawns / 19 merge refusals driven by
        SHUTDOWN(no_heartbeat) kills, yet the run was never flagged unhealthy."""
        watchdog_killed = [
            _make_task(
                id=f"T-wd-{i}",
                status="failed",
                result_summary=f"Retried: Agent sess-{i} reaped (heartbeat timeout)",
            )
            for i in range(19)
        ]
        genuine = [_make_task(id=f"T-ok-{i}", status="done") for i in range(2)]
        healthy, counts = compute_run_health(watchdog_killed + genuine)
        assert healthy is False
        assert counts[TERMINATOR_WATCHDOG_KILLED] == 19
        assert counts[TERMINATOR_AGENT_COMPLETED] == 2

    def test_minority_forced_terminations_still_healthy_when_no_task_actually_failed(self) -> None:
        """The fraction-based heuristic only gets a chance to run when the
        hard rule (any FAILED-status task, any auto-completed-after-death,
        any unresolved-in-metrics) does not already fire. A stray
        forced-kill-shaped marker on an otherwise DONE task (e.g. leftover
        text from an earlier retry that later succeeded) below the 50%
        threshold should not sink the verdict."""
        stale_marker_but_done = [
            _make_task(
                id="T-wd-1",
                status="done",
                result_summary="Retried: Agent sess-1 reaped (heartbeat timeout); retried and succeeded",
            )
        ]
        genuine = [_make_task(id=f"T-ok-{i}", status="done") for i in range(9)]
        healthy, counts = compute_run_health(stale_marker_but_done + genuine)
        assert healthy is True
        assert counts[TERMINATOR_WATCHDOG_KILLED] == 1

    def test_any_failed_task_forces_unhealthy_hard_rule(self) -> None:
        """Regression for the D2 openrouter leg (KILL-NOTE.md, 2026-07-02):
        a single failed task whose result_summary/terminal_reason text does
        not match ANY forced-kill marker (e.g. a raw provider BadRequestError)
        used to fall through classify_task_terminator's default and count as
        TERMINATOR_AGENT_COMPLETED, so compute_run_health never saw it and
        reported HEALTHY for a 100%-failure run. The hard rule (n_failed > 0)
        must catch this regardless of text-pattern matching."""
        failed = [
            _make_task(
                id="T-1",
                status="failed",
                result_summary=(
                    "BadRequestError: Error code: 400 - \"This endpoint's maximum context "
                    'length is 163840 tokens. However, you requested about 201099 tokens."'
                ),
            )
        ]
        healthy, counts = compute_run_health(failed)
        assert healthy is False
        assert counts[TERMINATOR_AGENT_REPORTED_FAILURE] == 1
        assert counts.get(TERMINATOR_AGENT_COMPLETED, 0) == 0

    def test_unresolved_in_metrics_forces_unhealthy(self) -> None:
        healthy, _counts = compute_run_health([_make_task(id="T-ok", status="done")], n_unresolved=1)
        assert healthy is False

    def test_incomplete_declared_task_forces_unhealthy_on_empty_run(self) -> None:
        """Issue #3010: a single-task run whose only agent produced zero model
        output and was reaped left its one task open/claimed -- neither done nor
        failed -- so done+failed was empty. A 0/0 run with a declared task that
        never finished must NOT be HEALTHY, and the reap must show in the tally
        rather than being silently dropped."""
        healthy, counts = compute_run_health([], n_incomplete_declared=1)
        assert healthy is False
        assert counts.get(TERMINATOR_INCOMPLETE_DECLARED, 0) == 1

    def test_incomplete_declared_alongside_completed_still_unhealthy(self) -> None:
        healthy, counts = compute_run_health([_make_task(id="T-ok", status="done")], n_incomplete_declared=1)
        assert healthy is False
        assert counts[TERMINATOR_AGENT_COMPLETED] == 1
        assert counts[TERMINATOR_INCOMPLETE_DECLARED] == 1

    def test_zero_incomplete_declared_preserves_empty_counts(self) -> None:
        """The incomplete-declared key must not leak into the empty-run tally."""
        healthy, counts = compute_run_health([], n_incomplete_declared=0)
        assert healthy is True
        assert counts == {}


class TestCountIncompleteDeclared:
    def test_none_histogram_is_zero(self) -> None:
        assert count_incomplete_declared(None) == 0

    def test_counts_open_claimed_in_progress_orphaned(self) -> None:
        histogram = {"open": 1, "claimed": 2, "in_progress": 1, "orphaned": 1, "done": 3, "failed": 1}
        assert count_incomplete_declared(histogram) == 5

    def test_excludes_terminal_and_parking_states(self) -> None:
        histogram = {"done": 2, "failed": 1, "refused": 1, "planned": 1, "suspended": 1, "pending_approval": 1}
        assert count_incomplete_declared(histogram) == 0


class TestCountNeverTerminal:
    """The two never-terminated signals describe one population, so they are
    combined by max() -- summing counted the same reaped task twice and made
    the Run Health totals disagree with the Overview."""

    def test_overlapping_signals_are_not_summed(self) -> None:
        # The #3010 repro: ONE task, seen by both the collector (unresolved)
        # and the status histogram (open) -> it must count once, not twice.
        assert count_never_terminal(1, 1) == 1

    def test_takes_the_larger_estimate(self) -> None:
        assert count_never_terminal(3, 1) == 3
        assert count_never_terminal(1, 4) == 4

    def test_zero_when_neither_signal_fired(self) -> None:
        assert count_never_terminal(0, 0) == 0


class TestRunHealthExitCode:
    def test_healthy_exits_zero(self) -> None:
        assert run_health_exit_code(healthy=True) == EXIT_RUN_HEALTHY == 0

    def test_unhealthy_exits_distinct_nonzero(self) -> None:
        code = run_health_exit_code(healthy=False)
        assert code == EXIT_RUN_UNHEALTHY
        assert code != 0

    def test_unhealthy_code_does_not_collide_with_cli_error_codes(self) -> None:
        """The outcome signal is only machine-readable if it cannot be confused
        with a CLI error: click raises UsageError/NoSuchOption with exit code 2,
        run_bootstrap raises SystemExit(2) on seed/config failures, and 1 is the
        generic error code."""
        assert EXIT_RUN_UNHEALTHY not in (0, 1, 2)


class TestRunHealthyFromStatusCounts:
    def test_none_or_empty_is_healthy(self) -> None:
        assert run_healthy_from_status_counts(None) is True
        assert run_healthy_from_status_counts({}) is True

    def test_all_done_is_healthy(self) -> None:
        assert run_healthy_from_status_counts({"total": 3, "done": 3}) is True

    def test_stuck_declared_task_is_unhealthy(self) -> None:
        # Issue #3010 repro: total 1, done 0, failed 0, but the one task is
        # still open -> the run did not meet its goal.
        assert run_healthy_from_status_counts({"total": 1, "open": 1}) is False

    def test_failed_task_is_unhealthy(self) -> None:
        assert run_healthy_from_status_counts({"total": 1, "failed": 1}) is False


class TestRunHealthInRecommendations:
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

    def test_unhealthy_run_always_produces_a_recommendation(self) -> None:
        recs = self._call(
            run_healthy=False,
            terminator_counts={TERMINATOR_WATCHDOG_KILLED: 19, TERMINATOR_AGENT_COMPLETED: 2},
        )
        assert any("UNHEALTHY" in r for r in recs)
        assert any(TERMINATOR_WATCHDOG_KILLED in r for r in recs)

    def test_healthy_run_with_no_other_issues_stays_empty(self) -> None:
        assert self._call(run_healthy=True) == []


class TestGenerateRetrospectiveRunHealth:
    def test_healthy_run_reports_healthy_verdict(self, tmp_path: Path) -> None:
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
        assert "## Run Health" in content
        assert "**Verdict:** HEALTHY" in content
        assert "No issues detected; run looks healthy." in content

    def test_watchdog_killed_majority_reports_unhealthy_not_healthy(self, tmp_path: Path) -> None:
        """The core regression: a run where most terminations are watchdog
        kills must NOT be reported as healthy, even with a high nominal
        completion rate on the surviving tasks."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        failed = [
            _make_task(
                id=f"T-wd-{i}",
                status="failed",
                result_summary=f"Retried: Agent sess-{i} reaped (heartbeat timeout)",
            )
            for i in range(19)
        ]
        done = [_make_task(id=f"T-ok-{i}") for i in range(2)]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=failed,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 60,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Verdict:** UNHEALTHY" in content
        assert "No issues detected; run looks healthy." not in content
        assert "UNHEALTHY" in content
        assert "Watchdog-killed | 19" in content

    def test_case1_stale_snapshot_reconciles_4_failed_from_metrics(self, tmp_path: Path) -> None:
        """Regression for D2 claude/sdd-snapshot attempt 2 (2026-07-02): the
        orchestrator's done_tasks/failed_tasks arrived here EMPTY (stale
        tasks_by_status snapshot fetched before this tick's own reaping
        persisted the 4 failures), yet the CLI's own final tally said
        "Failed: 4" and collector.task_metrics has 4 started-but-never-
        completed manager attempts (retry_or_fail_task never calls
        collector.complete_task()). The retrospective must reconcile from
        task_metrics ground truth: 4 failed, 0 done, 4 total, UNHEALTHY --
        not "0 done / 0 total ... HEALTHY" as it did before this fix."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        for i in range(4):
            collector.start_task(f"mgr-attempt-{i}", "manager", "claude-sonnet-5", "claude")
        run_start = time.time() - 382  # 6m22s, matching the ground-truth wall clock
        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=run_start,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Completion rate:** 0% (0 done / 4 total)" in content
        assert "**Failed tasks:** 4" in content
        assert "**Verdict:** UNHEALTHY" in content
        assert "6m 22s" in content
        assert "No issues detected; run looks healthy." not in content

    def test_case3_auto_completed_after_death_counted_when_task_object_available(self, tmp_path: Path) -> None:
        """Regression for D2 claude/attempt1-tools-zero (2026-07-02): a task
        that was auto-completed after its agent died (result_summary
        written by agent_lifecycle.py's orphan-handling path) must be
        classified as TERMINATOR_AUTO_COMPLETED_AFTER_DEATH and must force
        the verdict to UNHEALTHY, never silently folded into a healthy
        "0 auto-completed" count."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        done = [
            _make_task(
                id="mgr-orphan-1",
                status="done",
                result_summary="Auto-completed after agent sess-1 died; janitor passed",
            )
        ]
        generate_retrospective(
            done_tasks=done,
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 53,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Verdict:** UNHEALTHY" in content
        assert "Auto-completed after agent death | 1" in content
        assert "No issues detected; run looks healthy." not in content


class TestRetrospectiveRegeneration:
    """Regression for the A5 stale-retrospective bug: a canary run's
    retrospective reported "100% completion, HEALTHY" at T+58s (generated
    right after the manager's own task finished), and 2 of the run's 3
    total tasks subsequently failed (janitor-rejected) without the report
    ever being regenerated. The fix requires that (1) a mid-run generation
    is labeled INTERIM and (2) a later "shutdown-final" generation for the
    same retrospective.md path always overwrites it with the true final
    state, never leaving the stale HEALTHY snapshot in place."""

    def test_final_generation_overwrites_stale_mid_run_healthy_snapshot(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        runtime_dir = tmp_path / "runtime"

        # T+58s: manager task alone has finished. Mid-run heuristic fires
        # with only the 1 completed task visible - 100%/HEALTHY.
        manager_task = _make_task(id="mgr-1", title="Decompose goal", role="manager", status="done")
        generate_retrospective(
            done_tasks=[manager_task],
            failed_tasks=[],
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=time.time() - 58,
            trigger_reason="mid-run",
        )
        interim_content = (runtime_dir / "retrospective.md").read_text()
        assert "INTERIM" in interim_content
        assert "**Verdict:** HEALTHY" in interim_content
        assert "**Completion rate:** 100% (1 done / 1 total)" in interim_content

        # T+3min: the 2 child tasks (backend, qa) subsequently fail
        # (janitor-rejected). True orchestrator shutdown regenerates the
        # retrospective with the FINAL task lists, overwriting the stale
        # snapshot above.
        backend_task = _make_task(
            id="backend-1",
            title="Add hello subcommand to cli.py",
            role="backend",
            status="failed",
            result_summary="Agent backend-1 died; janitor failed: ['test_passes: ...']",
        )
        qa_task = _make_task(
            id="qa-1",
            title="Add test for hello subcommand",
            role="qa",
            status="failed",
            result_summary="Agent qa-1 died; janitor failed: ['test_passes: ...']",
        )
        generate_retrospective(
            done_tasks=[manager_task],
            failed_tasks=[backend_task, qa_task],
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=time.time() - 180,
        )
        final_content = (runtime_dir / "retrospective.md").read_text()

        # The stale HEALTHY snapshot must be gone, not merely appended to.
        assert "INTERIM" not in final_content
        assert "**Verdict:** UNHEALTHY" in final_content
        assert "**Completion rate:** 33% (1 done / 3 total)" in final_content
        assert "**Failed tasks:** 2" in final_content
        assert "Add hello subcommand to cli.py" in final_content
        assert "Add test for hello subcommand" in final_content
        assert "No issues detected; run looks healthy." not in final_content

    def test_mid_run_generation_is_labeled_interim(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[_make_task(id="T-1")],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
            trigger_reason="mid-run",
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "INTERIM" in content
        assert "mid-run" in content

    def test_shutdown_final_generation_has_no_interim_label(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        generate_retrospective(
            done_tasks=[_make_task(id="T-1")],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "INTERIM" not in content


class TestUnresolvedMetricsEntry:
    def test_unresolved_metrics_entry_reconciles_into_failed_count(self, tmp_path: Path) -> None:
        """A task_metrics entry the collector started but that never reached
        done_tasks/failed_tasks nor collector.complete_task() (end_time is
        None) must be counted as failed/unresolved rather than silently
        dropped -- this is the generalised proxy for both the auto-complete-
        after-death and stale-snapshot failure modes when no Task object
        with result_summary text is available at all."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        collector.start_task("orphan-task-1", "manager", "claude-sonnet-5", "claude")
        generate_retrospective(
            done_tasks=[],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 53,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Failed tasks:** 1" in content
        assert "**Verdict:** UNHEALTHY" in content
        assert "Unresolved in metrics (started, outcome never reconciled) | 1" in content


class TestPersistedCostReconciliation:
    """Cover the retrospective's cross-check against the durable
    .sdd/metrics/tasks.jsonl sidecar (see _read_persisted_task_costs).

    Ground truth: orphan-recovery fold-ins (agent_lifecycle.py
    orphan_cost_folded_in) can land in the persisted sidecar without ever
    reaching the in-memory MetricsCollector's task_metrics entry for that
    task_id by the time generate_retrospective() is called -- the
    retrospective must not silently report $0 in that case.
    """

    def _write_sidecar(self, tmp_path: Path, rows: list[dict[str, object]]) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        sidecar = metrics_dir / "tasks.jsonl"
        with sidecar.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_zero_in_memory_cost_reconciled_from_sidecar(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        # Task started via start_task() (as agent_lifecycle.py does at spawn
        # time) but its cost was never folded into the collector -- cost_usd
        # stays at the dataclass default of 0.0.
        collector.start_task("T-orphan", "qa", "MiniMax-M2.7-highspeed", "openai_agents")
        # But the durable sidecar (written by the evolution aggregator from
        # the orphan/auto-complete path) DOES have the real recorded cost.
        self._write_sidecar(tmp_path, [{"task_id": "T-orphan", "cost_usd": 0.009851}])

        done = [_make_task(id="T-orphan", role="qa", status="failed")]
        generate_retrospective(
            done_tasks=[],
            failed_tasks=done,
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 30,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Total cost:** $0.0099" in content

    def test_no_sidecar_file_does_not_crash(self, tmp_path: Path) -> None:
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        collector.start_task("T-1", "backend", "claude-sonnet-5", "claude")
        generate_retrospective(
            done_tasks=[_make_task(id="T-1")],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        retro = tmp_path / "runtime" / "retrospective.md"
        assert retro.exists()
        assert "**Total cost:** $0.0000" in retro.read_text()

    def test_nonzero_in_memory_cost_not_overwritten_by_sidecar(self, tmp_path: Path) -> None:
        """If the collector already has a real recorded cost for a task,
        the sidecar cross-check must not clobber it (only $0.0 entries are
        eligible for reconciliation)."""
        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        m = collector.start_task("T-1", "backend", "claude-sonnet-5", "claude")
        m.end_time = m.start_time + 5.0
        m.success = True
        m.cost_usd = 0.05
        # Sidecar disagrees (e.g. stale/partial write) -- in-memory wins
        # because it is non-zero.
        self._write_sidecar(tmp_path, [{"task_id": "T-1", "cost_usd": 999.0}])

        generate_retrospective(
            done_tasks=[_make_task(id="T-1")],
            failed_tasks=[],
            collector=collector,
            runtime_dir=tmp_path / "runtime",
            run_start_ts=time.time() - 10,
        )
        content = (tmp_path / "runtime" / "retrospective.md").read_text()
        assert "**Total cost:** $0.0500" in content

    def test_cost_aggregation_log_line_emitted(self, tmp_path: Path, caplog: object) -> None:
        import logging

        collector = MetricsCollector(metrics_dir=tmp_path / "metrics")
        collector.start_task("T-orphan", "qa", "MiniMax-M2.7-highspeed", "openai_agents")
        self._write_sidecar(tmp_path, [{"task_id": "T-orphan", "cost_usd": 0.009851}])

        with caplog.at_level(logging.INFO, logger="bernstein.core.quality.retrospective"):  # type: ignore[attr-defined]
            generate_retrospective(
                done_tasks=[],
                failed_tasks=[_make_task(id="T-orphan", role="qa", status="failed")],
                collector=collector,
                runtime_dir=tmp_path / "runtime",
                run_start_ts=time.time() - 30,
            )
        messages = [r.message for r in caplog.records]  # type: ignore[attr-defined]
        assert any(m.startswith("retrospective_cost_aggregation: source=") for m in messages)
        assert any("reconciled_from_sidecar=1" in m for m in messages)
