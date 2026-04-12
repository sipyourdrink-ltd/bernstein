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

from bernstein.core.metrics import get_collector
from bernstein.core.retrospective import generate_retrospective

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


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

    task_lines: list[str] = []
    for task in sorted(done_tasks, key=lambda t: t.title):
        task_lines.append(f"- [x] {task.title}")
    for task in sorted(failed_tasks, key=lambda t: t.title):
        task_lines.append(f"- [ ] {task.title} *(failed)*")

    hours, rem = divmod(int(wall_clock_s), 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        duration_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes:
        duration_str = f"{minutes}m {seconds}s"
    else:
        duration_str = f"{seconds}s"

    lines = [
        "# Run Summary",
        "",
        f"**Total completed:** {total_completed}",
        f"**Total failed:** {total_failed}",
        f"**Files modified:** {files_modified}",
        f"**Estimated cost:** ${total_cost:.4f}",
        f"**Wall-clock duration:** {duration_str}",
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

    generate_retrospective(
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        collector=collector,
        runtime_dir=runtime_dir,
        run_start_ts=orch._run_start_ts,
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

    summary_data = RunSummaryData(
        run_id=orch._run_id,
        tasks_completed=len(done_tasks),
        tasks_total=total,
        tasks_failed=len(failed_tasks),
        wall_clock_seconds=wall_clock_s,
        total_cost_usd=total_cost,
        quality_score=quality_score,
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
