"""Orchestrator run summary: end-of-run reports and summary cards.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.savings_calculator import calculate_savings
from bernstein.core.metrics import get_collector
from bernstein.core.retrospective import generate_retrospective

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def generate_run_summary(
    orch: Any,
    done_tasks: list[Task],
    failed_tasks: list[Task],
) -> None:
    """Write a run completion summary to .sdd/runtime/summary.md.

    Args:
        orch: The orchestrator instance.
        done_tasks: Tasks that completed successfully.
        failed_tasks: Tasks that failed.
    """
    runtime_dir = orch._workdir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    summary_path = runtime_dir / "summary.md"

    total_completed = len(done_tasks)
    total_failed = len(failed_tasks)
    wall_clock_s = time.time() - orch._run_start_ts

    collector = get_collector(orch._workdir / ".sdd" / "metrics")
    total_cost = collector.get_total_cost()
    files_modified: int = sum(getattr(m, "files_modified", 0) for m in collector.task_metrics.values())
    savings = calculate_savings(
        collector.task_metrics.values(),
        wall_clock_seconds=wall_clock_s,
        total_cost_usd=total_cost,
        completed_tasks=total_completed,
    )

    task_lines: list[str] = [f"- [x] {task.title}" for task in sorted(done_tasks, key=lambda t: t.title)]
    for task in sorted(failed_tasks, key=lambda t: t.title):
        task_lines.append(f"- [ ] {task.title} *(failed)*")

    duration_str = _format_duration(wall_clock_s)

    lines = [
        "# Run Summary",
        "",
        f"**Total completed:** {total_completed}",
        f"**Total failed:** {total_failed}",
        f"**Files modified:** {files_modified}",
        f"**Estimated cost:** ${total_cost:.4f}",
        f"**Cost per task:** ${savings.cost_per_task_usd:.4f}",
        f"**Wall-clock duration:** {duration_str}",
        f"**Sequential estimate:** {_format_duration(savings.sequential_time_seconds)}",
        f"**Time saved:** {_format_duration(savings.time_saved_seconds)} ({savings.time_saved_pct:.0%})",
        f"**Model routing savings:** ${savings.routing_savings_usd:.4f}",
        "",
        "## Tasks",
        "",
    ]
    lines.extend(task_lines)
    lines.append("")

    summary_path.write_text("\n".join(lines))
    orch._summary_written = True
    logger.info("Run complete. Summary at .sdd/runtime/summary.md")

    orch._post_bulletin(
        "status",
        f"run complete: {total_completed} tasks done, {total_failed} failed, "
        f"${total_cost:.4f} spent, {duration_str} elapsed",
    )
    orch._notify(
        "run.completed",
        "Bernstein run complete",
        f"{total_completed} tasks done, {total_failed} failed in {duration_str}.",
        tasks_completed=total_completed,
        tasks_failed=total_failed,
        files_modified=files_modified,
        cost_usd=round(total_cost, 4),
        duration=duration_str,
    )

    # NOTE: this generate_run_summary() entry point fires from a tick-level
    # "queue looks empty" heuristic, not true orchestrator shutdown - tasks
    # spawned just before this point can still fail afterwards. Mark the
    # retrospective INTERIM so a stale HEALTHY snapshot is never mistaken
    # for the final report (see A5 stale-retrospective bug).
    generate_retrospective(
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        collector=collector,
        runtime_dir=runtime_dir,
        run_start_ts=orch._run_start_ts,
        trigger_reason="mid-run",
    )

    emit_summary_card(
        orch,
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        collector=collector,
        wall_clock_s=wall_clock_s,
        total_cost=total_cost,
    )


def emit_summary_card(
    orch: Any,
    done_tasks: list[Task],
    failed_tasks: list[Task],
    collector: Any,
    wall_clock_s: float,
    total_cost: float,
) -> None:
    """Print the end-of-run summary card and write summary.json.

    Suppressed when the ``BERNSTEIN_QUIET`` environment variable is set.

    Args:
        orch: The orchestrator instance.
        done_tasks: Completed tasks.
        failed_tasks: Failed tasks.
        collector: Live MetricsCollector for quality metrics.
        wall_clock_s: Wall-clock duration in seconds.
        total_cost: Total cost in USD.
    """
    from bernstein.cli.summary_card import RunSummaryData, print_summary_card, write_summary_json

    total = len(done_tasks) + len(failed_tasks)

    # Quality score: fraction of completed tasks where janitor verification passed.
    task_metrics = collector._task_metrics  # type: ignore[reportPrivateUsage]
    verified = [m for m in task_metrics.values() if m.end_time is not None]
    quality_score: float | None = None
    if verified:
        quality_score = sum(1 for m in verified if m.janitor_passed) / len(verified)

    savings = calculate_savings(
        task_metrics.values(),
        wall_clock_seconds=wall_clock_s,
        total_cost_usd=total_cost,
        completed_tasks=len(done_tasks),
    )

    # Issue #3014: surface requested-vs-actual isolation downgrades recorded by
    # the spawner. Guarded so a stubbed/mock spawner without a real list is safe.
    spawner_downgrades = getattr(getattr(orch, "_spawner", None), "isolation_downgrades", None)
    isolation_downgrades = [d.as_dict() for d in spawner_downgrades] if isinstance(spawner_downgrades, list) else []

    summary_data = RunSummaryData(
        run_id=orch._run_id,
        tasks_completed=len(done_tasks),
        tasks_total=total,
        tasks_failed=len(failed_tasks),
        wall_clock_seconds=wall_clock_s,
        total_cost_usd=total_cost,
        quality_score=quality_score,
        sequential_time_seconds=savings.sequential_time_seconds,
        cost_per_task_usd=savings.cost_per_task_usd,
        routing_savings_usd=savings.routing_savings_usd,
        isolation_downgrades=isolation_downgrades,
    )

    sdd_dir = orch._workdir / ".sdd"
    try:
        write_summary_json(summary_data, orch._run_id, sdd_dir)
    except OSError as exc:
        logger.warning("Failed to write summary.json: %s", exc)

    quiet = os.environ.get("BERNSTEIN_QUIET", "").strip() == "1"
    if not quiet:
        try:
            print_summary_card(summary_data)
        except Exception as exc:
            logger.debug("Summary card render failed (non-critical): %s", exc)
