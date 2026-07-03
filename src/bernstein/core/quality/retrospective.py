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

from bernstein.core.models import Complexity, Task, TaskStatus

if TYPE_CHECKING:
    from bernstein.core.metrics import MetricsCollector, TaskMetrics

logger = logging.getLogger(__name__)


def _read_persisted_task_costs(runtime_dir: Path) -> dict[str, float]:
    """Read per-task cost_usd from the durable ``.sdd/metrics/tasks.jsonl`` sidecar.

    This file is appended to by the evolution aggregator's
    ``record_task_completion()`` call, which fires from BOTH the normal
    janitor-verified completion path AND the orphan/auto-completed-after-
    death path (agent_lifecycle.py ``handle_orphaned_task`` ->
    ``orch._evolution.record_task_completion(...)``). It is therefore a
    more durable record of "what cost actually happened" than the
    in-memory ``MetricsCollector`` passed into this function: the collector
    reflects only what has been folded in by the moment THIS call fires,
    while the jsonl file reflects everything persisted to disk up to now
    regardless of whether this retrospective call runs in the same process
    tick as the fold-in, a later tick, or (in principle) a separate process
    reading .sdd/ off disk.

    Ground truth (2026-07-03, D2 minimax attempt-e938bd33): a mid-run
    retrospective fired at 01:55:26 and correctly reported $0.0174 from 3
    task rows -- that was an accurate snapshot AT THAT MOMENT. Two more
    orphan-cost fold-ins landed at 01:56:21 and 01:57:15 (+$0.009627,
    +$0.010522), but the run's "shutdown-final" regeneration never fired
    again afterward (that gap is an orchestrator.py shutdown-sequencing
    issue, out of this module's ownership), so the stale $0.0174 INTERIM
    retrospective was the last one ever written even though
    .sdd/metrics/tasks.jsonl on disk had the complete $0.0375 across 5
    rows. Cross-checking against the persisted file here means that ANY
    future retrospective generation -- whenever it happens to fire --
    reports the fullest picture available on disk at that instant, rather
    than being limited to whatever the in-memory collector had folded in
    by that exact tick.

    Args:
        runtime_dir: The ``.sdd/runtime`` directory passed to
            :func:`generate_retrospective`. The metrics sidecar lives at
            the sibling path ``.sdd/metrics/tasks.jsonl``.

    Returns:
        Mapping of task_id -> most-recently-written cost_usd for that
        task_id (last write wins, so a retried task's final recorded cost
        is used rather than double-counting each attempt).
    """
    metrics_path = runtime_dir.parent / "metrics" / "tasks.jsonl"
    costs: dict[str, float] = {}
    if not metrics_path.exists():
        logger.debug(
            "cost_aggregation: persisted sidecar not found at %s -- skipping durable cross-check",
            metrics_path,
        )
        return costs
    try:
        with metrics_path.open() as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "cost_aggregation: skipping malformed line %d in %s: %s",
                        line_no,
                        metrics_path,
                        exc,
                    )
                    continue
                task_id = record.get("task_id")
                if not task_id:
                    continue
                cost_usd = record.get("cost_usd")
                if cost_usd is None:
                    continue
                costs[task_id] = float(cost_usd)  # last write wins (final retry outcome)
    except OSError as exc:
        logger.warning("cost_aggregation: failed to read %s: %s", metrics_path, exc)
        return {}
    return costs


def _count_by_field(tasks: list[Task], field: str) -> dict[str, int]:
    """Count tasks grouped by a given field name."""
    counts: dict[str, int] = defaultdict(int)
    for t in tasks:
        val = getattr(t, field)
        key = val.value if hasattr(val, "value") else str(val)
        counts[key] += 1
    return counts


