"""HTML/Markdown run report generation for ``bernstein export``.

Reads task history and metrics from ``.sdd/`` and renders either:
- A self-contained HTML report with inline CSS (no external dependencies)
- A Markdown report as fallback

Enforces a 500KB file-size limit for offline rendering.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 500 * 1024  # 500 KB
REPO_URL = "https://github.com/sipyourdrink-ltd/bernstein"


# ---------------------------------------------------------------------------
# Minimal data classes (avoid importing from orchestration module)
# ---------------------------------------------------------------------------


@dataclass
class _TaskRow:
    title: str
    role: str
    status: str
    model: str
    duration_s: float
    cost_usd: float
    janitor_passed: bool


@dataclass
class _ModelCost:
    model: str
    total_cost_usd: float
    invocation_count: int
    total_tokens: int


@dataclass
class _TimelineEntry:
    title: str
    start_offset_s: float
    end_offset_s: float


@dataclass
class _ExportReport:
    goal: str
    run_id: str
    duration_s: float
    total_cost_usd: float
    tasks_completed: int
    tasks_failed: int
    agents_spawned: int
    task_rows: list[_TaskRow] = field(default_factory=list)
    model_costs: list[_ModelCost] = field(default_factory=list)
    timeline_entries: list[_TimelineEntry] = field(default_factory=list)
    quality_pass_count: int = 0
    quality_fail_count: int = 0


# ---------------------------------------------------------------------------
# Data loading (mirrors RunReportGenerator logic, kept local to avoid
# pulling in the full orchestration module via meta-path redirect).
# ---------------------------------------------------------------------------


def _load_report_data(workdir: Path, run_id: str | None) -> _ExportReport:
    """Load run data from ``.sdd/`` and return a minimal report object."""
    sdd = workdir / ".sdd"

    if not run_id:
        run_id = _detect_latest(sdd)
        if run_id == "unknown":
            return _ExportReport(
                goal="",
                run_id="unknown",
                duration_s=0.0,
                total_cost_usd=0.0,
                tasks_completed=0,
                tasks_failed=0,
                agents_spawned=0,
            )

    summary = _load_json(sdd / "runs" / run_id / "summary.json")
    metrics_dir = sdd / "metrics"

    tasks_completed = int(summary.get("tasks_completed", 0))
    tasks_failed = int(summary.get("tasks_failed", 0))
    duration_s = float(summary.get("wall_clock_seconds", 0.0))
    total_cost_usd = float(summary.get("total_cost_usd", 0.0))
    goal = str(summary.get("goal", ""))

    # Load task metrics from .sdd/archive/tasks.jsonl
    task_metrics: list[dict[str, Any]] = []
    archive_path = sdd / "archive" / "tasks.jsonl"
    if archive_path.is_file():
        for line in archive_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    task_metrics.append(json.loads(line))

    # Load agent count
    agents_spawned = 0
    if metrics_dir.is_dir():
        agents_spawned = len(list(metrics_dir.glob("agent_*.json")))

    # Build task rows and timeline
    task_rows: list[_TaskRow] = []
    timeline_entries: list[_TimelineEntry] = []
    quality_pass = 0
    quality_fail = 0
    valid_starts = [float(tm["start_time"]) for tm in task_metrics if tm.get("start_time")]
    run_start = min(valid_starts) if valid_starts else 0.0

    for tm in task_metrics:
        title = str(tm.get("task_id", "unknown"))
        role = str(tm.get("role", ""))
        model = str(tm.get("model", ""))
        success = bool(tm.get("success", False))
        janitor = bool(tm.get("janitor_passed", False))
        cost = float(tm.get("cost_usd", 0.0))
        start_time = float(tm.get("start_time", 0.0))
        end_time = float(tm.get("end_time", 0.0))
        dur = end_time - start_time if end_time > start_time else 0.0

        task_rows.append(
            _TaskRow(
                title=title,
                role=role,
                status="done" if success else "failed",
                model=model,
                duration_s=dur,
                cost_usd=cost,
                janitor_passed=janitor,
            )
        )

        if janitor and end_time > 0:
            quality_pass += 1
        elif end_time > 0:
            quality_fail += 1

        if start_time > 0 and run_start > 0:
            timeline_entries.append(
                _TimelineEntry(
                    title=title,
                    start_offset_s=start_time - run_start,
                    end_offset_s=(end_time - run_start) if end_time > 0 else (start_time - run_start),
                )
            )

    # Load cost data
    model_costs: list[_ModelCost] = []
    default_cost_path = metrics_dir / f"costs_{run_id}.json"
    cost_path: Path | None = default_cost_path
    if not default_cost_path.exists():
        fallback = workdir / ".sdd" / "runtime" / "costs" / f"{run_id}.json"
        cost_path = fallback if fallback.exists() else None

    if cost_path:
        cost_data = _load_json(cost_path)
        if cost_data:
            total_cost_usd = float(cost_data.get("total_spent_usd", total_cost_usd))
            for mc in cost_data.get("per_model", []):
                model_costs.append(
                    _ModelCost(
                        model=str(mc.get("model", "")),
                        total_cost_usd=float(mc.get("total_cost_usd", 0.0)),
                        invocation_count=int(mc.get("invocation_count", 0)),
                        total_tokens=int(mc.get("total_tokens", 0)),
                    )
                )

    return _ExportReport(
        goal=goal,
        run_id=run_id,
        duration_s=duration_s,
        total_cost_usd=total_cost_usd,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        agents_spawned=agents_spawned,
        task_rows=task_rows,
        model_costs=model_costs,
        timeline_entries=timeline_entries,
        quality_pass_count=quality_pass,
        quality_fail_count=quality_fail,
    )


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning empty dict on failure."""
    if not path.exists():
        return {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return cast(dict[str, Any], raw)
    return {}


def _detect_latest(sdd: Path) -> str:
    """Find the most recent run ID from ``.sdd/runs/``."""
    runs_dir = sdd / "runs"
    if not runs_dir.is_dir():
        return "unknown"
    run_dirs = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in run_dirs:
        if d.is_dir() and (d / "summary.json").exists():
            return d.name
    return "unknown"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string.

    Returns strings like ``"1h 23m 4s"``, ``"2m 5s"``, or ``"45s"``.
    """
    s = int(seconds)
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_duration_short(seconds: float) -> str:
    """Format seconds into a compact duration string for summaries.

    Returns ``"18 minutes"``, ``"2h 15m"``, etc.
    """
    s = int(seconds)
    if s < 60:
        return f"{s} seconds"
    minutes, _secs = divmod(s, 60)
    hours, remaining_min = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_min}m"
    return f"{remaining_min} minutes"


def _agent_summary(report: _ExportReport) -> str:
    """Build a human-readable agent summary string."""
    model_counts: dict[str, int] = {}
    for row in report.task_rows:
        m = row.model or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1
    parts = [f"{m} x{n}" for m, n in sorted(model_counts.items())]
    return f"{report.agents_spawned} ({', '.join(parts)})"


def _sequential_estimate(report: _ExportReport) -> str:
    """Calculate sequential time estimate from individual task durations."""
    total = sum(row.duration_s for row in report.task_rows)
    return _fmt_duration_short(total)


# ---------------------------------------------------------------------------
# HTML renderer - fully self-contained, inline CSS, no external deps
# ---------------------------------------------------------------------------

_TIMELINE_EMPTY_ROW = '<tr><td colspan="3" style="color:#6c757d;">No timeline data available.</td></tr>'


def _render_html(report: _ExportReport) -> str:
    """Render a self-contained HTML report with inline CSS."""

    # CSS block intentionally on long lines to keep readable; suppress E501.
    css = """\
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 960px; margin: 0 auto; padding: 2rem; color: #1a1a2e; background: #f8f9fa; }
  h1 { color: #16213e; border-bottom: 2px solid #0f3460; padding-bottom: 0.5rem; }
  h2 { color: #0f3460; margin-top: 2rem; }
  .summary { background: #fff; border-radius: 8px; padding: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin: 1rem 0; }
  .summary p { margin: 0.4rem 0; line-height: 1.6; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin: 0.5rem 0; }
  th { background: #0f3460; color: #fff; padding: 0.75rem; text-align: left; font-weight: 600; }
  td { padding: 0.6rem 0.75rem; border-bottom: 1px solid #e9ecef; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f1f3f5; }
  .pass { color: #28a745; font-weight: 600; }
  .fail { color: #dc3545; font-weight: 600; }
  .footer { text-align: center; margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #dee2e6; color: #6c757d; font-size: 0.85rem; }
  .footer a { color: #0f3460; text-decoration: none; }
  .footer a:hover { text-decoration: underline; }
  .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }
  .badge.pass { background: #d4edda; color: #155724; }
  .badge.fail { background: #f8d7da; color: #721c24; }
</style>
"""  # noqa: E501

    date_str = report.run_id[:10] if len(report.run_id) >= 10 else "unknown"
    agent_sum = _agent_summary(report)
    seq_est = _sequential_estimate(report)
    total_gates = report.quality_pass_count + report.quality_fail_count

    rows_html = ""
    for row in report.task_rows:
        status_class = "pass" if row.status == "done" else "fail"
        status_text = "PASS" if row.janitor_passed else "FAIL"
        status_badge = f'<span class="badge {status_class}">{status_text}</span>'
        rows_html += f"""\
<tr>
  <td>{row.title}</td>
  <td>{row.role or "-"}</td>
  <td>{row.status}</td>
  <td>{row.model or "-"}</td>
  <td>{_fmt_duration(row.duration_s)}</td>
  <td>${row.cost_usd:.4f}</td>
  <td>{status_badge}</td>
</tr>\n"""

    cost_rows_html = ""
    for mc in report.model_costs:
        cost_rows_html += f"""\
<tr>
  <td>{mc.model}</td>
  <td>${mc.total_cost_usd:.4f}</td>
  <td>{mc.invocation_count}</td>
  <td>{mc.total_tokens:,}</td>
</tr>\n"""

    agent_rows_html = ""
    model_counts: dict[str, int] = {}
    for row in report.task_rows:
        m = row.model or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1
    for m, n in sorted(model_counts.items()):
        agent_rows_html += f"""\
<tr>
  <td>{m}</td>
  <td>{n}</td>
</tr>\n"""

    timeline_rows_html = ""
    for entry in report.timeline_entries:
        timeline_rows_html += f"""\
<tr>
  <td>{entry.title}</td>
  <td>{entry.start_offset_s:.1f}s</td>
  <td>{entry.end_offset_s:.1f}s</td>
</tr>\n"""

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bernstein Run Report - {date_str}</title>
{css}
</head>
<body>

<h1>Bernstein Run Report - {date_str}</h1>
<p style="color:#6c757d;">Generated by Bernstein v1.7.0</p>

<div class="summary">
  <p><strong>Goal:</strong> {report.goal or "-"}</p>
  <p><strong>Tasks:</strong> {report.tasks_completed} completed, {report.tasks_failed} failed</p>
  <p><strong>Agents:</strong> {agent_sum}</p>
  <p><strong>Duration:</strong> {_fmt_duration_short(report.duration_s)} (sequential estimate: {seq_est})</p>
  <p><strong>Cost:</strong> ${report.total_cost_usd:.2f}</p>
  <p><strong>Quality:</strong> {report.quality_pass_count}/{total_gates} gates passed</p>
</div>

<h2>Task Breakdown</h2>
<table>
<thead>
<tr><th>Task</th><th>Role</th><th>Status</th><th>Model</th><th>Duration</th><th>Cost</th><th>Janitor</th></tr>
</thead>
<tbody>
{rows_html}</tbody>
</table>

<h2>Task Timeline</h2>
<table>
<thead>
<tr><th>Task</th><th>Start (offset)</th><th>End (offset)</th></tr>
</thead>
<tbody>
{timeline_rows_html or _TIMELINE_EMPTY_ROW}</tbody>
</table>

<h2>Quality Gates</h2>
<table>
<thead>
<tr><th>Metric</th><th>Value</th></tr>
</thead>
<tbody>
<tr><td>Passed</td><td class="pass">{report.quality_pass_count}</td></tr>
<tr><td>Failed</td><td class="fail">{report.quality_fail_count}</td></tr>
<tr><td>Total</td><td>{total_gates}</td></tr>
</tbody>
</table>

<h2>Cost Breakdown by Model</h2>
<table>
<thead>
<tr><th>Model</th><th>Cost</th><th>Invocations</th><th>Tokens</th></tr>
</thead>
<tbody>
{cost_rows_html}</tbody>
</table>

<h2>Per-Agent Stats</h2>
<table>
<thead>
<tr><th>Model</th><th>Tasks</th></tr>
</thead>
<tbody>
{agent_rows_html}</tbody>
</table>

<div class="footer">
  <p>Generated by <a href="{REPO_URL}" target="_blank" rel="noopener">Bernstein</a> - Declarative Orchestration</p>
</div>

</body>
</html>
"""
    return html


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_markdown(report: _ExportReport) -> str:
    """Render a Markdown report."""
    lines: list[str] = []
    date_str = report.run_id[:10] if len(report.run_id) >= 10 else "unknown"

    lines.extend((f"# Bernstein Run Report - {date_str}", "", "Generated by Bernstein v1.7.0", ""))
    agent_sum = _agent_summary(report)
    seq_est = _sequential_estimate(report)
    total_gates = report.quality_pass_count + report.quality_fail_count

    if report.goal:
        lines.append(f"**Goal:** {report.goal}")
    lines.extend(
        (
            f"**Tasks:** {report.tasks_completed} completed, {report.tasks_failed} failed",
            f"**Agents:** {agent_sum}",
            f"**Duration:** {_fmt_duration_short(report.duration_s)} (sequential estimate: {seq_est})",
            f"**Cost:** ${report.total_cost_usd:.2f}",
            f"**Quality:** {report.quality_pass_count}/{total_gates} gates passed",
            "",
        )
    )

    # Task breakdown
    lines.extend(("## Task Breakdown", ""))
    if report.task_rows:
        lines.extend(
            (
                "| Task | Role | Status | Model | Duration | Cost | Janitor |",
                "|------|------|--------|-------|----------|------|---------|",
            )
        )
        for row in report.task_rows:
            janitor = "PASS" if row.janitor_passed else "FAIL"
            lines.append(
                f"| {row.title} | {row.role or '-'} | {row.status} "
                f"| {row.model or '-'} | {_fmt_duration(row.duration_s)} "
                f"| ${row.cost_usd:.4f} | {janitor} |"
            )
    else:
        lines.append("No tasks recorded.")
    lines.append("")

    # Task timeline
    lines.extend(("## Task Timeline", ""))
    if report.timeline_entries:
        lines.extend(("| Task | Start (offset) | End (offset) |", "|------|----------------|--------------|"))
        for entry in report.timeline_entries:
            lines.append(f"| {entry.title} | {entry.start_offset_s:.1f}s | {entry.end_offset_s:.1f}s |")
    else:
        lines.append("No timeline data available.")
    lines.append("")

    # Quality gates
    lines.extend(
        (
            "## Quality Gates",
            "",
            f"- **Passed:** {report.quality_pass_count}",
            f"- **Failed:** {report.quality_fail_count}",
            f"- **Total:** {total_gates}",
            "",
        )
    )

    # Cost breakdown
    lines.extend(("## Cost Breakdown by Model", ""))
    if report.model_costs:
        lines.extend(("| Model | Cost | Invocations | Tokens |", "|-------|------|-------------|--------|"))
        for mc in report.model_costs:
            lines.append(f"| {mc.model} | ${mc.total_cost_usd:.4f} | {mc.invocation_count} | {mc.total_tokens:,} |")
    else:
        lines.append("No cost data available.")
    lines.append("")

    # Per-agent stats
    lines.extend(("## Per-Agent Stats", ""))
    model_counts: dict[str, int] = {}
    for row in report.task_rows:
        m = row.model or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1
    if model_counts:
        lines.extend(("| Model | Tasks |", "|-------|-------|"))
        for m, n in sorted(model_counts.items()):
            lines.append(f"| {m} | {n} |")
    else:
        lines.append("No agent data available.")
    lines.extend(("", f"*Generated by [Bernstein]({REPO_URL}) - Declarative Agent Orchestration*"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_run_report(
    workdir: Path,
    run_id: str | None = None,
    fmt: str = "html",
    output_path: str | None = None,
) -> str | Path:
    """Generate a shareable run report.

    Args:
        workdir: Project root containing ``.sdd/``.
        run_id: Specific run to export.  Defaults to latest.
        fmt: Output format - ``"html"`` or ``"md"``.
        output_path: Optional file path.  Defaults to stdout / ``.sdd/reports/``.

    Returns:
        File path if written to disk, otherwise the report string.

    Raises:
        ValueError: If no run data is found.
    """
    fmt = fmt.lower()
    if fmt not in ("html", "md"):
        raise ValueError(f"Unsupported format: {fmt!r}. Use 'html' or 'md'.")

    report = _load_report_data(workdir, run_id)

    if report.run_id == "unknown":
        raise ValueError("No run data found. Has a run completed in this project?")

    raw = _render_html(report) if fmt == "html" else _render_markdown(report)

    # Enforce 500KB size limit
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_FILE_SIZE:
        raise ValueError(
            f"Report exceeds {MAX_FILE_SIZE // 1024}KB size limit "
            f"({len(encoded) // 1024}KB). Truncating is not supported."
        )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw, encoding="utf-8")
        return out

    # Default: write to .sdd/reports/
    sdd_reports = workdir / ".sdd" / "reports"
    sdd_reports.mkdir(parents=True, exist_ok=True)
    suffix = ".html" if fmt == "html" else ".md"
    out = sdd_reports / f"{report.run_id}{suffix}"
    out.write_text(raw, encoding="utf-8")
    return out
