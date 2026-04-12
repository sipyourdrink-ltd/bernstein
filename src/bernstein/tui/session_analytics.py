"""Session analytics — analyze task traces for insights.

Parses .sdd/traces/*.jsonl files to extract session metadata and facets:
- Goal: What the agent was trying to accomplish
- Category: Type of task (bug fix, feature, refactor, etc.)
- Agent helpfulness: How effective the agent was

Generates summary reports in .sdd/reports/.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    """Extracted session metadata from a trace.

    Attributes:
        trace_id: Unique trace ID.
        session_id: Agent session ID.
        task_ids: Tasks handled in this session.
        agent_role: Role of the agent.
        model: Model used.
        start_time: Session start time (UTC).
        end_time: Session end time (UTC, None if still running).
        duration_seconds: Total session duration.
        outcome: Final outcome (success/failed/unknown).
        goal: Inferred goal from task descriptions.
        category: Inferred task category.
        steps_count: Number of decision steps.
        edits_made: Number of edit steps.
        verifications: Number of verification steps.
        files_touched: Unique files modified.
        tokens_used: Total tokens consumed.
        helpfulness_score: 0-100 score for agent effectiveness.
    """

    trace_id: str
    session_id: str
    task_ids: list[str]
    agent_role: str
    model: str
    start_time: datetime
    end_time: datetime | None
    duration_seconds: float
    outcome: str
    goal: str
    category: str
    steps_count: int
    edits_made: int
    verifications: int
    files_touched: list[str]
    tokens_used: int
    helpfulness_score: int


@dataclass
class AnalyticsReport:
    """Summary report from session analytics.

    Attributes:
        generated_at: When the report was generated.
        total_sessions: Total sessions analyzed.
        successful_sessions: Sessions with success outcome.
        failed_sessions: Sessions with failed outcome.
        avg_duration_seconds: Average session duration.
        avg_helpfulness: Average helpfulness score.
        category_breakdown: Count of sessions per category.
        role_breakdown: Count of sessions per role.
        model_breakdown: Count of sessions per model.
        top_goals: Most common goals.
        sessions: Individual session metadata.
    """

    generated_at: datetime
    total_sessions: int
    successful_sessions: int
    failed_sessions: int
    avg_duration_seconds: float
    avg_helpfulness: float
    category_breakdown: dict[str, int]
    role_breakdown: dict[str, int]
    model_breakdown: dict[str, int]
    top_goals: list[tuple[str, int]]
    sessions: list[SessionMeta]


# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

CATEGORY_PATTERNS: dict[str, list[str]] = {
    "bug_fix": ["fix", "bug", "error", "crash", "broken", "issue", "defect"],
    "feature": ["add", "feat", "implement", "create", "new", "support"],
    "refactor": ["refactor", "restructure", "reorganize", "clean", "simplify"],
    "test": ["test", "spec", "coverage", "assert", "verify"],
    "docs": ["doc", "readme", "comment", "explain", "document"],
    "config": ["config", "setting", "option", "env", "parameter"],
    "ci": ["ci", "pipeline", "build", "deploy", "workflow", "action"],
}


def _infer_category(detail: str) -> str:
    """Infer task category from description text.

    Args:
        detail: Task detail text.

    Returns:
        Inferred category string.
    """
    text = detail.lower()
    for category, keywords in CATEGORY_PATTERNS.items():
        if any(kw in text for kw in keywords):
            return category
    return "other"


def _extract_goal(task_snapshots: list[dict[str, Any]]) -> str:
    """Extract goal from task snapshots.

    Args:
        task_snapshots: List of serialized task dicts.

    Returns:
        Extracted goal string.
    """
    if not task_snapshots:
        return "(unknown)"

    first = task_snapshots[0]
    return first.get("title", first.get("description", "(unknown)"))[:200]


# ---------------------------------------------------------------------------
# Helpfulness scoring
# ---------------------------------------------------------------------------


def _calculate_helpfulness(
    outcome: str,
    edits_made: int,
    verifications: int,
    steps_count: int,
    duration_seconds: float,
) -> int:
    """Calculate agent helpfulness score (0-100).

    Higher score = more effective agent.

    Args:
        outcome: Final outcome (success/failed/unknown).
        edits_made: Number of edit steps.
        verifications: Number of verification steps.
        steps_count: Total number of steps.
        duration_seconds: Session duration.

    Returns:
        Score from 0 to 100.
    """
    score = 50  # Base score

    # Success bonus
    if outcome == "success":
        score += 30
    elif outcome == "failed":
        score -= 30

    # Verification ratio (agents that verify are more thorough)
    if steps_count > 0:
        verify_ratio = verifications / steps_count
        if verify_ratio > 0.1:
            score += 10
        elif verify_ratio > 0.05:
            score += 5

    # Edit efficiency (reasonable edit count)
    if edits_made > 0:
        if edits_made <= 10:
            score += 10
        elif edits_made <= 20:
            score += 5
        else:
            score -= 5  # Too many edits

    # Duration penalty (very long sessions may indicate struggles)
    if duration_seconds > 3600:  # > 1 hour
        score -= 10
    elif duration_seconds < 60:  # < 1 minute (too fast, maybe failed)
        score -= 5

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Trace parsing
# ---------------------------------------------------------------------------


def parse_trace(trace_path: Path) -> SessionMeta | None:
    """Parse a JSONL trace file into a SessionMeta.

    Args:
        trace_path: Path to the .jsonl trace file.

    Returns:
        SessionMeta if parsing succeeds, None otherwise.
    """
    if not trace_path.exists():
        return None

    try:
        content = trace_path.read_text(encoding="utf-8")
        lines = [json.loads(line) for line in content.strip().splitlines() if line.strip()]

        if not lines:
            return None

        # First line is usually the trace header
        header = lines[0]
        steps_data = lines[1:] if len(lines) > 1 else []

        # Extract header fields
        trace_id = header.get("trace_id", trace_path.stem)
        session_id = header.get("session_id", trace_path.stem)
        task_ids = header.get("task_ids", [])
        agent_role = header.get("agent_role", "unknown")
        model = header.get("model", "unknown")
        outcome = header.get("outcome", "unknown")
        task_snapshots = header.get("task_snapshots", [])

        # Parse timestamps
        spawn_ts = header.get("spawn_ts", 0)
        end_ts = header.get("end_ts")
        start_time = datetime.fromtimestamp(spawn_ts, tz=UTC)
        end_time = datetime.fromtimestamp(end_ts, tz=UTC) if end_ts else None
        duration_seconds = (end_ts - spawn_ts) if end_ts else 0

        # Count step types
        edits_made = 0
        verifications = 0
        files_touched: set[str] = set()
        tokens_used = 0

        for step in steps_data:
            step_type = step.get("type", "")
            if step_type == "edit":
                edits_made += 1
            elif step_type == "verify":
                verifications += 1

            files = step.get("files", [])
            files_touched.update(files)

            tokens_used += step.get("tokens", 0)

        # Infer goal and category
        goal = _extract_goal(task_snapshots)
        detail_text = " ".join(s.get("detail", "") for s in steps_data)
        category = _infer_category(f"{goal} {detail_text}")

        # Calculate helpfulness
        helpfulness_score = _calculate_helpfulness(
            outcome,
            edits_made,
            verifications,
            len(steps_data),
            duration_seconds,
        )

        return SessionMeta(
            trace_id=trace_id,
            session_id=session_id,
            task_ids=task_ids,
            agent_role=agent_role,
            model=model,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration_seconds,
            outcome=outcome,
            goal=goal,
            category=category,
            steps_count=len(steps_data),
            edits_made=edits_made,
            verifications=verifications,
            files_touched=sorted(files_touched),
            tokens_used=tokens_used,
            helpfulness_score=helpfulness_score,
        )

    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.warning("Failed to parse trace %s: %s", trace_path, exc)
        return None


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def analyze_traces(traces_dir: Path | None = None) -> AnalyticsReport:
    """Analyze all traces and generate a summary report.

    Args:
        traces_dir: Directory containing .jsonl trace files.
                   If None, uses .sdd/traces/.

    Returns:
        AnalyticsReport with aggregated data.
    """
    if traces_dir is None:
        traces_dir = Path.cwd() / ".sdd" / "traces"

    sessions: list[SessionMeta] = []

    if traces_dir.exists():
        for trace_file in traces_dir.glob("*.jsonl"):
            meta = parse_trace(trace_file)
            if meta:
                sessions.append(meta)

    now = datetime.now(tz=UTC)

    if not sessions:
        return AnalyticsReport(
            generated_at=now,
            total_sessions=0,
            successful_sessions=0,
            failed_sessions=0,
            avg_duration_seconds=0.0,
            avg_helpfulness=0.0,
            category_breakdown={},
            role_breakdown={},
            model_breakdown={},
            top_goals=[],
            sessions=[],
        )

    # Aggregate metrics
    successful = sum(1 for s in sessions if s.outcome == "success")
    failed = sum(1 for s in sessions if s.outcome == "failed")
    total_duration = sum(s.duration_seconds for s in sessions)
    total_helpfulness = sum(s.helpfulness_score for s in sessions)

    # Breakdowns
    category_breakdown: dict[str, int] = {}
    role_breakdown: dict[str, int] = {}
    model_breakdown: dict[str, int] = {}
    goal_counts: dict[str, int] = {}

    for s in sessions:
        category_breakdown[s.category] = category_breakdown.get(s.category, 0) + 1
        role_breakdown[s.agent_role] = role_breakdown.get(s.agent_role, 0) + 1
        model_breakdown[s.model] = model_breakdown.get(s.model, 0) + 1

        # Normalize goal for counting
        goal_key = s.goal[:50]
        goal_counts[goal_key] = goal_counts.get(goal_key, 0) + 1

    top_goals = sorted(goal_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return AnalyticsReport(
        generated_at=now,
        total_sessions=len(sessions),
        successful_sessions=successful,
        failed_sessions=failed,
        avg_duration_seconds=total_duration / len(sessions),
        avg_helpfulness=total_helpfulness / len(sessions),
        category_breakdown=category_breakdown,
        role_breakdown=role_breakdown,
        model_breakdown=model_breakdown,
        top_goals=top_goals,
        sessions=sessions,
    )


def format_report(report: AnalyticsReport) -> str:
    """Format an analytics report as a readable string.

    Args:
        report: The analytics report to format.

    Returns:
        Formatted report string.
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("SESSION ANALYTICS REPORT")
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append("=" * 60)
    lines.append("")

    lines.append("SUMMARY")
    lines.append("-" * 30)
    lines.append(f"  Total sessions:      {report.total_sessions}")
    lines.append(f"  Successful:          {report.successful_sessions}")
    lines.append(f"  Failed:              {report.failed_sessions}")
    if report.total_sessions > 0:
        success_rate = report.successful_sessions / report.total_sessions * 100
        lines.append(f"  Success rate:        {success_rate:.1f}%")
    lines.append(f"  Avg duration:        {report.avg_duration_seconds:.0f}s")
    lines.append(f"  Avg helpfulness:     {report.avg_helpfulness:.1f}/100")
    lines.append("")

    if report.category_breakdown:
        lines.append("TASK CATEGORIES")
        lines.append("-" * 30)
        for cat, count in sorted(report.category_breakdown.items(), key=lambda x: x[1], reverse=True):
            bar = "█" * count
            lines.append(f"  {cat:15s} {count:4d} {bar}")
        lines.append("")

    if report.role_breakdown:
        lines.append("AGENT ROLES")
        lines.append("-" * 30)
        for role, count in sorted(report.role_breakdown.items(), key=lambda x: x[1], reverse=True):
            bar = "█" * count
            lines.append(f"  {role:15s} {count:4d} {bar}")
        lines.append("")

    if report.model_breakdown:
        lines.append("MODELS")
        lines.append("-" * 30)
        for model, count in sorted(report.model_breakdown.items(), key=lambda x: x[1], reverse=True):
            bar = "█" * count
            lines.append(f"  {model:15s} {count:4d} {bar}")
        lines.append("")

    if report.top_goals:
        lines.append("TOP GOALS")
        lines.append("-" * 30)
        for goal, count in report.top_goals[:5]:
            lines.append(f"  [{count}x] {goal}")
        lines.append("")

    return "\n".join(lines)


def save_report(
    report: AnalyticsReport,
    reports_dir: Path | None = None,
) -> Path:
    """Save an analytics report to .sdd/reports/.

    Args:
        report: The analytics report to save.
        reports_dir: Directory to save reports. If None, uses .sdd/reports/.

    Returns:
        Path to the saved report file.
    """
    if reports_dir is None:
        reports_dir = Path.cwd() / ".sdd" / "reports"

    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = report.generated_at.strftime("%Y%m%d_%H%M%S")
    filename = f"session_analytics_{timestamp}.md"
    report_path = reports_dir / filename

    formatted = format_report(report)
    report_path.write_text(formatted, encoding="utf-8")

    return report_path
