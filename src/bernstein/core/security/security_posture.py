"""SEC-019: Security posture scoring per run.

Computes a weighted security score from permission usage, secret detection,
sandbox compliance, audit integrity, and policy compliance metrics collected
during an orchestration run.  Produces a letter-graded ``PostureReport``
suitable for operator dashboards and CI gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from typing import Literal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric weights
# ---------------------------------------------------------------------------

METRIC_WEIGHTS: dict[str, float] = {
    "permissions": 0.25,
    "secrets": 0.20,
    "sandbox": 0.20,
    "audit_integrity": 0.15,
    "policy_compliance": 0.20,
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityMetric:
    """A single scored security metric.

    Attributes:
        name: Machine-readable metric name (e.g. ``permissions``).
        score: Numeric score in the range ``[0, 100]``.
        weight: Weight used when computing the overall posture score.
        details: Human-readable explanation of how the score was derived.
    """

    name: str
    score: float
    weight: float
    details: str


@dataclass(frozen=True)
class PostureReport:
    """Aggregate security posture report for a single run.

    Attributes:
        run_id: Identifier of the orchestration run.
        overall_score: Weighted average score ``[0, 100]``.
        grade: Letter grade derived from *overall_score*.
        metrics: Individual metric scores that fed into the aggregate.
        generated_at: ISO-8601 timestamp of report generation.
        recommendations: Actionable suggestions for improving posture.
    """

    run_id: str
    overall_score: float
    grade: Literal["A", "B", "C", "D", "F"]
    metrics: list[SecurityMetric]
    generated_at: str
    recommendations: list[str]


# ---------------------------------------------------------------------------
# Individual metric scorers
# ---------------------------------------------------------------------------


def score_permissions(
    denied: int,
    escalated: int,
    total: int,
) -> SecurityMetric:
    """Score permission usage.

    Higher score when fewer requests were escalated relative to total.
    Denied requests are expected (good security hygiene) and do not penalise.

    Args:
        denied: Number of permission requests that were denied.
        escalated: Number of permission requests that were escalated.
        total: Total permission requests observed.
    """
    if total == 0:
        score = 100.0
        details = "No permission requests observed."
    else:
        escalation_rate = escalated / total
        score = max(0.0, 100.0 - escalation_rate * 100.0)
        details = f"{escalated}/{total} escalated, {denied}/{total} denied (escalation rate {escalation_rate:.0%})."
    return SecurityMetric(
        name="permissions",
        score=round(score, 2),
        weight=METRIC_WEIGHTS["permissions"],
        details=details,
    )


def score_secrets(detected: int, blocked: int) -> SecurityMetric:
    """Score secret handling.

    Perfect score when every detected secret was blocked.  Unblocked secrets
    penalise the score proportionally.

    Args:
        detected: Total secrets detected during the run.
        blocked: Secrets that were successfully blocked from leaking.
    """
    if detected == 0:
        score = 100.0
        details = "No secrets detected."
    else:
        blocked_rate = blocked / detected
        score = blocked_rate * 100.0
        unblocked = detected - blocked
        details = f"{blocked}/{detected} blocked, {unblocked} leaked (block rate {blocked_rate:.0%})."
    return SecurityMetric(
        name="secrets",
        score=round(score, 2),
        weight=METRIC_WEIGHTS["secrets"],
        details=details,
    )


def score_sandbox(violations: int) -> SecurityMetric:
    """Score sandbox compliance.

    Starts at 100 and loses 20 points per violation, floored at 0.

    Args:
        violations: Number of sandbox escape attempts or violations.
    """
    score = max(0.0, 100.0 - 20.0 * violations)
    details = "No sandbox violations." if violations == 0 else f"{violations} violation(s) detected (-20 each)."
    return SecurityMetric(
        name="sandbox",
        score=round(score, 2),
        weight=METRIC_WEIGHTS["sandbox"],
        details=details,
    )


def score_audit_integrity(verified: bool, gaps: int) -> SecurityMetric:
    """Score audit log integrity.

    Full marks when the log is verified with zero gaps.  Unverified logs
    receive a 50-point penalty; each gap costs 10 points.

    Args:
        verified: Whether the audit log passed integrity verification.
        gaps: Number of gaps or missing entries found in the audit trail.
    """
    score = 100.0
    parts: list[str] = []
    if not verified:
        score -= 50.0
        parts.append("integrity check failed (-50)")
    if gaps > 0:
        score -= 10.0 * gaps
        parts.append(f"{gaps} gap(s) (-10 each)")
    score = max(0.0, score)
    details = "; ".join(parts) if parts else "Audit log verified, no gaps."
    return SecurityMetric(
        name="audit_integrity",
        score=round(score, 2),
        weight=METRIC_WEIGHTS["audit_integrity"],
        details=details,
    )


def score_policy_compliance(passed: int, total: int) -> SecurityMetric:
    """Score policy compliance.

    Ratio of passed checks to total checks, expressed as a percentage.

    Args:
        passed: Number of policy checks that passed.
        total: Total number of policy checks evaluated.
    """
    if total == 0:
        score = 100.0
        details = "No policy checks configured."
    else:
        score = (passed / total) * 100.0
        details = f"{passed}/{total} checks passed ({score:.0f}%)."
    return SecurityMetric(
        name="policy_compliance",
        score=round(score, 2),
        weight=METRIC_WEIGHTS["policy_compliance"],
        details=details,
    )


# ---------------------------------------------------------------------------
# Aggregate computation
# ---------------------------------------------------------------------------

_GRADE_THRESHOLDS: list[tuple[float, Literal["A", "B", "C", "D", "F"]]] = [
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
]


def _assign_grade(score: float) -> Literal["A", "B", "C", "D", "F"]:
    """Map a numeric score to a letter grade."""
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def _build_recommendations(metrics: list[SecurityMetric]) -> list[str]:
    """Generate recommendations for metrics scoring below 80."""
    recs: list[str] = []
    for m in metrics:
        if m.score >= 80.0:
            continue
        if m.name == "permissions":
            recs.append("Reduce permission escalations by tightening agent scopes.")
        elif m.name == "secrets":
            recs.append("Ensure all detected secrets are blocked before they leak.")
        elif m.name == "sandbox":
            recs.append("Investigate sandbox violations and harden isolation.")
        elif m.name == "audit_integrity":
            recs.append("Repair audit log gaps and re-verify integrity chain.")
        elif m.name == "policy_compliance":
            recs.append("Review and fix failing policy checks.")
    return recs


def compute_posture(
    run_id: str,
    *,
    permissions: SecurityMetric,
    secrets: SecurityMetric,
    sandbox: SecurityMetric,
    audit: SecurityMetric,
    policy: SecurityMetric,
) -> PostureReport:
    """Compute the aggregate security posture for a run.

    Args:
        run_id: Identifier of the orchestration run.
        permissions: Metric from :func:`score_permissions`.
        secrets: Metric from :func:`score_secrets`.
        sandbox: Metric from :func:`score_sandbox`.
        audit: Metric from :func:`score_audit_integrity`.
        policy: Metric from :func:`score_policy_compliance`.

    Returns:
        A fully populated :class:`PostureReport`.
    """
    metrics = [permissions, secrets, sandbox, audit, policy]
    total_weight = sum(m.weight for m in metrics)
    overall = 0.0 if total_weight == 0 else sum(m.score * m.weight for m in metrics) / total_weight
    overall = round(overall, 2)
    grade = _assign_grade(overall)
    recommendations = _build_recommendations(metrics)
    return PostureReport(
        run_id=run_id,
        overall_score=overall,
        grade=grade,
        metrics=metrics,
        generated_at=datetime.now(tz=UTC).isoformat(),
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Rich-formatted output
# ---------------------------------------------------------------------------


def format_posture_report(report: PostureReport) -> str:
    """Render a posture report as a Rich-formatted string.

    Args:
        report: The posture report to format.

    Returns:
        A string containing the rendered Rich output.
    """
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=88)

    # Header
    grade_colours: dict[str, str] = {
        "A": "green",
        "B": "blue",
        "C": "yellow",
        "D": "dark_orange",
        "F": "red",
    }
    colour = grade_colours.get(report.grade, "white")
    header = Text.assemble(
        ("Security Posture: ", "bold"),
        (f"{report.overall_score:.1f}/100 ", "bold"),
        (f"[{report.grade}]", f"bold {colour}"),
    )
    console.print(header)
    console.print(f"Run: {report.run_id}  |  {report.generated_at}\n")

    # Metrics table
    table = Table(title="Metrics", expand=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Details")
    for m in report.metrics:
        score_style = "green" if m.score >= 80 else ("yellow" if m.score >= 60 else "red")
        table.add_row(
            m.name,
            f"[{score_style}]{m.score:.1f}[/{score_style}]",
            f"{m.weight:.2f}",
            m.details,
        )
    console.print(table)

    # Recommendations
    if report.recommendations:
        rec_text = "\n".join(f"  - {r}" for r in report.recommendations)
        console.print(Panel(rec_text, title="Recommendations", border_style="yellow"))

    return buf.getvalue()
