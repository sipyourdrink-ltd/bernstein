"""Automated post-mortem report generation for failed orchestration runs.

Analyses a completed (or failed) run and produces a structured report
covering:

- Chronological event timeline
- Root-cause analysis derived from agent log failure patterns
- Contributing factors (rate limits, compile errors, test failures, …)
- Per-task agent decision traces for every failed task
- Recommended actions to prevent recurrence

Data sources (read-only):
- ``.sdd/runs/{run_id}/summary.json`` — high-level run summary
- ``.sdd/metrics/task_*.json`` / ``*.jsonl`` — per-task metrics
- ``.sdd/runtime/{session}.log`` / ``.sdd/logs/{session}.log`` — agent logs
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PostMortemEvent:
    """A single event in the chronological timeline.

    Attributes:
        timestamp: Unix epoch seconds (0 means unknown).
        label: Short human-readable description of the event.
        kind: Category such as ``"task_start"``, ``"task_fail"``, ``"error"``.
        task_id: Associated task identifier if applicable.
    """

    timestamp: float
    label: str
    kind: str
    task_id: str = ""


@dataclass
class FailedTaskTrace:
    """Agent decision trace for a single failed task.

    Attributes:
        task_id: Task identifier.
        role: Agent role that attempted the task.
        model: Model used.
        session_id: Agent session identifier.
        dominant_failure: Most frequent failure category detected in logs.
        error_snippets: Up to three representative error messages.
        files_touched: Files the agent modified before failing.
        retry_context: Concise retry-context summary from the log aggregator.
    """

    task_id: str
    role: str
    model: str
    session_id: str
    dominant_failure: str
    error_snippets: list[str]
    files_touched: list[str]
    retry_context: str


@dataclass
class ContributingFactor:
    """A factor that contributed to the run failure.

    Attributes:
        category: E.g. ``"rate_limit"``, ``"compile_error"``, ``"test_failure"``.
        count: How many times this pattern was observed.
        description: Human-readable explanation.
    """

    category: str
    count: int
    description: str


@dataclass
class RecommendedAction:
    """A concrete action recommended to prevent recurrence.

    Attributes:
        priority: ``"high"``, ``"medium"``, or ``"low"``.
        action: Imperative description of what to do.
        rationale: Why this action is recommended.
    """

    priority: str
    action: str
    rationale: str


@dataclass
class PostMortemReport:
    """Full post-mortem report for a run.

    Attributes:
        run_id: Orchestrator run identifier.
        goal: High-level goal of the run.
        generated_at: Unix epoch when the report was generated.
        total_tasks: Total number of tasks in the run.
        failed_tasks: Number of tasks that failed.
        success_rate_pct: Percentage of tasks that succeeded.
        timeline: Chronological events ordered by timestamp.
        failed_task_traces: Per-task traces for every failed task.
        contributing_factors: Aggregated failure patterns across all agents.
        recommended_actions: Prioritised list of remediation actions.
    """

    run_id: str
    goal: str
    generated_at: float
    total_tasks: int
    failed_tasks: int
    success_rate_pct: float
    timeline: list[PostMortemEvent] = field(default_factory=list)
    failed_task_traces: list[FailedTaskTrace] = field(default_factory=list)
    contributing_factors: list[ContributingFactor] = field(default_factory=list)
    recommended_actions: list[RecommendedAction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

_FACTOR_DESCRIPTIONS: dict[str, str] = {
    "rate_limit": "Agent hit API rate limits, causing delays or retries.",
    "compile_error": "Syntax or import errors prevented code execution.",
    "test_failure": "Automated tests failed, blocking task completion.",
    "tool_failure": "Tool calls (file I/O, shell, etc.) returned errors.",
    "git_error": "Git operations failed (merge conflict, worktree lock).",
    "timeout": "Agent exceeded the allowed time budget for the task.",
    "permission": "File or system permission errors blocked progress.",
}

_ACTION_TEMPLATES: dict[str, tuple[str, str, str]] = {
    # category → (priority, action, rationale)
    "rate_limit": (
        "high",
        "Add exponential back-off and configure a lower concurrency ceiling.",
        "Repeated rate-limit hits stall agents and inflate wall-clock time.",
    ),
    "compile_error": (
        "high",
        "Add a pre-task linting step so agents start from a clean baseline.",
        "Compile errors at the start of a task waste the full model budget.",
    ),
    "test_failure": (
        "medium",
        "Run the failing tests locally and fix them before the next run.",
        "Persistent test failures indicate regressions introduced during the run.",
    ),
    "tool_failure": (
        "medium",
        "Review tool permissions and verify the environment setup.",
        "Tool failures often indicate missing dependencies or config drift.",
    ),
    "git_error": (
        "high",
        "Resolve open worktree locks or merge conflicts before re-running.",
        "Git errors leave the workspace in an inconsistent state.",
    ),
    "timeout": (
        "medium",
        "Increase the per-task timeout or decompose the task into smaller steps.",
        "Timeouts indicate that tasks are scoped too broadly for the model.",
    ),
    "permission": (
        "low",
        "Verify file-system permissions for the workspace directory.",
        "Permission errors are usually a one-time environment misconfiguration.",
    ),
}


def _html_badge(label: str, color: str) -> str:
    """Render a small colored badge span."""
    import html as _html

    _badge_css = "padding:2px 6px;border-radius:3px;font-size:0.8em"
    return f'<span style="background:{color};color:#fff;{_badge_css}">{_html.escape(label)}</span>'


def _html_status_badge(kind: str) -> str:
    """Render a status kind badge."""
    colors = {
        "task_start": "#2196F3",
        "task_complete": "#4CAF50",
        "task_fail": "#F44336",
        "error": "#FF9800",
    }
    color = colors.get(kind, "#9E9E9E")
    return _html_badge(kind, color)


def _html_priority_badge(priority: str) -> str:
    """Render a priority badge."""
    colors = {"high": "#F44336", "medium": "#FF9800", "low": "#4CAF50"}
    color = colors.get(priority, "#9E9E9E")
    return _html_badge(priority.upper(), color)


def _html_timeline_section(report: PostMortemReport) -> str:
    """Render the HTML timeline section."""
    import datetime as _dt
    import html

    if not report.timeline:
        return "<p class='muted'>No timeline data available.</p>"

    def _ev_time(ev: PostMortemEvent) -> str:
        return _dt.datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S") if ev.timestamp > 0 else "\u2014"

    timeline_rows = "\n".join(
        f"<tr><td>{_ev_time(ev)}</td>"
        f"<td>{html.escape(ev.label)}</td>"
        f"<td>{_html_status_badge(ev.kind)}</td>"
        f"<td>{html.escape(ev.task_id) if ev.task_id else '\u2014'}</td></tr>"
        for ev in report.timeline
    )
    return f"""