# ---------------------------------------------------------------------------
# Run-health honesty
# ---------------------------------------------------------------------------
#
# Bug: a run could be reported "healthy" (completion_rate high, 0
# recommendations, "No issues detected; run looks healthy.") even when
# almost every task's terminal state was FORCED by watchdog/timeout/janitor
# machinery rather than an agent genuinely finishing its own work. Ground
# truth: 2026-07-02 run9-attempt9 - 21 spawns / 19 merge refusals / 746
# SHUTDOWN(no_heartbeat) signals in 40 minutes, driven by a heartbeat-dir
# bug (see work/agent-reports/2026-07-02-run9-attempt9-audit.md) - and
# nothing in the retrospective would have surfaced this as unhealthy.
#
# There is NO dedicated "who/what terminated this task" field on ``Task``
# today. ``Task.terminal_reason`` (bernstein/core/tasks/models.py:422) is a
# narrow categorical field written only for a handful of agent-reported
# outcomes (see the match statement in
# bernstein/core/tasks/task_lifecycle.py:342, values like
# "error_max_turns" / "error_max_budget_usd" / "model_error" /
# "blocking_limit") - it is never populated for orchestrator-forced kills.
#
# The actual forcing-reason text for orchestrator-forced kills DOES exist,
# but only as free-text written into ``Task.result_summary`` at the call
# sites that force a termination:
#   - watchdog/heartbeat kill: "Agent {id} reaped (heartbeat timeout)"
#     (bernstein/core/agents/agent_lifecycle.py:_reap_heartbeat_timeout,
#     ~line 1567)
#   - janitor rejection: "Agent {id} died; janitor failed: {signals}"
#     (bernstein/core/agents/agent_lifecycle.py:handle_orphaned_task,
#     ~line 1208)
#   - retry-budget exhaustion / DLQ: "Max retries exceeded: {reason}"
#     (bernstein/core/tasks/task_lifecycle.py:retry_or_fail_task, ~line 819)
#   - orphaned-with-no-signal death: "Agent {id} died; no completion
#     signals and no files modified" (agent_lifecycle.py:_handle_orphan_no_signals)
#   - auto-completion inferred (not agent-reported) after the agent process
#     already died: "Auto-completed ..." / "... after agent {id} died"
#     (agent_lifecycle.py:handle_orphaned_task /
#     _handle_orphan_no_signals)
#
# Rather than inventing a new field that every one of those call sites
# would need to start writing (out of scope for this fix, and those call
# sites are owned by other concurrently-edited branches per the wave plan
# collision rules), this module derives a best-effort terminator category
# by keyword-matching the EXISTING ``result_summary`` / ``terminal_reason``
# text. This is a proxy, not ground truth: if the free-text wording at any
# of the call sites above changes, these markers must be updated too.
_WATCHDOG_MARKERS: tuple[str, ...] = ("heartbeat timeout", "reaped (heartbeat", "reaped stale agent")
_JANITOR_MARKERS: tuple[str, ...] = ("janitor failed", "janitor rejected", "janitor:")
_TIMEOUT_MARKERS: tuple[str, ...] = ("wall-clock", "exceeded timeout", "timeout)", "max turns", "max_turns")
_AUTO_COMPLETED_MARKERS: tuple[str, ...] = ("auto-completed", "after agent")
_FORCED_OTHER_MARKERS: tuple[str, ...] = (
    "max retries exceeded",
    "died; no completion signals",
    "died; escalating",
    "sibling_aborted",
    "parent_aborted",
    "shutdown_signal",
    "kill_requested",
)

# Categories, in the priority order they are matched (most-specific first).
TERMINATOR_WATCHDOG_KILLED = "watchdog_killed"
TERMINATOR_JANITOR_REJECTED = "janitor_rejected"
TERMINATOR_TIMEOUT_KILLED = "timeout_killed"
TERMINATOR_OTHER_FORCED = "other_forced"
TERMINATOR_AUTO_COMPLETED_AFTER_DEATH = "auto_completed_after_death"
TERMINATOR_AGENT_COMPLETED = "agent_completed"
# A task whose terminal status is FAILED but whose result_summary/
# terminal_reason text did not match any of the forced-kill marker sets
# above (e.g. a provider error surfaced directly, a non-retryable API
# failure). Bug this closes: such a task used to fall through to the
# TERMINATOR_AGENT_COMPLETED default -- a FAILED task counted as a genuine
# completion -- which let compute_run_health's non-agent-fraction check
# miss it entirely. Ground truth: 2026-07-02 D2 openrouter leg
# (work/bernstein/proofs/d2/openrouter/KILL-NOTE.md) -- 1/1 tasks failed
# (deepseek max_tokens context-length BadRequestError, no forced-kill
# keyword anywhere in the text) and the retrospective still printed
# "Verdict: HEALTHY" / "Agent-completed: 1".
TERMINATOR_AGENT_REPORTED_FAILURE = "agent_reported_failure"

# Categories that do NOT represent a genuine agent-reported outcome. A run
# where most terminations fall in this set is not "healthy" no matter how
# high the raw completion_rate looks.
_NON_AGENT_CATEGORIES: frozenset[str] = frozenset(
    {
        TERMINATOR_WATCHDOG_KILLED,
        TERMINATOR_JANITOR_REJECTED,
        TERMINATOR_TIMEOUT_KILLED,
        TERMINATOR_OTHER_FORCED,
        TERMINATOR_AUTO_COMPLETED_AFTER_DEATH,
        TERMINATOR_AGENT_REPORTED_FAILURE,
    }
)

# A run is UNHEALTHY when more than this fraction of task terminations were
# non-agent-caused (watchdog/timeout/janitor/other-forced/auto-completed).
_UNHEALTHY_NON_AGENT_FRACTION = 0.5


