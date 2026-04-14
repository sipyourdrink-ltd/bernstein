"""Run retrospective report generation.

Analyses completed and failed tasks alongside in-memory metrics to produce a
post-run retrospective document written to .sdd/runtime/retrospective.md.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bernstein.core.models import Complexity, Task

if TYPE_CHECKING:
    from bernstein.core.metrics import MetricsCollector, TaskMetrics

logger = logging.getLogger(__name__)


def _count_by_field(tasks: list[Task], field: str) -> dict[str, int]:
    """Count tasks grouped by a given field name."""
    counts: dict[str, int] = defaultdict(int)
    for t in tasks:
        val = getattr(t, field)
        key = val.value if hasattr(val, "value") else str(val)
        counts[key] += 1
    return counts


def _write_rate_table(
    lines: list[str],
    header: str,
    columns: str,
    separator: str,
    done_counts: dict[str, int],
    failed_counts: dict[str, int],
    sort_key: object = None,
) -> None:
    """Write a rate table (by role or complexity) into *lines*."""
    all_keys = sorted(set(done_counts) | set(failed_counts), key=sort_key)  # type: ignore[arg-type]
    if not all_keys:
        return
    lines.append(header)
    lines.append("")
    lines.append(columns)
    lines.append(separator)
    for key in all_keys:
        d = done_counts.get(key, 0)
        f = failed_counts.get(key, 0)
        tot = d + f
        rate = f / tot * 100 if tot else 0.0
        lines.append(f"| {key} | {d} | {f} | {tot} | {rate:.0f}% |")
    lines.append("")


def _write_failure_analysis(lines: list[str], done_tasks: list[Task], failed_tasks: list[Task]) -> None:
    """Write the Failure Analysis section."""
    lines.append("## Failure Analysis")
    lines.append("")

    role_done = _count_by_field(done_tasks, "role")
    role_failed = _count_by_field(failed_tasks, "role")
    _write_rate_table(
        lines, "### By role", "| Role | Done | Failed | Total | Failure rate |",
        "|------|------|--------|-------|--------------|", role_done, role_failed,
    )

    cx_done = _count_by_field(done_tasks, "complexity")
    cx_failed = _count_by_field(failed_tasks, "complexity")
    _write_rate_table(
        lines, "### By complexity", "| Complexity | Done | Failed | Total | Failure rate |",
        "|------------|------|--------|-------|--------------|", cx_done, cx_failed,
        sort_key=lambda v: list(Complexity).index(Complexity(v)),
    )

    if failed_tasks:
        lines.append("### Failed task titles")
        lines.append("")
        for t in sorted(failed_tasks, key=lambda t: t.title):
            lines.append(f"- {t.title} *(role: {t.role}, complexity: {t.complexity.value})*")
        lines.append("")


def _write_performance_section(
    lines: list[str],
    task_metrics: dict[str, TaskMetrics],
    all_tasks: list[Task],
) -> None:
    """Write the Performance section."""
    lines.append("## Performance")
    lines.append("")

    role_durations: dict[str, list[float]] = defaultdict(list)
    for tm in task_metrics.values():
        if tm.end_time is not None:
            role_durations[tm.role].append(tm.end_time - tm.start_time)

    if role_durations:
        lines.append("### Average duration by role")
        lines.append("")
        lines.append("| Role | Tasks measured | Avg duration |")
        lines.append("|------|---------------|--------------|")
        for role in sorted(role_durations):
            durs = role_durations[role]
            lines.append(f"| {role} | {len(durs)} | {_fmt_seconds(sum(durs) / len(durs))} |")
        lines.append("")

    task_id_to_cx: dict[str, str] = {t.id: t.complexity.value for t in all_tasks}
    cx_durations: dict[str, list[float]] = defaultdict(list)
    for tm in task_metrics.values():
        if tm.end_time is not None:
            cx = task_id_to_cx.get(tm.task_id)
            if cx:
                cx_durations[cx].append(tm.end_time - tm.start_time)

    if cx_durations:
        lines.append("### Average duration by complexity")
        lines.append("")
        lines.append("| Complexity | Tasks measured | Avg duration |")
        lines.append("|------------|---------------|--------------|")
        for cx in sorted(cx_durations, key=lambda v: list(Complexity).index(Complexity(v))):
            durs = cx_durations[cx]
            lines.append(f"| {cx} | {len(durs)} | {_fmt_seconds(sum(durs) / len(durs))} |")
        lines.append("")


def _write_cost_breakdown(lines: list[str], task_metrics: dict[str, TaskMetrics]) -> None:
    """Write the Cost Breakdown section."""
    lines.append("## Cost Breakdown")
    lines.append("")

    model_costs: dict[str, float] = defaultdict(float)
    model_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        m = tm.model or "unknown"
        model_costs[m] += tm.cost_usd
        model_counts[m] += 1

    if model_costs:
        lines.append("### By model")
        lines.append("")
        lines.append("| Model | Tasks | Cost |")
        lines.append("|-------|-------|------|")
        for model in sorted(model_costs, key=lambda m: model_costs[m], reverse=True):
            lines.append(f"| {model} | {model_counts[model]} | ${model_costs[model]:.4f} |")
        lines.append("")

    role_costs: dict[str, float] = defaultdict(float)
    role_task_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        role_costs[tm.role] += tm.cost_usd
        role_task_counts[tm.role] += 1

    if role_costs:
        lines.append("### By role")
        lines.append("")
        lines.append("| Role | Tasks | Cost |")
        lines.append("|------|-------|------|")
        for role in sorted(role_costs, key=lambda r: role_costs[r], reverse=True):
            lines.append(f"| {role} | {role_task_counts[role]} | ${role_costs[role]:.4f} |")
        lines.append("")

    _write_token_breakdown(lines, task_metrics)


def _write_token_breakdown(lines: list[str], task_metrics: dict[str, TaskMetrics]) -> None:
    """Write the token usage sub-section if token data is available."""
    total_prompt = sum(tm.tokens_prompt for tm in task_metrics.values())
    total_completion = sum(tm.tokens_completion for tm in task_metrics.values())
    if total_prompt == 0 and total_completion == 0:
        return

    model_token_data: dict[str, dict[str, int]] = defaultdict(lambda: {"prompt": 0, "completion": 0})
    for tm in task_metrics.values():
        m = tm.model or "unknown"
        model_token_data[m]["prompt"] += tm.tokens_prompt
        model_token_data[m]["completion"] += tm.tokens_completion

    lines.append("### Token usage by model")
    lines.append("")
    lines.append("| Model | Prompt tokens | Completion tokens | Total tokens |")
    lines.append("|-------|--------------|------------------|-------------|")

    def _total(k: str) -> int:
        return model_token_data[k]["prompt"] + model_token_data[k]["completion"]

    for m in sorted(model_token_data, key=_total, reverse=True):
        p, c = model_token_data[m]["prompt"], model_token_data[m]["completion"]
        lines.append(f"| {m} | {p:,} | {c:,} | {p + c:,} |")
    lines.append("")
    total = total_prompt + total_completion
    lines.append(f"**Total tokens:** {total:,} ({total_prompt:,} prompt, {total_completion:,} completion)")
    lines.append("")


def _write_agent_summary(lines: list[str], collector: MetricsCollector) -> None:
    """Write the Agent Summary section."""
    lines.append("## Agent Summary")
    lines.append("")

    agent_metrics = collector._agent_metrics  # type: ignore[reportPrivateUsage]
    if not agent_metrics:
        lines.append("*(No in-memory agent metrics available.)*")
        lines.append("")
        return

    timed_out_or_killed: list[str] = []
    high_failure: list[str] = []

    lines.append("| Agent | Role | Tasks done | Tasks failed | Cost |")
    lines.append("|-------|------|-----------|--------------|------|")
    for am in sorted(agent_metrics.values(), key=lambda a: a.role):
        lines.append(
            f"| {am.agent_id[:8]} | {am.role} | {am.tasks_completed} "
            f"| {am.tasks_failed} | ${am.total_cost_usd:.4f} |"
        )
        if am.tasks_completed == 0 and am.tasks_failed == 0 and am.end_time is not None:
            timed_out_or_killed.append(f"{am.agent_id[:8]} ({am.role})")
        tot = am.tasks_completed + am.tasks_failed
        if tot >= 2 and am.tasks_failed / tot > 0.5:
            high_failure.append(f"{am.agent_id[:8]} ({am.role})")
    lines.append("")

    if timed_out_or_killed:
        lines.append("### Agents that may have been killed or timed out")
        lines.append("")
        for entry in timed_out_or_killed:
            lines.append(f"- {entry}")
        lines.append("")

    if high_failure:
        lines.append("### Agents with high failure rates")
        lines.append("")
        for entry in high_failure:
            lines.append(f"- {entry}")
        lines.append("")


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
    task_metrics: dict[str, TaskMetrics] = collector._task_metrics  # type: ignore[reportPrivateUsage]
    # get_total_cost() sums agent_metrics; when only task_metrics are populated
    # (e.g. bernstein retro reading from archive) fall back to summing task costs.
    total_cost = collector.get_total_cost()
    if abs(total_cost) < 1e-9 and task_metrics:
        total_cost = sum(tm.cost_usd for tm in task_metrics.values())

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

    _write_failure_analysis(lines, done_tasks, failed_tasks)
    _write_performance_section(lines, task_metrics, all_tasks)
    _write_cost_breakdown(lines, task_metrics)
    _write_agent_summary(lines, collector)

    _section("## Recommendations")
    _section("")
    role_failed = _count_by_field(failed_tasks, "role")
    role_done = _count_by_field(done_tasks, "role")
    cx_failed = _count_by_field(failed_tasks, "complexity")
    recommendations = _build_recommendations(
        n_done=n_done, n_failed=n_failed,
        role_failed=role_failed, role_done=role_done,
        cx_failed=cx_failed, total_cost=total_cost, wall_clock_s=wall_clock_s,
    )
    if recommendations:
        for rec in recommendations:
            _section(f"- {rec}")
    else:
        _section("- No issues detected; run looks healthy.")
    _section("")

    retro_path.write_text("\n".join(lines))
    logger.info("Retrospective written to .sdd/runtime/retrospective.md")

    sdd_dir = runtime_dir.parent
    goal = all_tasks[0].title if all_tasks else "Unknown goal"
    run_id = time.strftime("%Y%m%d-%H%M%S")
    append_to_project_memory(
        sdd_dir=sdd_dir, run_id=run_id, goal=goal,
        tasks_done=n_done, tasks_failed=n_failed,
        cost_usd=total_cost, lesson="",
    )


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
        recs.append("Run exceeded 2 hours — consider parallelising independent tasks or increasing max_agents.")

    return recs


def append_to_project_memory(
    *,
    sdd_dir: Path,
    run_id: str,
    goal: str,
    tasks_done: int,
    tasks_failed: int,
    cost_usd: float,
    lesson: str = "",
) -> None:
    """Append a run summary to the cross-run project memory.

    Maintains a JSON file of the last 20 run outcomes. Each run summary includes
    the run ID, goal, task completion counts, cost, and any lessons learned.

    Args:
        sdd_dir: Path to .sdd directory.
        run_id: Unique identifier for the run (e.g., "20260329-120000").
        goal: High-level goal for the run.
        tasks_done: Number of tasks completed.
        tasks_failed: Number of tasks that failed.
        cost_usd: Total cost in USD for the run.
        lesson: Optional lesson or note from the run.
    """
    sdd_dir = Path(sdd_dir)
    memory_dir = sdd_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memory_dir / "project_memory.json"

    # Load existing entries or start fresh
    entries: list[dict[str, object]] = []
    if memory_file.exists():
        try:
            raw = json.loads(memory_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                entries = cast("list[dict[str, object]]", raw)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read project memory, starting fresh")

    # Append new entry
    entry: dict[str, object] = {
        "run_id": run_id,
        "goal": goal,
        "tasks_done": tasks_done,
        "tasks_failed": tasks_failed,
        "cost_usd": cost_usd,
        "lesson": lesson,
        "timestamp": time.time(),
    }
    entries.append(entry)

    # Keep only last 20 entries
    if len(entries) > 20:
        entries = entries[-20:]

    # Write back
    try:
        memory_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error(f"Failed to write project memory: {e}")


def get_recent_project_memory_from_json(
    sdd_dir: Path,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Retrieve recent run summaries from the project memory JSON file.

    Reads entries written by :func:`append_to_project_memory`.

    Args:
        sdd_dir: Path to .sdd directory.
        limit: Maximum number of recent entries to return.

    Returns:
        List of run summary dicts, most recent last.
    """
    memory_file = Path(sdd_dir) / "memory" / "project_memory.json"
    if not memory_file.exists():
        return []

    try:
        raw = json.loads(memory_file.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            entries = cast("list[dict[str, object]]", raw)
            return entries[-limit:]
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read project memory")

    return []


def gather_project_memory_from_json(sdd_dir: Path) -> str:
    """Build a formatted summary of recent run history for context injection.

    Reads from the JSON project memory file written by
    :func:`append_to_project_memory`.

    Args:
        sdd_dir: Path to .sdd directory.

    Returns:
        Formatted memory string or empty string if no memory found.
    """
    items = get_recent_project_memory_from_json(sdd_dir, limit=5)
    if not items:
        return ""

    lines = ["## Recent run history"]
    for item in items:
        goal = item.get("goal", "")
        done = item.get("tasks_done", 0)
        failed = item.get("tasks_failed", 0)
        total = int(done) + int(failed)  # type: ignore[arg-type]
        cost = item.get("cost_usd", 0.0)
        lesson = item.get("lesson", "")
        lines.append(f"- **{goal}**: {done}/{total} done, ${cost:.2f}")
        if lesson:
            lines.append(f"  Lesson: {lesson}")

    return "\n".join(lines)