<table>
  <thead><tr><th>Time</th><th>Event</th><th>Kind</th><th>Task</th></tr></thead>
  <tbody>{timeline_rows}</tbody>
</table>"""


def _html_rca_section(report: PostMortemReport) -> str:
    """Render the HTML root cause analysis section."""
    import html

    if not report.contributing_factors:
        return "<p class='muted'>No dominant failure pattern detected. Review agent logs manually.</p>"
    dominant = max(report.contributing_factors, key=lambda f: f.count)
    factor_rows = "\n".join(
        f"<tr><td><strong>{html.escape(f.category)}</strong></td><td>{f.count}</td><td>{html.escape(f.description)}</td></tr>"
        for f in sorted(report.contributing_factors, key=lambda f: f.count, reverse=True)
    )
    return f"""
<div class='callout'>
  <strong>Primary cause:</strong> {html.escape(dominant.category)} ({dominant.count} occurrence(s))<br>
  <em>{html.escape(dominant.description)}</em>
</div>
<table>
  <thead><tr><th>Category</th><th>Count</th><th>Description</th></tr></thead>
  <tbody>{factor_rows}</tbody>
</table>"""


def _html_trace_card(trace: Any) -> str:
    """Render a single failed task trace as an HTML card."""
    import html

    snippets_html = ""
    if trace.error_snippets:
        snippet_text = "\n".join(html.escape(s[:200]) for s in trace.error_snippets[:3])
        snippets_html = f"<pre class='error-box'>{snippet_text}</pre>"
    files_html = (
        ", ".join(f"<code>{html.escape(f)}</code>" for f in trace.files_touched[:5])
        if trace.files_touched
        else "\u2014"
    )
    retry_html = (
        f"<p><strong>Retry context:</strong> {html.escape(trace.retry_context)}</p>" if trace.retry_context else ""
    )
    return f"""<div class='trace-card'>
  <h3>Task <code>{html.escape(trace.task_id)}</code></h3>
  <table class='compact'>
    <tr><th>Role</th><td>{html.escape(trace.role) or "\u2014"}</td></tr>
    <tr><th>Model</th><td>{html.escape(trace.model) or "\u2014"}</td></tr>
    <tr><th>Session</th><td><code>{html.escape(trace.session_id) or "\u2014"}</code></td></tr>
    <tr><th>Dominant failure</th><td><code>{html.escape(trace.dominant_failure) or "unknown"}</code></td></tr>
    <tr><th>Files touched</th><td>{files_html}</td></tr>
  </table>
  {snippets_html}
  {retry_html}