def classify_task_terminator(task: Task) -> str:
    """Classify how a task's terminal state was reached.

    Best-effort proxy derived from ``task.result_summary`` /
    ``task.terminal_reason`` free text - see the module-level comment
    above for exactly what is and is not tracked today. Returns one of
    the ``TERMINATOR_*`` constants.
    """
    text = f"{task.result_summary or ''} {task.terminal_reason or ''}".lower()
    if any(m in text for m in _WATCHDOG_MARKERS):
        return TERMINATOR_WATCHDOG_KILLED
    if any(m in text for m in _JANITOR_MARKERS):
        return TERMINATOR_JANITOR_REJECTED
    if any(m in text for m in _TIMEOUT_MARKERS):
        return TERMINATOR_TIMEOUT_KILLED
    if any(m in text for m in _FORCED_OTHER_MARKERS):
        return TERMINATOR_OTHER_FORCED
    if any(m in text for m in _AUTO_COMPLETED_MARKERS):
        # Read-side confirmation for the orphan_auto_complete WARNING logged
        # by agent_lifecycle.py's _try_auto_complete/handle_orphaned_task at
        # write time: this classification is derived entirely from
        # task.result_summary text those call sites write (e.g.
        # "Auto-completed: agent {id} ..." / "Auto-completed after agent
        # {id} died; janitor passed"). If this log line's task_id never has
        # a matching orphan_auto_complete WARNING in the same run, the two
        # ends have drifted out of sync.
        matched = next((m for m in _AUTO_COMPLETED_MARKERS if m in text), None)
        logger.debug(
            "classify_task_terminator: task_id=%s -> auto_completed_after_death (matched_marker=%r, result_summary=%r)",
            task.id,
            matched,
            task.result_summary,
        )
        return TERMINATOR_AUTO_COMPLETED_AFTER_DEATH
    if task.status == TaskStatus.FAILED:
        return TERMINATOR_AGENT_REPORTED_FAILURE
    return TERMINATOR_AGENT_COMPLETED


def compute_run_health(all_tasks: list[Task], n_unresolved: int = 0) -> tuple[bool, dict[str, int]]:
    """Compute the run-health verdict and per-terminator-category counts.

    Args:
        all_tasks: Every task considered for this run's retrospective
            (done + failed).
        n_unresolved: Count of tasks the metrics collector saw started but
            that never reconciled to either ``all_tasks`` or a
            ``collector.complete_task()`` call (see
            ``generate_retrospective``'s reconciliation block). Treated as
            additional non-agent-verified terminations for health purposes.

    Returns:
        Tuple of ``(healthy, counts)`` where ``counts`` maps each
        ``TERMINATOR_*`` category to the number of tasks in it.

        Hard rule (bug fix): ``healthy`` is ``False`` whenever ANY task
        has status FAILED, or ``n_unresolved > 0``, or there is at least
        one ``TERMINATOR_AUTO_COMPLETED_AFTER_DEATH`` -- regardless of the
        text-pattern classification below. This does not depend on
        ``classify_task_terminator`` finding a forced-kill marker in the
        task's free text, because that classification is a best-effort
        proxy that can miss real failures (e.g. a provider error with no
        forced-kill keyword -- see TERMINATOR_AGENT_REPORTED_FAILURE).
        Absent any hard-rule trigger, ``healthy`` is ``False`` when more
        than ``_UNHEALTHY_NON_AGENT_FRACTION`` of terminations were
        non-agent-caused (watchdog/timeout/janitor/other-forced/
        auto-completed-after-death/agent-reported-failure). An empty task
        list with no unresolved tasks is vacuously healthy.
    """
    counts: dict[str, int] = defaultdict(int)
    for t in all_tasks:
        counts[classify_task_terminator(t)] += 1

    total = len(all_tasks) + n_unresolved
    if total == 0:
        return True, dict(counts)

    n_failed = sum(1 for t in all_tasks if t.status == TaskStatus.FAILED)
    auto_completed = counts.get(TERMINATOR_AUTO_COMPLETED_AFTER_DEATH, 0)
    if n_failed > 0 or auto_completed > 0 or n_unresolved > 0:
        return False, dict(counts)

    non_agent = sum(counts.get(cat, 0) for cat in _NON_AGENT_CATEGORIES)
    healthy = (non_agent / total) <= _UNHEALTHY_NON_AGENT_FRACTION
    return healthy, dict(counts)


