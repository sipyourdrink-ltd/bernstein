"""Run report generation.

Reads metrics from a completed orchestration run and generates a
structured markdown report covering task breakdown, quality gates,
cost analysis, and an ASCII timeline.

Data sources (read-only):
- ``.sdd/metrics/`` — per-task and per-agent metrics (via MetricsCollector)
- ``.sdd/runtime/costs/{run_id}.json`` — token-level cost data (via CostTracker)
- ``.sdd/runs/{run_id}/summary.json`` — high-level run summary
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskRow:
    """One row in the task breakdown table.

    Attributes:
        title: Short task title.
        role: Agent role that executed the task.
        status: ``"done"`` or ``"failed"``.
        model: Model used (e.g. ``"sonnet"``).
        duration_s: Wall-clock seconds the task took.
        cost_usd: Cost in USD for the task.
        janitor_passed: Whether janitor verification passed.
    """

    title: str
    role: str
    status: str
    model: str
    duration_s: float
    cost_usd: float
    janitor_passed: bool


@dataclass
class ModelCost:
    """Per-model cost aggregation.

    Attributes:
        model: Model name.
        total_cost_usd: Total cost attributed to this model.
        invocation_count: Number of invocations.
        total_tokens: Total tokens consumed.
    """

    model: str
    total_cost_usd: float
    invocation_count: int
    total_tokens: int


@dataclass
class TimelineEntry:
    """A task's start/end relative to the run start for the ASCII timeline.

    Attributes:
        title: Short task title.
        start_offset_s: Seconds after run start that the task began.
        end_offset_s: Seconds after run start that the task ended.
    """

    title: str
    start_offset_s: float
    end_offset_s: float


@dataclass
class RunReport:
    """Structured representation of a run report.

    Attributes:
        goal: High-level goal of the run (from summary or empty).
        run_id: Orchestrator run identifier.
        duration_s: Total wall-clock duration in seconds.
        total_cost_usd: Total cost in USD.
        tasks_completed: Number of tasks that succeeded.
        tasks_failed: Number of tasks that failed.
        agents_spawned: Number of agent sessions spawned.
        task_rows: Per-task breakdown rows.
        model_costs: Per-model cost breakdown.
        timeline_entries: Timeline entries for ASCII rendering.
        quality_pass_count: Tasks that passed janitor verification.
        quality_fail_count: Tasks that failed janitor verification.
    """

    goal: str
    run_id: str
    duration_s: float
    total_cost_usd: float
    tasks_completed: int
    tasks_failed: int
    agents_spawned: int
    task_rows: list[TaskRow] = field(default_factory=list[TaskRow])
    model_costs: list[ModelCost] = field(default_factory=list[ModelCost])
    timeline_entries: list[TimelineEntry] = field(default_factory=list[TimelineEntry])
    quality_pass_count: int = 0
    quality_fail_count: int = 0


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class RunReportGenerator:
    """Generates a :class:`RunReport` from data in ``.sdd/``.

    Args:
        workdir: Project root containing the ``.sdd/`` directory.
        run_id: Specific run to report on.  If ``None``, the latest
            run is auto-detected from ``.sdd/runs/``.
    """

    def __init__(self, workdir: Path, run_id: str | None = None) -> None:
        self._workdir = workdir
        self._sdd = workdir / ".sdd"
        self._run_id = run_id or self._detect_latest_run_id()

    # -- public API ---------------------------------------------------------

    def generate(self) -> RunReport:
        """Collect data from ``.sdd/`` and build a :class:`RunReport`.

        Returns:
            Fully populated ``RunReport``.
        """
        summary = self._load_summary()
        task_metrics = self._load_task_metrics()
        agent_metrics = self._load_agent_metrics()
        cost_data = self._load_cost_data()

        run_id = self._run_id
        goal = str(summary.get("goal", ""))
        duration_s = float(summary.get("wall_clock_seconds", 0.0))
        total_cost_usd = float(summary.get("total_cost_usd", 0.0))
        tasks_completed = int(summary.get("tasks_completed", 0))
        tasks_failed = int(summary.get("tasks_failed", 0))
        agents_spawned = len(agent_metrics)

        # If summary had no cost but cost_data does, prefer cost_data.
        if total_cost_usd == 0.0 and cost_data:
            total_cost_usd = float(cost_data.get("total_spent_usd", 0.0))

        # Build task rows and timeline
        task_rows: list[TaskRow] = []
        timeline_entries: list[TimelineEntry] = []
        quality_pass = 0
        quality_fail = 0
        run_start = self._infer_run_start(task_metrics)

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
                TaskRow(
                    title=title,
                    role=role,
                    status="done" if success else "failed",
                    model=model,
                    duration_s=dur,
                    cost_usd=cost,
                    janitor_passed=janitor,
                )
            )

            if end_time > 0:
                if janitor:
                    quality_pass += 1
                else:
                    quality_fail += 1

            if start_time > 0 and run_start > 0:
                timeline_entries.append(
                    TimelineEntry(
                        title=title,
                        start_offset_s=start_time - run_start,
                        end_offset_s=(end_time - run_start) if end_time > 0 else (start_time - run_start),
                    )
                )

        # Build model costs from cost_data
        model_costs: list[ModelCost] = []
        for mc in cost_data.get("per_model", []):
            model_costs.append(
                ModelCost(
                    model=str(mc.get("model", "")),
                    total_cost_usd=float(mc.get("total_cost_usd", 0.0)),
                    invocation_count=int(mc.get("invocation_count", 0)),
                    total_tokens=int(mc.get("total_tokens", 0)),
                )
            )

        return RunReport(
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

    def to_markdown(self, report: RunReport) -> str:
        """Render a :class:`RunReport` as a markdown string.

        Args:
            report: The report to render.

        Returns:
            Multi-line markdown string.
        """
        lines: list[str] = []

        # -- Summary -----------------------------------------------------------
        lines.append("# Run Report")
        lines.append("")
        if report.goal:
            lines.append(f"**Goal:** {report.goal}")
        lines.append(f"**Run ID:** `{report.run_id}`")
        lines.append(f"**Duration:** {_fmt_duration(report.duration_s)}")
        lines.append(f"**Total cost:** ${report.total_cost_usd:.4f}")
        lines.append(f"**Tasks completed:** {report.tasks_completed}")
        lines.append(f"**Tasks failed:** {report.tasks_failed}")
        lines.append(f"**Agents spawned:** {report.agents_spawned}")
        lines.append("")

        # -- Task breakdown ----------------------------------------------------
        lines.append("## Task Breakdown")
        lines.append("")
        if report.task_rows:
            lines.append("| Task | Role | Status | Model | Duration | Cost |")
            lines.append("|------|------|--------|-------|----------|------|")
            for row in report.task_rows:
                status_icon = "pass" if row.status == "done" else "FAIL"
                lines.append(
                    f"| {row.title} | {row.role} | {status_icon} "
                    f"| {row.model} | {_fmt_duration(row.duration_s)} "
                    f"| ${row.cost_usd:.4f} |"
                )
        else:
            lines.append("No tasks recorded.")
        lines.append("")

        # -- Quality gates -----------------------------------------------------
        lines.append("## Quality Gates")
        lines.append("")
        total_verified = report.quality_pass_count + report.quality_fail_count
        if total_verified > 0:
            pass_rate = report.quality_pass_count / total_verified * 100
            lines.append(f"- **Janitor pass rate:** {pass_rate:.0f}% ({report.quality_pass_count}/{total_verified})")
            lines.append(f"- **Passed:** {report.quality_pass_count}")
            lines.append(f"- **Failed:** {report.quality_fail_count}")
        else:
            lines.append("No quality gate data available.")
        lines.append("")

        # -- Cost analysis -----------------------------------------------------
        lines.append("## Cost Analysis")
        lines.append("")
        if report.model_costs:
            lines.append("| Model | Cost | Invocations | Tokens |")
            lines.append("|-------|------|-------------|--------|")
            for mc in report.model_costs:
                lines.append(f"| {mc.model} | ${mc.total_cost_usd:.4f} | {mc.invocation_count} | {mc.total_tokens:,} |")
            lines.append("")
            # Most expensive task
            if report.task_rows:
                most_expensive = max(report.task_rows, key=lambda r: r.cost_usd)
                lines.append(f"**Most expensive task:** {most_expensive.title} (${most_expensive.cost_usd:.4f})")
        else:
            lines.append("No cost data available.")
        lines.append("")

        # -- Timeline ----------------------------------------------------------
        lines.append("## Timeline")
        lines.append("")
        if report.timeline_entries:
            lines.append(_render_ascii_timeline(report.timeline_entries, report.duration_s))
        else:
            lines.append("No timeline data available.")
        lines.append("")

        return "\n".join(lines)

    def save(self, report: RunReport, path: Path | None = None) -> Path:
        """Write the markdown report to disk.

        Args:
            report: The report to save.
            path: Explicit output path.  Defaults to
                ``.sdd/reports/{run_id}.md``.

        Returns:
            Path where the report was written.
        """
        if path is None:
            reports_dir = self._sdd / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = reports_dir / f"{report.run_id}.md"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

        md = self.to_markdown(report)
        path.write_text(md, encoding="utf-8")
        logger.info("Run report written to %s", path)
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
        """Load per-task metrics from ``.sdd/metrics/task_*.json``.

        Falls back to scanning JSONL metric files for task_completion_time
        entries when structured per-task files are not available.
        """
        metrics_dir = self._sdd / "metrics"
        if not metrics_dir.is_dir():
            return []

        # Try structured per-task metric files first
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

        # Fallback: scan JSONL files for task_completion_time entries
        for f in sorted(metrics_dir.glob("*.jsonl")):
            try:
                for raw_line in f.read_text(encoding="utf-8").splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    entry_raw: Any = json.loads(raw_line)
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
                            "start_time": 0.0,
                            "end_time": float(entry.get("timestamp", 0.0)),
                            "cost_usd": 0.0,
                            "janitor_passed": False,
                        }
                    )
            except (OSError, json.JSONDecodeError):
                continue
        return results

    def _load_agent_metrics(self) -> list[dict[str, Any]]:
        """Load per-agent metrics from ``.sdd/metrics/agent_*.json``.

        Falls back to scanning JSONL for agent_success entries.
        """
        metrics_dir = self._sdd / "metrics"
        if not metrics_dir.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for f in sorted(metrics_dir.glob("agent_*.json")):
            try:
                raw: Any = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    results.append(cast("dict[str, Any]", raw))
            except (OSError, json.JSONDecodeError):
                continue
        if results:
            return results

        # Fallback: count distinct agent_id from JSONL agent_success entries
        seen_agents: set[str] = set()
        for f in sorted(metrics_dir.glob("*.jsonl")):
            try:
                for raw_line in f.read_text(encoding="utf-8").splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    entry_raw2: Any = json.loads(raw_line)
                    if not isinstance(entry_raw2, dict):
                        continue
                    entry2 = cast("dict[str, Any]", entry_raw2)
                    if entry2.get("metric_type") != "agent_success":
                        continue
                    labels: dict[str, Any] = entry2.get("labels") or {}
                    aid = str(labels.get("agent_id", ""))
                    if aid and aid not in seen_agents:
                        seen_agents.add(aid)
                        results.append({"agent_id": aid})
            except (OSError, json.JSONDecodeError):
                continue
        return results

    def _load_cost_data(self) -> dict[str, Any]:
        """Load cost report from ``.sdd/metrics/costs_{run_id}.json``."""
        cost_path = self._sdd / "metrics" / f"costs_{self._run_id}.json"
        if not cost_path.exists():
            # Fallback: try runtime costs
            cost_path = self._sdd / "runtime" / "costs" / f"{self._run_id}.json"
        if not cost_path.exists():
            return {}
        try:
            raw: Any = json.loads(cost_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast("dict[str, Any]", raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load cost data: %s", exc)
        return {}

    @staticmethod
    def _infer_run_start(task_metrics: list[dict[str, Any]]) -> float:
        """Infer the run start time as the earliest task start_time."""
        starts = [float(tm.get("start_time", 0.0)) for tm in task_metrics if float(tm.get("start_time", 0.0)) > 0]
        return min(starts) if starts else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string.

    Args:
        seconds: Duration in seconds.

    Returns:
        String like ``"1h 23m 4s"`` or ``"45s"``.
    """
    s = int(seconds)
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _render_ascii_timeline(entries: list[TimelineEntry], total_duration_s: float) -> str:
    """Render an ASCII timeline of task execution.

    Each task is shown as a bar ``[====]`` positioned proportionally
    within a fixed-width track.

    Args:
        entries: Timeline entries with start/end offsets.
        total_duration_s: Total run duration for scaling.

    Returns:
        Multi-line string with the ASCII timeline.
    """
    if total_duration_s <= 0:
        return "No timeline data (zero duration)."

    width = 60  # characters for the timeline bar
    lines: list[str] = []
    lines.append("```")

    # Determine max title length for alignment
    max_title = max((len(e.title[:20]) for e in entries), default=0)
    pad = max(max_title, 12)

    # Header
    lines.append(f"{'Task':<{pad}}  |{'=' * width}|")

    for entry in sorted(entries, key=lambda e: e.start_offset_s):
        start_frac = entry.start_offset_s / total_duration_s
        end_frac = entry.end_offset_s / total_duration_s
        start_col = int(start_frac * width)
        end_col = int(end_frac * width)
        # Ensure at least 1-char bar
        end_col = max(end_col, start_col + 1)
        # Clamp
        start_col = max(0, min(start_col, width - 1))
        end_col = max(start_col + 1, min(end_col, width))

        bar = " " * start_col + "#" * (end_col - start_col) + " " * (width - end_col)
        title = entry.title[:20]
        lines.append(f"{title:<{pad}}  |{bar}|")

    lines.append(f"{'':>{pad}}   0s{'':>{width - 10}}{_fmt_duration(total_duration_s):>6}")
    lines.append("```")
    return "\n".join(lines)