</div>"""


def _html_actions_section(report: PostMortemReport) -> str:
    """Render the HTML recommended actions section."""
    import html

    if not report.recommended_actions:
        return "<p class='muted'>No specific actions recommended.</p>"
    action_rows = "\n".join(
        f"<tr><td>{_html_priority_badge(a.priority)}</td><td>{html.escape(a.action)}</td><td><em>{html.escape(a.rationale)}</em></td></tr>"
        for a in sorted(
            report.recommended_actions,
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.priority, 3),
        )
    )
    return f"""
<table>
  <thead><tr><th>Priority</th><th>Action</th><th>Rationale</th></tr></thead>
  <tbody>{action_rows}</tbody>
</table>"""


class PostMortemGenerator:
    """Generates a :class:`PostMortemReport` from ``.sdd/`` data.

    Args:
        workdir: Project root containing the ``.sdd/`` directory.
        run_id: Specific run to analyse.  If ``None``, the latest run is
            auto-detected from ``.sdd/runs/``.
    """

    def __init__(self, workdir: Path, run_id: str | None = None) -> None:
        self._workdir = workdir
        self._sdd = workdir / ".sdd"
        self._run_id = run_id or self._detect_latest_run_id()

    # -- public API ---------------------------------------------------------

    def generate(self) -> PostMortemReport:
        """Collect data and build a :class:`PostMortemReport`.

        Returns:
            Fully populated ``PostMortemReport``.
        """
        summary = self._load_summary()
        task_metrics = self._load_task_metrics()

        goal = str(summary.get("goal", ""))
        total = len(task_metrics)
        failed_count = sum(1 for tm in task_metrics if not bool(tm.get("success", False)))
        success_rate = ((total - failed_count) / total * 100) if total > 0 else 0.0

        timeline = self._build_timeline(task_metrics)
        failed_traces = self._build_failed_traces(task_metrics)
        factors = self._aggregate_factors(failed_traces)
        actions = self._build_recommendations(factors)

        return PostMortemReport(
            run_id=self._run_id,
            goal=goal,
            generated_at=time.time(),
            total_tasks=total,
            failed_tasks=failed_count,
            success_rate_pct=success_rate,
            timeline=timeline,
            failed_task_traces=failed_traces,
            contributing_factors=factors,
            recommended_actions=actions,
        )

    @staticmethod
    def _md_timeline(report: PostMortemReport) -> list[str]:
        """Render the timeline section as markdown lines."""
        import datetime as _dt

        lines: list[str] = ["## Event Timeline", ""]
        if not report.timeline:
            lines.append("No timeline data available.")
            return lines
        lines.append("| Time | Event | Kind | Task |")
        lines.append("|------|-------|------|------|")
        for ev in report.timeline:
            t_str = _dt.datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S") if ev.timestamp > 0 else "—"
            lines.append(f"| {t_str} | {ev.label} | `{ev.kind}` | {ev.task_id or '—'} |")
        return lines

    @staticmethod
    def _md_root_cause(report: PostMortemReport) -> list[str]:
        """Render the root cause analysis section as markdown lines."""
        lines: list[str] = ["## Root Cause Analysis", ""]
        if not report.contributing_factors:
            lines.append("No dominant failure pattern detected. Review agent logs manually for more detail.")
            return lines
        dominant = max(report.contributing_factors, key=lambda f: f.count)
        lines.append(f"**Primary cause:** {dominant.category} ({dominant.count} occurrence(s))")
        lines.append("")
        lines.append(f"> {dominant.description}")
        lines.append("")
        if len(report.contributing_factors) > 1:
            lines.append("**Additional contributing factors:**")
            lines.append("")
            for factor in sorted(report.contributing_factors, key=lambda f: f.count, reverse=True):
                if factor is dominant:
                    continue
                lines.append(f"- **{factor.category}** ({factor.count}x): {factor.description}")
        return lines

    @staticmethod
    def _md_trace(trace: Any) -> list[str]:
        """Render a single failed task trace as markdown lines."""
        lines: list[str] = [f"### Task `{trace.task_id}`", ""]
        lines.append(f"- **Role:** {trace.role or '—'}")
        lines.append(f"- **Model:** {trace.model or '—'}")
        lines.append(f"- **Session:** `{trace.session_id or '—'}`")
        lines.append(f"- **Dominant failure:** `{trace.dominant_failure or 'unknown'}`")
        if trace.files_touched:
            lines.append(f"- **Files touched:** {', '.join(f'`{f}`' for f in trace.files_touched[:5])}")
        if trace.error_snippets:
            lines.extend(["", "**Error snippets:**", "", "```"])
            for snippet in trace.error_snippets[:3]:
                lines.append(snippet[:200])
            lines.append("```")
        if trace.retry_context:
            lines.extend(["", f"**Retry context:** {trace.retry_context}"])
        lines.append("")
        return lines

    @staticmethod
    def _md_actions(report: PostMortemReport) -> list[str]:
        """Render the recommended actions section as markdown lines."""
        lines: list[str] = ["## Recommended Actions", ""]
        if not report.recommended_actions:
            lines.append("No specific actions recommended.")
            return lines
        for action in sorted(
            report.recommended_actions,
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.priority, 3),
        ):
            badge = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(action.priority, "•")
            lines.append(f"{badge} **[{action.priority.upper()}]** {action.action}")
            lines.append(f"  _{action.rationale}_")
            lines.append("")
        return lines

    def to_markdown(self, report: PostMortemReport) -> str:
        """Render a :class:`PostMortemReport` as a markdown string.

        Args:
            report: The report to render.

        Returns:
            Multi-line markdown string.
        """
        import datetime

        lines: list[str] = []
        ts = datetime.datetime.fromtimestamp(report.generated_at).strftime("%Y-%m-%d %H:%M:%S")

        lines.append(f"# Post-Mortem Report — Run `{report.run_id}`")
        lines.append("")
        if report.goal:
            lines.append(f"**Goal:** {report.goal}")
        lines.append(f"**Generated:** {ts}")
        lines.append(f"**Tasks:** {report.total_tasks} total, {report.failed_tasks} failed")
        if report.total_tasks:
            lines.append(f"**Success rate:** {report.success_rate_pct:.0f}%")
        lines.append("")

        lines.extend(self._md_timeline(report))
        lines.append("")
        lines.extend(self._md_root_cause(report))
        lines.append("")

        lines.append("## Agent Decision Traces (Failed Tasks)")
        lines.append("")
        if report.failed_task_traces:
            for trace in report.failed_task_traces:
                lines.extend(self._md_trace(trace))
        else:
            lines.append("No failed task traces available.")
        lines.append("")

        lines.extend(self._md_actions(report))
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _html_timeline_section(report: PostMortemReport) -> str:
        """Render the timeline section of the HTML report."""
        import datetime
        import html

        _badge_css = "padding:2px 6px;border-radius:3px;font-size:0.8em"

        def _status_badge(kind: str) -> str:
            colors = {
                "task_start": "#2196F3",
                "task_complete": "#4CAF50",
                "task_fail": "#F44336",
                "error": "#FF9800",
            }
            color = colors.get(kind, "#9E9E9E")
            label = html.escape(kind)
            return f'<span style="background:{color};color:#fff;{_badge_css}">{label}</span>'

        if not report.timeline:
            return "<p class='muted'>No timeline data available.</p>"

        def _ev_time(ev: PostMortemEvent) -> str:
            return datetime.datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S") if ev.timestamp > 0 else "—"

        timeline_rows = "\n".join(
            f"<tr><td>{_ev_time(ev)}</td>"
            f"<td>{html.escape(ev.label)}</td>"
            f"<td>{_status_badge(ev.kind)}</td>"
            f"<td>{html.escape(ev.task_id) if ev.task_id else '—'}</td></tr>"
            for ev in report.timeline
        )
        return f"""