def _write_run_health_section(
    lines: list[str],
    all_tasks: list[Task],
    n_unresolved: int = 0,
) -> tuple[bool, dict[str, int]]:
    """Write the Run Health section and return (healthy, counts) for reuse in Recommendations."""
    healthy, counts = compute_run_health(all_tasks, n_unresolved=n_unresolved)
    total = len(all_tasks) + n_unresolved
    agent_completed = counts.get(TERMINATOR_AGENT_COMPLETED, 0)
    watchdog_killed = counts.get(TERMINATOR_WATCHDOG_KILLED, 0)
    janitor_rejected = counts.get(TERMINATOR_JANITOR_REJECTED, 0)
    timeout_killed = counts.get(TERMINATOR_TIMEOUT_KILLED, 0)
    other_forced = counts.get(TERMINATOR_OTHER_FORCED, 0)
    auto_completed = counts.get(TERMINATOR_AUTO_COMPLETED_AFTER_DEATH, 0)
    agent_reported_failure = counts.get(TERMINATOR_AGENT_REPORTED_FAILURE, 0)

    verdict = "HEALTHY" if healthy else "UNHEALTHY"
    logger.info(
        "run health: %d/%d agent-completed, %d watchdog-killed, %d janitor-rejected, "
        "%d timeout-killed, %d other-forced, %d auto-completed-after-death, "
        "%d agent-reported-failure, %d unresolved-in-metrics -> %s",
        agent_completed,
        total,
        watchdog_killed,
        janitor_rejected,
        timeout_killed,
        other_forced,
        auto_completed,
        agent_reported_failure,
        n_unresolved,
        verdict,
    )

    lines.extend(("## Run Health", ""))
    lines.append(f"- **Verdict:** {verdict}")
    lines.extend(
        (
            "",
            "| Terminator category | Count |",
            "|----------------------|-------|",
            f"| Agent-completed | {agent_completed} |",
            f"| Agent-reported failure | {agent_reported_failure} |",
            f"| Watchdog-killed | {watchdog_killed} |",
            f"| Janitor-rejected | {janitor_rejected} |",
            f"| Timeout-killed | {timeout_killed} |",
            f"| Auto-completed after agent death | {auto_completed} |",
            f"| Other forced termination | {other_forced} |",
            f"| Unresolved in metrics (started, outcome never reconciled) | {n_unresolved} |",
            "",
        )
    )
    if not healthy:
        lines.append(
            f"- **Warning:** {total - agent_completed}/{total} task terminations were NOT genuine "
            "agent completions (failed/watchdog/timeout/janitor/other-forced/auto-completed/"
            "unresolved) - the completion rate above does not reflect real progress. Investigate "
            "the dominant non-agent category before trusting this run's outcome."
        )
        lines.append("")

    return healthy, counts


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
    lines.extend((header, "", columns, separator))
    for key in all_keys:
        d = done_counts.get(key, 0)
        f = failed_counts.get(key, 0)
        tot = d + f
        rate = f / tot * 100 if tot else 0.0
        lines.append(f"| {key} | {d} | {f} | {tot} | {rate:.0f}% |")
    lines.append("")


def _write_failure_analysis(lines: list[str], done_tasks: list[Task], failed_tasks: list[Task]) -> None:
    """Write the Failure Analysis section."""
    lines.extend(("## Failure Analysis", ""))

    role_done = _count_by_field(done_tasks, "role")
    role_failed = _count_by_field(failed_tasks, "role")
    _write_rate_table(
        lines,
        "### By role",
        "| Role | Done | Failed | Total | Failure rate |",
        "|------|------|--------|-------|--------------|",
        role_done,
        role_failed,
    )

    cx_done = _count_by_field(done_tasks, "complexity")
    cx_failed = _count_by_field(failed_tasks, "complexity")
    _write_rate_table(
        lines,
        "### By complexity",
        "| Complexity | Done | Failed | Total | Failure rate |",
        "|------------|------|--------|-------|--------------|",
        cx_done,
        cx_failed,
        sort_key=lambda v: list(Complexity).index(Complexity(v)),
    )

    if failed_tasks:
        lines.extend(("### Failed task titles", ""))
        for t in sorted(failed_tasks, key=lambda t: t.title):
            lines.append(f"- {t.title} *(role: {t.role}, complexity: {t.complexity.value})*")
        lines.append("")


def _write_performance_section(
    lines: list[str],
    task_metrics: dict[str, TaskMetrics],
    all_tasks: list[Task],
) -> None:
    """Write the Performance section."""
    lines.extend(("## Performance", ""))

    role_durations: dict[str, list[float]] = defaultdict(list)
    for tm in task_metrics.values():
        if tm.end_time is not None:
            role_durations[tm.role].append(tm.end_time - tm.start_time)

    if role_durations:
        lines.extend(
            (
                "### Average duration by role",
                "",
                "| Role | Tasks measured | Avg duration |",
                "|------|---------------|--------------|",
            )
        )
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
        lines.extend(
            (
                "### Average duration by complexity",
                "",
                "| Complexity | Tasks measured | Avg duration |",
                "|------------|---------------|--------------|",
            )
        )
        for cx in sorted(cx_durations, key=lambda v: list(Complexity).index(Complexity(v))):
            durs = cx_durations[cx]
            lines.append(f"| {cx} | {len(durs)} | {_fmt_seconds(sum(durs) / len(durs))} |")
        lines.append("")


