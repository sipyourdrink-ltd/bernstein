"""Run-summary rendering for archived plans.

The lifecycle module prepends a ``## Run summary`` (or ``## Failure
reason``) Markdown block to plan files when they move into the
``completed/`` or ``blocked/`` archive bucket.  This module provides
the typed inputs and deterministic Markdown renderer used by both
buckets.

The rendered block is intentionally text-only Markdown, but it is
wrapped in HTML-style ``<!-- ... -->`` markers so the surrounding
file remains a valid YAML document end-to-end.  ``yaml.safe_load``
ignores HTML-comment-prefixed input only when stripped first, so the
existing plan loader is the canonical reader; users copying an
archived plan back into ``active/`` need only delete the comment
block to restore the original plan body.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "FailureSummary",
    "GateResult",
    "ModelCost",
    "RunSummary",
    "TaskCounts",
    "render_failure_block",
    "render_summary_block",
]


@dataclass(frozen=True, slots=True)
class GateResult:
    """One janitor gate's pass/fail outcome.

    Attributes:
        name: Gate name (e.g. ``"tests"``, ``"lint"``).
        passed: True if the gate passed.
        detail: Optional short description shown in the rendered table.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Per-model spend breakdown.

    Attributes:
        model: Model identifier.
        spend_usd: Cumulative USD spent on this model during the run.
    """

    model: str
    spend_usd: float


@dataclass(frozen=True, slots=True)
class TaskCounts:
    """Final task tallies for the archived run.

    Attributes:
        completed: Tasks that finished successfully.
        failed: Tasks that terminally failed.
        skipped: Tasks that were skipped (e.g. dependency abort).
    """

    completed: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Inputs for the success-path ``## Run summary`` block.

    The renderer fills in placeholders (``"n/a"``) for empty values so
    the four canonical subsections are always present.

    Attributes:
        pr_url: URL of the pull request opened by ``bernstein pr``.
        gate_results: Janitor gate outcomes (one row per gate).
        model_costs: Per-model spend rows.
        wall_clock_seconds: Real time from run start to archival.
        agent_time_seconds: Sum of agent-side wall time across all
            sessions.
        tasks: Final task counts.
    """

    pr_url: str = ""
    gate_results: list[GateResult] = field(default_factory=list[GateResult])
    model_costs: list[ModelCost] = field(default_factory=list[ModelCost])
    wall_clock_seconds: float = 0.0
    agent_time_seconds: float = 0.0
    tasks: TaskCounts = field(default_factory=TaskCounts)


@dataclass(frozen=True, slots=True)
class FailureSummary:
    """Inputs for the failure-path ``## Failure reason`` block.

    Attributes:
        failing_stage: Name of the stage in which the run aborted.
        task_ids: IDs of the tasks that failed in that stage.
        last_error: Tail of the most recent error message; truncated by
            the renderer if longer than ``_MAX_ERROR_CHARS``.
    """

    failing_stage: str = ""
    task_ids: list[str] = field(default_factory=list[str])
    last_error: str = ""


_MAX_ERROR_CHARS: int = 2000
"""Truncation length for ``last_error`` excerpt in the failure block."""


def _format_duration(seconds: float) -> str:
    """Render seconds as ``HhMMmSSs`` (e.g. ``1h02m07s``)."""
    if seconds <= 0:
        return "0s"
    s = round(seconds)
    hours, remainder = divmod(s, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _render_gate_table(rows: list[GateResult]) -> list[str]:
    """Render the gate-result Markdown table (always 3 columns)."""
    lines: list[str] = ["| Gate | Result | Detail |", "|------|--------|--------|"]
    if not rows:
        lines.append("| _none_ | n/a | n/a |")
        return lines
    for row in rows:
        result = "pass" if row.passed else "fail"
        detail = row.detail.replace("|", "\\|") if row.detail else "—"
        lines.append(f"| {row.name} | {result} | {detail} |")
    return lines


def _render_cost_table(rows: list[ModelCost]) -> list[str]:
    """Render the per-model cost Markdown table."""
    lines: list[str] = ["| Model | Spend (USD) |", "|-------|-------------|"]
    if not rows:
        lines.append("| _none_ | $0.0000 |")
        return lines
    total = 0.0
    for row in rows:
        total += row.spend_usd
        lines.append(f"| {row.model} | ${row.spend_usd:.4f} |")
    lines.append(f"| **total** | **${total:.4f}** |")
    return lines


def render_summary_block(summary: RunSummary) -> str:
    """Render the success ``## Run summary`` Markdown block.

    Args:
        summary: Run summary inputs.

    Returns:
        Multi-line Markdown string ending with a trailing newline.
    """
    pr_url = summary.pr_url or "n/a"
    lines: list[str] = [
        "<!--",
        "## Run summary",
        "",
        f"- PR: {pr_url}",
        "",
        "### Gate results",
    ]
    lines.extend(_render_gate_table(summary.gate_results))
    lines.extend(["", "### Cost breakdown"])
    lines.extend(_render_cost_table(summary.model_costs))
    wall = _format_duration(summary.wall_clock_seconds)
    agent = _format_duration(summary.agent_time_seconds)
    lines.extend(
        [
            "",
            "### Duration",
            f"- Wall-clock: {wall}",
            f"- Agent-time: {agent}",
            "",
            "### Tasks",
            f"- Completed: {summary.tasks.completed}",
            f"- Failed: {summary.tasks.failed}",
            f"- Skipped: {summary.tasks.skipped}",
            "-->",
            "",
        ]
    )
    return "\n".join(lines)


def render_failure_block(failure: FailureSummary) -> str:
    """Render the failure ``## Failure reason`` Markdown block.

    Args:
        failure: Failure inputs.

    Returns:
        Multi-line Markdown string ending with a trailing newline.
    """
    stage = failure.failing_stage or "n/a"
    task_ids = ", ".join(failure.task_ids) if failure.task_ids else "n/a"
    excerpt = failure.last_error or ""
    truncated = False
    if len(excerpt) > _MAX_ERROR_CHARS:
        excerpt = excerpt[-_MAX_ERROR_CHARS:]
        truncated = True
    lines: list[str] = [
        "<!--",
        "## Failure reason",
        "",
        f"- Failing stage: {stage}",
        f"- Failed task ids: {task_ids}",
        "",
        "### Last error excerpt",
        "```",
        excerpt or "(no error captured)",
        "```",
    ]
    if truncated:
        lines.append(f"_(error log truncated to last {_MAX_ERROR_CHARS} chars)_")
    lines.extend(["-->", ""])
    return "\n".join(lines)