<table>
  <thead><tr><th>Time</th><th>Event</th><th>Kind</th><th>Task</th></tr></thead>
  <tbody>{timeline_rows}</tbody>
</table>"""

    @staticmethod
    def _html_rca_section(report: PostMortemReport) -> str:
        """Render the root cause analysis section of the HTML report."""
        import html

        if not report.contributing_factors:
            return "<p class='muted'>No dominant failure pattern detected. Review agent logs manually.</p>"

        dominant = max(report.contributing_factors, key=lambda f: f.count)
        factor_rows = "\n".join(
            f"<tr><td><strong>{html.escape(f.category)}</strong></td><td>{f.count}</td><td>{html.escape(f.description)}</td></tr>"
            for f in sorted(report.contributing_factors, key=lambda f: f.count, reverse=True)
        )
        return f"""
<div class='callout'>
  <strong>Primary cause:</strong> {html.escape(dominant.category)} ({dominant.count} occurrence(s))<br>
  <em>{html.escape(dominant.description)}</em>
</div>
<table>
  <thead><tr><th>Category</th><th>Count</th><th>Description</th></tr></thead>
  <tbody>{factor_rows}</tbody>
</table>"""

    @staticmethod
    def _html_traces_section(report: PostMortemReport) -> str:
        """Render the failed task traces section of the HTML report."""
        import html

        if not report.failed_task_traces:
            return "<p class='muted'>No failed task traces.</p>"

        trace_parts: list[str] = []
        for trace in report.failed_task_traces:
            snippets_html = ""
            if trace.error_snippets:
                snippet_text = "\n".join(html.escape(s[:200]) for s in trace.error_snippets[:3])
                snippets_html = f"<pre class='error-box'>{snippet_text}</pre>"
            files_html = (
                ", ".join(f"<code>{html.escape(f)}</code>" for f in trace.files_touched[:5])
                if trace.files_touched
                else "—"
            )
            trace_parts.append(
                f"""<div class='trace-card'>
  <h3>Task <code>{html.escape(trace.task_id)}</code></h3>
  <table class='compact'>
    <tr><th>Role</th><td>{html.escape(trace.role) or "—"}</td></tr>
    <tr><th>Model</th><td>{html.escape(trace.model) or "—"}</td></tr>
    <tr><th>Session</th><td><code>{html.escape(trace.session_id) or "—"}</code></td></tr>
    <tr><th>Dominant failure</th><td><code>{html.escape(trace.dominant_failure) or "unknown"}</code></td></tr>
    <tr><th>Files touched</th><td>{files_html}</td></tr>
  </table>
  {snippets_html}
  {f"<p><strong>Retry context:</strong> {html.escape(trace.retry_context)}</p>" if trace.retry_context else ""}
