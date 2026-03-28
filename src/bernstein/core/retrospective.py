"""Run retrospective report generation.

Analyses completed and failed tasks alongside in-memory metrics to produce a
post-run retrospective document written to .sdd/runtime/retrospective.md.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from pathlib import Path

from bernstein.core.metrics import MetricsCollector, TaskMetrics
from bernstein.core.models import Complexity, Task

logger = logging.getLogger(__name__)


def generate_retrospective(
    done_tasks: list[Task],
    failed_tasks: list[Task],
    collector: MetricsCollector,
    runtime_dir: Path,
    run_start_ts: float,
) -> None:
    """Write a run retrospective to .sdd/runtime/retrospective.md.

    Analyses task completion rates, duration by role/complexity, cost
    breakdown by model/role, agent failure patterns, and produces
    actionable recommendations.

    Args:
        done_tasks: Tasks with status 'done'.
        failed_tasks: Tasks with status 'failed'.
        collector: Live MetricsCollector instance for in-memory metrics.
        runtime_dir: Directory where retrospective.md is written.
        run_start_ts: Unix timestamp when the run started.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    retro_path = runtime_dir / "retrospective.md"

    all_tasks = done_tasks + failed_tasks
    total = len(all_tasks)
    n_done = len(done_tasks)
    n_failed = len(failed_tasks)
    completion_rate = (n_done / total * 100) if total else 0.0

    wall_clock_s = time.time() - run_start_ts
    total_cost = collector.get_total_cost()
    task_metrics: dict[str, TaskMetrics] = collector._task_metrics  # noqa: SLF001

    lines: list[str] = []
    _section = lines.append

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S")
    _section("# Run Retrospective")
    _section("")
    _section(f"Generated: {ts_str}")
    _section("")

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    _section("## Overview")
    _section("")
    hours, rem = divmod(int(wall_clock_s), 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        duration_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes:
        duration_str = f"{minutes}m {seconds}s"
    else:
        duration_str = f"{seconds}s"

    _section(f"- **Completion rate:** {completion_rate:.0f}% ({n_done} done / {total} total)")
    _section(f"- **Failed tasks:** {n_failed}")
    _section(f"- **Total cost:** ${total_cost:.4f}")
    _section(f"- **Wall-clock duration:** {duration_str}")
    _section("")

    # ------------------------------------------------------------------
    # Failure analysis
    # ------------------------------------------------------------------
    _section("## Failure Analysis")
    _section("")

    # By role
    role_done: dict[str, int] = defaultdict(int)
    role_failed: dict[str, int] = defaultdict(int)
    for t in done_tasks:
        role_done[t.role] += 1
    for t in failed_tasks:
        role_failed[t.role] += 1

    all_roles = sorted(set(role_done) | set(role_failed))
    if all_roles:
        _section("### By role")
        _section("")
        _section("| Role | Done | Failed | Total | Failure rate |")
        _section("|------|------|--------|-------|--------------|")
        for role in all_roles:
            d = role_done[role]
            f = role_failed[role]
            tot = d + f
            rate = f / tot * 100 if tot else 0.0
            _section(f"| {role} | {d} | {f} | {tot} | {rate:.0f}% |")
        _section("")

    # By complexity
    cx_done: dict[str, int] = defaultdict(int)
    cx_failed: dict[str, int] = defaultdict(int)
    for t in done_tasks:
        cx_done[t.complexity.value] += 1
    for t in failed_tasks:
        cx_failed[t.complexity.value] += 1

    all_complexities = sorted(
        set(cx_done) | set(cx_failed),
        key=lambda v: list(Complexity).index(Complexity(v)),
    )
    if all_complexities:
        _section("### By complexity")
        _section("")
        _section("| Complexity | Done | Failed | Total | Failure rate |")
        _section("|------------|------|--------|-------|--------------|")
        for cx in all_complexities:
            d = cx_done[cx]
            f = cx_failed[cx]
            tot = d + f
            rate = f / tot * 100 if tot else 0.0
            _section(f"| {cx} | {d} | {f} | {tot} | {rate:.0f}% |")
        _section("")

    # Failed task titles
    if failed_tasks:
        _section("### Failed task titles")
        _section("")
        for t in sorted(failed_tasks, key=lambda t: t.title):
            _section(f"- {t.title} *(role: {t.role}, complexity: {t.complexity.value})*")
        _section("")

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------
    _section("## Performance")
    _section("")

    # Average duration by role from TaskMetrics
    role_durations: dict[str, list[float]] = defaultdict(list)
    for tm in task_metrics.values():
        if tm.end_time is not None:
            role_durations[tm.role].append(tm.end_time - tm.start_time)

    if role_durations:
        _section("### Average duration by role")
        _section("")
        _section("| Role | Tasks measured | Avg duration |")
        _section("|------|---------------|--------------|")
        for role in sorted(role_durations):
            durs = role_durations[role]
            avg = sum(durs) / len(durs)
            _section(f"| {role} | {len(durs)} | {_fmt_seconds(avg)} |")
        _section("")

    # Average duration by complexity — join task model data via task id
    task_id_to_complexity: dict[str, str] = {
        t.id: t.complexity.value for t in all_tasks
    }
    cx_durations: dict[str, list[float]] = defaultdict(list)
    for tm in task_metrics.values():
        if tm.end_time is not None:
            cx = task_id_to_complexity.get(tm.task_id)
            if cx:
                cx_durations[cx].append(tm.end_time - tm.start_time)

    if cx_durations:
        _section("### Average duration by complexity")
        _section("")
        _section("| Complexity | Tasks measured | Avg duration |")
        _section("|------------|---------------|--------------|")
        for cx in sorted(cx_durations, key=lambda v: list(Complexity).index(Complexity(v))):
            durs = cx_durations[cx]
            avg = sum(durs) / len(durs)
            _section(f"| {cx} | {len(durs)} | {_fmt_seconds(avg)} |")
        _section("")

    # ------------------------------------------------------------------
    # Cost breakdown
    # ------------------------------------------------------------------
    _section("## Cost Breakdown")
    _section("")

    # By model
    model_costs: dict[str, float] = defaultdict(float)
    model_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        m = tm.model or "unknown"
        model_costs[m] += tm.cost_usd
        model_counts[m] += 1

    if model_costs:
        _section("### By model")
        _section("")
        _section("| Model | Tasks | Cost |")
        _section("|-------|-------|------|")
        for model in sorted(model_costs, key=lambda m: model_costs[m], reverse=True):
            _section(f"| {model} | {model_counts[model]} | ${model_costs[model]:.4f} |")
        _section("")

    # By role
    role_costs: dict[str, float] = defaultdict(float)
    role_task_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        role_costs[tm.role] += tm.cost_usd
        role_task_counts[tm.role] += 1

    if role_costs:
        _section("### By role")
        _section("")
        _section("| Role | Tasks | Cost |")
        _section("|------|-------|------|")
        for role in sorted(role_costs, key=lambda r: role_costs[r], reverse=True):
            _section(f"| {role} | {role_task_counts[role]} | ${role_costs[role]:.4f} |")
        _section("")

    # ------------------------------------------------------------------
    # Agent summary
    # ------------------------------------------------------------------
    _section("## Agent Summary")
    _section("")

    agent_metrics = collector._agent_metrics  # noqa: SLF001
    if agent_metrics:
        timed_out_or_killed: list[str] = []
        high_failure: list[str] = []

        _section("| Agent | Role | Tasks done | Tasks failed | Cost |")
        _section("|-------|------|-----------|--------------|------|")
        for am in sorted(agent_metrics.values(), key=lambda a: a.role):
            _section(
                f"| {am.agent_id[:8]} | {am.role} | {am.tasks_completed} "
                f"| {am.tasks_failed} | ${am.total_cost_usd:.4f} |"
            )
            # Dead with no tasks completed and no cost → likely killed/timed out
            if am.tasks_completed == 0 and am.tasks_failed == 0 and am.end_time is not None:
                timed_out_or_killed.append(f"{am.agent_id[:8]} ({am.role})")
            tot = am.tasks_completed + am.tasks_failed
            if tot >= 2 and am.tasks_failed / tot > 0.5:
                high_failure.append(f"{am.agent_id[:8]} ({am.role})")
        _section("")

        if timed_out_or_killed:
            _section("### Agents that may have been killed or timed out")
            _section("")
            for entry in timed_out_or_killed:
                _section(f"- {entry}")
            _section("")

        if high_failure:
            _section("### Agents with high failure rates")
            _section("")
            for entry in high_failure:
                _section(f"- {entry}")
            _section("")
    else:
        _section("*(No in-memory agent metrics available.)*")
        _section("")

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------
    _section("## Recommendations")
    _section("")
    recommendations = _build_recommendations(
        n_done=n_done,
        n_failed=n_failed,
        role_failed=role_failed,
        role_done=role_done,
        cx_failed=cx_failed,
        total_cost=total_cost,
        wall_clock_s=wall_clock_s,
    )
    if recommendations:
        for rec in recommendations:
            _section(f"- {rec}")
    else:
        _section("- No issues detected; run looks healthy.")
    _section("")

    retro_path.write_text("\n".join(lines))
    logger.info("Retrospective written to .sdd/runtime/retrospective.md")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_seconds(s: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    if s < 60:
        return f"{s:.1f}s"
    minutes, secs = divmod(int(s), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m {secs}s"


def _build_recommendations(
    *,
    n_done: int,
    n_failed: int,
    role_failed: dict[str, int],
    role_done: dict[str, int],
    cx_failed: dict[str, int],
    total_cost: float,
    wall_clock_s: float,
) -> list[str]:
    """Return a list of recommendation strings based on run metrics.

    Args:
        n_done: Number of completed tasks.
        n_failed: Number of failed tasks.
        role_failed: Count of failures per role.
        role_done: Count of successes per role.
        cx_failed: Count of failures per complexity level.
        total_cost: Total cost in USD.
        wall_clock_s: Wall-clock duration in seconds.

    Returns:
        List of recommendation strings (may be empty).
    """
    recs: list[str] = []

    total = n_done + n_failed
    if total == 0:
        return recs

    overall_fail_rate = n_failed / total
    if overall_fail_rate >= 0.5:
        recs.append(
            f"Overall failure rate is {overall_fail_rate:.0%} — review task definitions "
            "and agent prompts before the next run."
        )

    # Per-role recommendations
    for role in sorted(set(role_failed) | set(role_done)):
        f = role_failed.get(role, 0)
        d = role_done.get(role, 0)
        tot = f + d
        if tot >= 2 and f / tot >= 0.5:
            recs.append(
                f"Role '{role}' has a {f / tot:.0%} failure rate ({f}/{tot}) — "
                "review role prompts and task descriptions."
            )

    # Per-complexity recommendations
    for cx in sorted(cx_failed):
        f = cx_failed[cx]
        # We don't have cx_done here — just flag high absolute failure counts
        if f >= 3:
            recs.append(
                f"Complexity '{cx}' had {f} failures — consider breaking these tasks "
                "into smaller pieces or raising estimated_minutes."
            )

    # Cost warnings
    if total_cost > 5.0:
        recs.append(
            f"Cost ${total_cost:.2f} is high — review model selection; consider "
            "routing more tasks to haiku or free-tier providers."
        )

    # Duration warnings (> 2 hours)
    if wall_clock_s > 7200:
        recs.append(
            "Run exceeded 2 hours — consider parallelising independent tasks or "
            "increasing max_agents."
        )

    return recs