def _write_cost_breakdown(lines: list[str], task_metrics: dict[str, TaskMetrics]) -> None:
    """Write the Cost Breakdown section."""
    lines.extend(("## Cost Breakdown", ""))

    model_costs: dict[str, float] = defaultdict(float)
    model_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        m = tm.model or "unknown"
        model_costs[m] += tm.cost_usd
        model_counts[m] += 1

    if model_costs:
        lines.extend(("### By model", "", "| Model | Tasks | Cost |", "|-------|-------|------|"))
        for model in sorted(model_costs, key=lambda m: model_costs[m], reverse=True):
            lines.append(f"| {model} | {model_counts[model]} | ${model_costs[model]:.4f} |")
        lines.append("")

    role_costs: dict[str, float] = defaultdict(float)
    role_task_counts: dict[str, int] = defaultdict(int)
    for tm in task_metrics.values():
        role_costs[tm.role] += tm.cost_usd
        role_task_counts[tm.role] += 1

    if role_costs:
        lines.extend(("### By role", "", "| Role | Tasks | Cost |", "|------|-------|------|"))
        for role in sorted(role_costs, key=lambda r: role_costs[r], reverse=True):
            lines.append(f"| {role} | {role_task_counts[role]} | ${role_costs[role]:.4f} |")
        lines.append("")

    _write_token_breakdown(lines, task_metrics)


def _write_token_breakdown(lines: list[str], task_metrics: dict[str, TaskMetrics]) -> None:
    """Write the token usage sub-section if token data is available."""
    total_prompt = sum(tm.tokens_prompt for tm in task_metrics.values())
    total_completion = sum(tm.tokens_completion for tm in task_metrics.values())
    if total_prompt == total_completion == 0:
        return

    model_token_data: dict[str, dict[str, int]] = defaultdict(lambda: {"prompt": 0, "completion": 0})
    for tm in task_metrics.values():
        m = tm.model or "unknown"
        model_token_data[m]["prompt"] += tm.tokens_prompt
        model_token_data[m]["completion"] += tm.tokens_completion

    lines.extend(
        (
            "### Token usage by model",
            "",
            "| Model | Prompt tokens | Completion tokens | Total tokens |",
            "|-------|--------------|------------------|-------------|",
        )
    )

    def _total(k: str) -> int:
        return model_token_data[k]["prompt"] + model_token_data[k]["completion"]

    for m in sorted(model_token_data, key=_total, reverse=True):
        p, c = model_token_data[m]["prompt"], model_token_data[m]["completion"]
        lines.append(f"| {m} | {p:,} | {c:,} | {p + c:,} |")
    lines.append("")
    total = total_prompt + total_completion
    lines.extend((f"**Total tokens:** {total:,} ({total_prompt:,} prompt, {total_completion:,} completion)", ""))


def _write_agent_summary(lines: list[str], collector: MetricsCollector) -> None:
    """Write the Agent Summary section."""
    lines.extend(("## Agent Summary", ""))

    agent_metrics = collector._agent_metrics  # type: ignore[reportPrivateUsage]
    if not agent_metrics:
        lines.extend(("*(No in-memory agent metrics available.)*", ""))
        return

    timed_out_or_killed: list[str] = []
    high_failure: list[str] = []

    lines.extend(
        ("| Agent | Role | Tasks done | Tasks failed | Cost |", "|-------|------|-----------|--------------|------|")
    )
    for am in sorted(agent_metrics.values(), key=lambda a: a.role):
        lines.append(
            f"| {am.agent_id[:8]} | {am.role} | {am.tasks_completed} | {am.tasks_failed} | ${am.total_cost_usd:.4f} |"
        )
        if am.tasks_completed == am.tasks_failed == 0 and am.end_time is not None:
            timed_out_or_killed.append(f"{am.agent_id[:8]} ({am.role})")
        tot = am.tasks_completed + am.tasks_failed
        if tot >= 2 and am.tasks_failed / tot > 0.5:
            high_failure.append(f"{am.agent_id[:8]} ({am.role})")
    lines.append("")

    if timed_out_or_killed:
        lines.extend(("### Agents that may have been killed or timed out", ""))
        for entry in timed_out_or_killed:
            lines.append(f"- {entry}")
        lines.append("")

    if high_failure:
        lines.extend(("### Agents with high failure rates", ""))
        for entry in high_failure:
            lines.append(f"- {entry}")
        lines.append("")