</div>"""
            )
        return "\n".join(trace_parts)

    @staticmethod
    def _html_actions_section(report: PostMortemReport) -> str:
        """Render the recommended actions section of the HTML report."""
        import html

        _badge_css = "padding:2px 6px;border-radius:3px;font-size:0.8em"

        def _priority_badge(priority: str) -> str:
            colors = {"high": "#F44336", "medium": "#FF9800", "low": "#4CAF50"}
            color = colors.get(priority, "#9E9E9E")
            label = html.escape(priority.upper())
            return f'<span style="background:{color};color:#fff;{_badge_css}">{label}</span>'

        if not report.recommended_actions:
            return "<p class='muted'>No specific actions recommended.</p>"

        action_rows = "\n".join(
            f"<tr><td>{_priority_badge(a.priority)}</td><td>{html.escape(a.action)}</td><td><em>{html.escape(a.rationale)}</em></td></tr>"
            for a in sorted(
                report.recommended_actions,
                key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(a.priority, 3),
            )
        )
        return f"""
<table>
  <thead><tr><th>Priority</th><th>Action</th><th>Rationale</th></tr></thead>
  <tbody>{action_rows}</tbody>
</table>"""

    def to_html(self, report: PostMortemReport) -> str:
        """Render a :class:`PostMortemReport` as a styled HTML document.

        Generates a fully self-contained HTML page with embedded CSS, proper
        tables for the timeline, contributing factors, and recommended actions.

        Args:
            report: The report to render.

        Returns:
            Complete HTML string with embedded styling.
        """
        import datetime
        import html

        ts = datetime.datetime.fromtimestamp(report.generated_at).strftime("%Y-%m-%d %H:%M:%S")
        title = html.escape(f"Post-Mortem: {report.run_id}")

        timeline_section = _html_timeline_section(report)
        rca_section = _html_rca_section(report)
        trace_cards = [_html_trace_card(t) for t in report.failed_task_traces]
        traces_section = "\n".join(trace_cards) if trace_cards else "<p class='muted'>No failed task traces.</p>"
        actions_section = _html_actions_section(report)

        if report.success_rate_pct >= 80:
            success_color = "#4CAF50"
        elif report.success_rate_pct >= 50:
            success_color = "#FF9800"
        else:
            success_color = "#F44336"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            max-width: 960px; margin: 2em auto; padding: 0 1.5em;
            color: #212121; line-height: 1.6; }}
    h1 {{ border-bottom: 3px solid #1565C0; padding-bottom: .3em; color: #1565C0; }}
    h2 {{ color: #283593; border-bottom: 1px solid #e0e0e0;
          padding-bottom: .2em; margin-top: 2em; }}
    h3 {{ color: #37474F; margin-top: 1em; }}
    .meta {{ display: flex; gap: 2em; flex-wrap: wrap; background: #f5f5f5;
             padding: .8em 1.2em; border-radius: 6px; margin: 1em 0; }}
    .meta span {{ font-size: .9em; }}
    .meta strong {{ color: #1565C0; }}
    table {{ width: 100%; border-collapse: collapse; margin: .8em 0; font-size: .9em; }}
    th {{ background: #E3F2FD; text-align: left; padding: .5em .8em; border-bottom: 2px solid #90CAF9; }}
    td {{ padding: .4em .8em; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
    tr:nth-child(even) {{ background: #FAFAFA; }}
    table.compact th {{ width: 140px; }}
    pre {{ background: #263238; color: #ECEFF1; padding: 1em; border-radius: 4px; overflow-x: auto; font-size: .85em; }}
    pre.error-box {{ background: #3E2723; color: #FFCCBC; margin: .5em 0; }}
    code {{ background: #e8eaf6; padding: 1px 4px; border-radius: 3px; font-size: .9em; }}
    .muted {{ color: #757575; font-style: italic; }}
    .callout {{ background: #FFF8E1; border-left: 4px solid #FFC107;
               padding: .8em 1.2em; border-radius: 0 6px 6px 0; margin: .8em 0; }}
    .trace-card {{ border: 1px solid #e0e0e0; border-radius: 6px; padding: 1em; margin: 1em 0; background: #FAFAFA; }}
    .stat {{ font-size: 1.5em; font-weight: bold; color: {success_color}; }}
    @media print {{ .trace-card {{ page-break-inside: avoid; }} }}
  </style>
</head>
<body>
<h1>Post-Mortem Report</h1>
<div class="meta">
  <span><strong>Run ID:</strong> <code>{html.escape(report.run_id)}</code></span>
  <span><strong>Generated:</strong> {ts}</span>
  {f"<span><strong>Goal:</strong> {html.escape(report.goal)}</span>" if report.goal else ""}
  <span><strong>Tasks:</strong> {report.total_tasks} total, {report.failed_tasks} failed</span>
  {
            f'<span><strong>Success rate:</strong> <span class="stat">{report.success_rate_pct:.0f}%</span></span>'
            if report.total_tasks
            else ""
        }
</div>

<h2>Event Timeline</h2>
{timeline_section}

<h2>Root Cause Analysis</h2>
{rca_section}

<h2>Agent Decision Traces (Failed Tasks)</h2>
{traces_section}

<h2>Recommended Actions</h2>
{actions_section}
</body>
</html>
"""

    def to_pdf(self, report: PostMortemReport, path: Path | None = None) -> Path:
        """Render a :class:`PostMortemReport` as a PDF file.

        Uses ``weasyprint`` if installed; falls back to saving an HTML file
        with a message directing the user to print from their browser.

        Args:
            report: The report to render.
            path: Explicit output path.  Defaults to
                ``.sdd/reports/postmortem_{run_id}.pdf``.

        Returns:
            Path where the PDF (or fallback HTML) was written.

        Raises:
            RuntimeError: If path cannot be resolved.
        """
        from pathlib import Path as _Path

        if path is None:
            reports_dir = self._sdd / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = reports_dir / f"postmortem_{report.run_id}.pdf"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

        html_content = self.to_html(report)

        # Try weasyprint (optional dependency).
        try:
            import weasyprint  # type: ignore[import-untyped]

            weasyprint.HTML(string=html_content).write_pdf(str(path))
            logger.info("Post-mortem PDF written to %s (weasyprint)", path)
            return _Path(path)
        except ImportError:
            pass

        # Try wkhtmltopdf via subprocess (common system tool).
        import subprocess
        import tempfile

        try:
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as tmp:
                tmp.write(html_content)
                tmp_path = tmp.name

            result = subprocess.run(
                ["wkhtmltopdf", "--quiet", tmp_path, str(path)],
                capture_output=True,
                timeout=60,
            )
            _Path(tmp_path).unlink(missing_ok=True)
            if result.returncode == 0:
                logger.info("Post-mortem PDF written to %s (wkhtmltopdf)", path)
                return _Path(path)
        except (subprocess.TimeoutExpired, OSError):
            pass
        finally:
            import os as _os

            with contextlib.suppress(Exception):
                _os.unlink(tmp_path)

        # Fallback: save HTML and inform user.
        html_path = _Path(str(path).replace(".pdf", ".html"))
        html_path.write_text(html_content, encoding="utf-8")
        logger.warning(
            "PDF export requires weasyprint or wkhtmltopdf. "
            "HTML saved to %s — open in browser and use File → Print → Save as PDF.",
            html_path,
        )
        return html_path

    def save(self, report: PostMortemReport, fmt: str = "markdown", path: Path | None = None) -> Path:
        """Write the post-mortem report to disk.

        Args:
            report: The report to save.
            fmt: ``"markdown"``, ``"html"``, or ``"pdf"``.
            path: Explicit output path.  Defaults to
                ``.sdd/reports/postmortem_{run_id}.{ext}``.

        Returns:
            Path where the report was written.
        """
        from pathlib import Path as _Path

        if fmt == "pdf":
            return self.to_pdf(report, path)

        ext = "html" if fmt == "html" else "md"
        if path is None:
            reports_dir = self._sdd / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = reports_dir / f"postmortem_{report.run_id}.{ext}"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

        content = self.to_html(report) if fmt == "html" else self.to_markdown(report)
        _Path(path).write_text(content, encoding="utf-8")
        logger.info("Post-mortem report written to %s", path)
        return path

    # -- internal helpers ---------------------------------------------------

    def _detect_latest_run_id(self) -> str:
        """Find the most recent run ID from ``.sdd/runs/``."""
        runs_dir = self._sdd / "runs"
        if not runs_dir.is_dir():
            return "unknown"
        run_dirs = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for d in run_dirs:
            if d.is_dir() and (d / "summary.json").exists():
                return d.name
        return "unknown"

    def _load_summary(self) -> dict[str, Any]:
        """Load summary.json for the run."""
        summary_path = self._sdd / "runs" / self._run_id / "summary.json"
        if not summary_path.exists():
            return {}
        try:
            raw: Any = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast("dict[str, Any]", raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load summary.json: %s", exc)
        return {}

    def _load_task_metrics(self) -> list[dict[str, Any]]:
        """Load per-task metrics from ``.sdd/metrics/task_*.json``."""
        metrics_dir = self._sdd / "metrics"
        if not metrics_dir.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for f in sorted(metrics_dir.glob("task_*.json")):
            try:
                raw: Any = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    results.append(cast("dict[str, Any]", raw))
            except (OSError, json.JSONDecodeError):
                continue
        if results:
            return results

        # Fallback: scan JSONL for task_completion_time entries
        for f in sorted(metrics_dir.glob("*.jsonl")):
            try:
                for raw_line in f.read_text(encoding="utf-8").splitlines():
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    entry_raw: Any = json.loads(stripped)
                    if not isinstance(entry_raw, dict):
                        continue
                    entry = cast("dict[str, Any]", entry_raw)
                    if entry.get("metric_type") != "task_completion_time":
                        continue
                    labels: dict[str, Any] = entry.get("labels") or {}
                    results.append(
                        {
                            "task_id": str(labels.get("task_id", "")),
                            "role": str(labels.get("role", "")),
                            "model": str(labels.get("model", "")),
                            "success": str(labels.get("success", "false")).lower() == "true",
                            "session_id": str(labels.get("session_id", "")),
                            "start_time": 0.0,
                            "end_time": float(entry.get("timestamp", 0.0)),
                            "cost_usd": 0.0,
                        }
                    )
            except (OSError, json.JSONDecodeError):
                continue
        return results

    def _build_timeline(self, task_metrics: list[dict[str, Any]]) -> list[PostMortemEvent]:
        """Build a chronological list of events from task metrics."""
        events: list[PostMortemEvent] = []
        for tm in task_metrics:
            task_id = str(tm.get("task_id", ""))
            start = float(tm.get("start_time", 0.0))
            end = float(tm.get("end_time", 0.0))
            success = bool(tm.get("success", False))

            if start > 0:
                events.append(
                    PostMortemEvent(
                        timestamp=start,
                        label=f"Task started: {task_id}",
                        kind="task_start",
                        task_id=task_id,
                    )
                )
            if end > 0:
                kind = "task_complete" if success else "task_fail"
                label = f"Task {'completed' if success else 'FAILED'}: {task_id}"
                events.append(PostMortemEvent(timestamp=end, label=label, kind=kind, task_id=task_id))

        return sorted(events, key=lambda e: e.timestamp)

    def _build_failed_traces(self, task_metrics: list[dict[str, Any]]) -> list[FailedTaskTrace]:
        """Build decision traces for each failed task using agent logs."""
        from bernstein.core.agent_log_aggregator import AgentLogAggregator

        aggregator = AgentLogAggregator(self._workdir)
        traces: list[FailedTaskTrace] = []

        for tm in task_metrics:
            if bool(tm.get("success", False)):
                continue

            task_id = str(tm.get("task_id", ""))
            role = str(tm.get("role", ""))
            model = str(tm.get("model", ""))
            session_id = str(tm.get("session_id", ""))

            if session_id and aggregator.log_exists(session_id):
                summary = aggregator.parse_log(session_id)
                retry_ctx = aggregator.failure_context_for_retry(session_id)
                error_snippets = [ev.message[:200] for ev in summary.events if ev.level == "error"][:3]
                dominant = summary.dominant_failure_category or ""
                files_touched = summary.files_modified
            else:
                retry_ctx = ""
                error_snippets = []
                dominant = ""
                files_touched = []

            traces.append(
                FailedTaskTrace(
                    task_id=task_id,
                    role=role,
                    model=model,
                    session_id=session_id,
                    dominant_failure=dominant,
                    error_snippets=error_snippets,
                    files_touched=files_touched,
                    retry_context=retry_ctx,
                )
            )

        return traces

    def _aggregate_factors(self, traces: list[FailedTaskTrace]) -> list[ContributingFactor]:
        """Count failure categories across all failed task traces."""
        counts: dict[str, int] = {}
        for trace in traces:
            if trace.dominant_failure:
                counts[trace.dominant_failure] = counts.get(trace.dominant_failure, 0) + 1

        factors: list[ContributingFactor] = []
        for category, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            desc = _FACTOR_DESCRIPTIONS.get(category, f"Repeated {category} errors.")
            factors.append(ContributingFactor(category=category, count=count, description=desc))
        return factors

    def _build_recommendations(self, factors: list[ContributingFactor]) -> list[RecommendedAction]:
        """Map contributing factors to concrete recommended actions."""
        seen: set[str] = set()
        actions: list[RecommendedAction] = []
        for factor in factors:
            template = _ACTION_TEMPLATES.get(factor.category)
            if template and factor.category not in seen:
                seen.add(factor.category)
                priority, action, rationale = template
                actions.append(RecommendedAction(priority=priority, action=action, rationale=rationale))
        return actions