def generate_retrospective(
    done_tasks: list[Task],
    failed_tasks: list[Task],
    collector: MetricsCollector,
    runtime_dir: Path,
    run_start_ts: float,
    *,
    trigger_reason: str = "shutdown-final",
    full_status_counts: dict[str, int] | None = None,
) -> None:
    """Write a run retrospective to .sdd/runtime/retrospective.md.

    Analyses task completion rates, duration by role/complexity, cost
    breakdown by model/role, agent failure patterns, and produces
    actionable recommendations.

    IMPORTANT (A5 stale-retrospective fix): this function unconditionally
    OVERWRITES any prior retrospective.md. Callers that invoke this from a
    tick-level "queue looks empty" heuristic rather than true orchestrator
    shutdown MUST pass ``trigger_reason="mid-run"`` so the report is
    labeled INTERIM and is never mistaken for the final verdict. Ground
    truth: a canary run's retrospective reported "100% completion,
    HEALTHY" at T+58s (right after the manager task alone finished), and
    2 of the run's 3 total tasks subsequently failed (janitor-rejected)
    without the report ever being regenerated - the run health honesty
    logic above (``compute_run_health`` / ``TERMINATOR_*``) is correct but
    was never re-run against the final task list. Only the true
    orchestrator-shutdown call site should use the default
    ``"shutdown-final"``, and it must always pass the FINAL done/failed
    task lists.

    Args:
        done_tasks: Tasks with status 'done'.
        failed_tasks: Tasks with status 'failed'.
        collector: Live MetricsCollector instance for in-memory metrics.
        runtime_dir: Directory where retrospective.md is written.
        run_start_ts: Unix timestamp when the run started.
        trigger_reason: Why this generation fired - "mid-run" for a
            tick-level heuristic that may not reflect the final run state,
            or "shutdown-final" (default) for the true end-of-run call.
        full_status_counts: Optional full task-state histogram (including
            non-terminal statuses like "open"/"claimed") for the log line.
            Falls back to a done/failed/unresolved-only histogram when
            not provided.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    retro_path = runtime_dir / "retrospective.md"

    all_tasks = done_tasks + failed_tasks
    wall_clock_s = time.time() - run_start_ts
    task_metrics: dict[str, TaskMetrics] = collector._task_metrics  # type: ignore[reportPrivateUsage]
    agent_metrics = collector._agent_metrics  # type: ignore[reportPrivateUsage]

    # ------------------------------------------------------------------
    # Ground-truth reconciliation against collector.task_metrics
    # ------------------------------------------------------------------
    #
    # Bug: done_tasks/failed_tasks are handed to this function by the
    # orchestrator's caller, which can be stale. Per-tick, the orchestrator
    # fetches a `tasks_by_status` snapshot once at the START of the tick
    # (orchestrator.py:1120, `fetch_all_tasks`) and only refreshes it via
    # local mutation; but reap_dead_agents / handle_orphaned_task
    # (agent_lifecycle.py, steps 5/5b run LATER in the SAME tick) persist
    # task outcomes straight to the task server over HTTP and never write
    # back into that same snapshot dict. `_generate_run_summary` is then
    # called with the now-stale snapshot at orchestrator.py:1691 -- so
    # tasks that failed or auto-completed during this tick's own
    # lifecycle-management steps are invisible to done_tasks/failed_tasks.
    # Ground truth: 2026-07-02 D2 claude/sdd-snapshot (attempt 2) -- 4
    # failed manager attempts and a CLI final tally of "Failed: 4", but
    # done_tasks/failed_tasks arrived here empty (0 done / 0 total).
    # Diagnosed but NOT fixed here (out of this module's ownership --
    # owned files are src/bernstein/core/quality/retrospective.py only);
    # see the handoff report for the orchestrator.py/agent_lifecycle.py fix.
    #
    # `collector.task_metrics` is populated by `collector.start_task()`
    # at agent-spawn time (task_lifecycle.py:1976) independently of the
    # tasks_by_status snapshot above, so it is a second, less-stale source
    # of "this task_id existed and something happened to it". However,
    # `collector.complete_task()` is only called from the janitor-verified
    # normal-completion path (task_lifecycle.py:2656) and from a couple of
    # narrow batch/fast-path call sites -- NOT from the retry/fail path
    # (task_lifecycle.py:retry_or_fail_task) or the orphan / auto-complete-
    # after-death path (agent_lifecycle.py:handle_orphaned_task, ~line
    # 1394, which posts completion to the task server and to a *separate*
    # `.sdd/metrics/*.jsonl` MetricsRecord file via emit_orphan_metrics(),
    # bypassing the collector entirely). So a task that failed via retry
    # exhaustion, or was auto-completed after its agent died, shows up in
    # `collector.task_metrics` as PERMANENTLY "started, never finished"
    # (`end_time is None`, `success` stuck at its `False` default) -- and
    # if it also never lands in done_tasks/failed_tasks (the staleness bug
    # above), it disappears from the retrospective entirely. Ground truth:
    # 2026-07-02 D2 claude/attempt1-tools-zero -- a genuine auto-completed-
    # after-death event and real cost $0.031996, retrospective said
    # "HEALTHY" / "$0.0000" / "Auto-completed after agent death: 0".
    #
    # Treat any such "unresolved" task_metrics entry not already accounted
    # for in all_tasks as a suspect/failed outcome for retrospective
    # purposes: we cannot prove it succeeded, so counting it as healthy-by-
    # omission is exactly the dishonesty this module exists to prevent.
    _known_task_ids = {t.id for t in all_tasks}
    unresolved_task_ids = sorted(
        tid for tid, tm in task_metrics.items() if tid not in _known_task_ids and tm.end_time is None
    )
    n_unresolved = len(unresolved_task_ids)
    if unresolved_task_ids:
        logger.warning(
            "retrospective: %d task(s) present in collector.task_metrics (started via "
            "start_task()) but absent from both done_tasks and failed_tasks AND never "
            "reached collector.complete_task() (end_time is None) -- ids=%s. Counting "
            "these as unresolved/failed for retrospective honesty rather than silently "
            "dropping them (see reconciliation comment in generate_retrospective).",
            n_unresolved,
            unresolved_task_ids,
        )

    n_done = len(done_tasks)
    n_failed = len(failed_tasks) + n_unresolved
    total = len(all_tasks) + n_unresolved
    completion_rate = (n_done / total * 100) if total else 0.0

    _status_histogram = full_status_counts if full_status_counts is not None else {"done": n_done, "failed": n_failed}
    logger.info(
        "Generating retrospective (trigger=%s): status_histogram=%s",
        trigger_reason,
        _status_histogram,
    )

    # get_total_cost() sums agent_metrics; when only task_metrics are populated
    # (e.g. bernstein retro reading from archive) fall back to summing task costs.
    #
    # Every cost record consumed here is logged so a case where a real
    # per-runner cost (e.g. $0.031996 in a runner log) vanishes into a
    # $0.0000 summary becomes visible in the retrospective-generation log
    # instead of only in the retrospective.md output (2026-07-02 D2
    # claude/attempt-1: retrospective said "HEALTHY" / cost $0.0000 while
    # the runner log recorded $0.031996 -- the source mismatch was
    # invisible without this).
    _agent_cost_total = sum(am.total_cost_usd for am in agent_metrics.values())
    logger.info(
        "cost_aggregation: source=agent_metrics records_found=%d total=$%.6f",
        len(agent_metrics),
        _agent_cost_total,
    )
    for _am in agent_metrics.values():
        logger.debug(
            "cost_aggregation: record source=agent_metrics agent_id=%s role=%s cost_usd=%.6f",
            _am.agent_id,
            _am.role,
            _am.total_cost_usd,
        )
    total_cost = collector.get_total_cost()
    if abs(total_cost) < 1e-9 and task_metrics:
        _parsed = 0
        _skipped = 0
        _fallback_total = 0.0
        for _task_id, _tm in task_metrics.items():
            if _tm.cost_usd:
                _parsed += 1
                _fallback_total += _tm.cost_usd
                logger.debug(
                    "cost_aggregation: record source=task_metrics task_id=%s role=%s cost_usd=%.6f",
                    _task_id,
                    _tm.role,
                    _tm.cost_usd,
                )
            else:
                _skipped += 1
        total_cost = _fallback_total
        logger.info(
            "cost_aggregation: agent_metrics total was $0.0000, falling back to source=task_metrics: "
            "N=%d records found, M=%d parsed (cost_usd>0), K=%d skipped (reasons=cost_usd==0), "
            "fallback_total=$%.6f",
            len(task_metrics),
            _parsed,
            _skipped,
            _fallback_total,
        )
    elif abs(total_cost) < 1e-9:
        logger.warning(
            "cost_aggregation: agent_metrics total=$0.0000 AND task_metrics is empty (N=0 records) -- "
            "total_cost will report as $0.0000 in the retrospective. If any runner actually incurred "
            "cost this run, this is a wiring gap between whatever recorded that cost and this "
            "MetricsCollector instance."
        )

    # ------------------------------------------------------------------
    # Durable cross-check against .sdd/metrics/tasks.jsonl
    # ------------------------------------------------------------------
    #
    # The in-memory collector (agent_metrics / task_metrics above) only
    # reflects whatever has been folded in by the exact moment this
    # function is called. The persisted tasks.jsonl sidecar is written by
    # the evolution aggregator's record_task_completion() from BOTH the
    # normal completion path and the orphan/auto-completed-after-death
    # path, so it can be strictly more complete than the in-memory view
    # (e.g. a fold-in landed after the last retrospective tick, or this
    # call is racing a fold-in in another tick). Reconcile per-task_id so
    # both the Overview total AND the by-model/by-role cost breakdown
    # (which reads task_metrics directly) reflect the fullest picture.
    _persisted_costs = _read_persisted_task_costs(runtime_dir)
    _reconciled = 0
    _reconciled_total_added = 0.0
    for _task_id, _persisted_cost in _persisted_costs.items():
        _tm = task_metrics.get(_task_id)
        if _tm is not None and abs(_tm.cost_usd) < 1e-9 and _persisted_cost > 0:
            _tm.cost_usd = _persisted_cost
            _reconciled += 1
            _reconciled_total_added += _persisted_cost
            logger.info(
                "cost_aggregation: reconciled task_id=%s cost_usd=%.6f from persisted "
                "source=.sdd/metrics/tasks.jsonl (in-memory collector had $0.0000 for this task)",
                _task_id,
                _persisted_cost,
            )
    if _reconciled:
        total_cost += _reconciled_total_added
    logger.info(
        "retrospective_cost_aggregation: source=%s rows=%d total_usd=%.6f "
        "(persisted_sidecar_rows=%d, reconciled_from_sidecar=%d)",
        "agent_metrics+task_metrics+persisted_sidecar" if _reconciled else "agent_metrics+task_metrics",
        len(task_metrics),
        total_cost,
        len(_persisted_costs),
        _reconciled,
    )
    logger.info("cost_aggregation: final total_cost=$%.6f (used in retrospective.md)", total_cost)

    lines: list[str] = []
    _section = lines.append

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S")
    _section("# Run Retrospective")
    _section("")
    if trigger_reason != "shutdown-final":
        _section(
            f"**INTERIM - run in progress** (trigger: {trigger_reason}). This snapshot was "
            "generated before all tasks reached a terminal state and will be overwritten by "
            "the final retrospective at orchestrator shutdown."
        )
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

    run_healthy, terminator_counts = _write_run_health_section(lines, all_tasks, n_unresolved=n_unresolved)

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
        n_done=n_done,
        n_failed=n_failed,
        role_failed=role_failed,
        role_done=role_done,
        cx_failed=cx_failed,
        total_cost=total_cost,
        wall_clock_s=wall_clock_s,
        run_healthy=run_healthy,
        terminator_counts=terminator_counts,
    )
    if recommendations:
        for rec in recommendations:
            _section(f"- {rec}")
    elif run_healthy:
        _section("- No issues detected; run looks healthy.")
    _section("")

    retro_path.write_text("\n".join(lines))
    logger.info(
        "Retrospective written to .sdd/runtime/retrospective.md (trigger=%s, done=%d, failed=%d, healthy=%s)",
        trigger_reason,
        n_done,
        n_failed,
        run_healthy,
    )

    sdd_dir = runtime_dir.parent
    goal = all_tasks[0].title if all_tasks else "Unknown goal"
    run_id = time.strftime("%Y%m%d-%H%M%S")
    append_to_project_memory(
        sdd_dir=sdd_dir,
        run_id=run_id,
        goal=goal,
        tasks_done=n_done,
        tasks_failed=n_failed,
        cost_usd=total_cost,
        lesson="",
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
    run_healthy: bool = True,
    terminator_counts: dict[str, int] | None = None,
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
        run_healthy: Result of :func:`compute_run_health` - False means most
            task terminations were non-agent-caused (watchdog/timeout/
            janitor/other-forced/auto-completed).
        terminator_counts: Per-category counts from :func:`compute_run_health`,
            used to name the dominant non-agent cause in the recommendation.

    Returns:
        List of recommendation strings (may be empty).
    """
    recs: list[str] = []

    total = n_done + n_failed
    if total == 0:
        return recs

    if not run_healthy:
        counts = terminator_counts or {}
        non_agent = {k: v for k, v in counts.items() if k != TERMINATOR_AGENT_COMPLETED and v > 0}
        dominant = max(non_agent, key=lambda k: non_agent[k]) if non_agent else "unknown"
        recs.append(
            f"UNHEALTHY: most task terminations were non-agent-caused (dominant cause: "
            f"{dominant}, see Run Health table) - do not trust the completion rate; "
            "diagnose the forcing mechanism (watchdog/timeout/janitor) before re-running."
        )

    overall_fail_rate = n_failed / total
    if overall_fail_rate >= 0.5:
        recs.append(
            f"Overall failure rate is {overall_fail_rate:.0%} - review task definitions "
            "and agent prompts before the next run."
        )

    # Per-role recommendations
    for role in sorted(set(role_failed) | set(role_done)):
        f = role_failed.get(role, 0)
        d = role_done.get(role, 0)
        tot = f + d
        if tot >= 2 and f / tot >= 0.5:
            recs.append(
                f"Role '{role}' has a {f / tot:.0%} failure rate ({f}/{tot}) - "
                "review role prompts and task descriptions."
            )

    # Per-complexity recommendations
    for cx in sorted(cx_failed):
        f = cx_failed[cx]
        # We don't have cx_done here - just flag high absolute failure counts
        if f >= 3:
            recs.append(
                f"Complexity '{cx}' had {f} failures - consider breaking these tasks "
                "into smaller pieces or raising estimated_minutes."
            )

    # Cost warnings
    if total_cost > 5.0:
        recs.append(
            f"Cost ${total_cost:.2f} is high - review model selection; consider "
            "routing more tasks to haiku or free-tier providers."
        )

    # Duration warnings (> 2 hours)
    if wall_clock_s > 7200:
        recs.append("Run exceeded 2 hours - consider parallelising independent tasks or increasing max_agents.")

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
