"""Security posture scoring.

Two projections live here.

:func:`compute_posture` (SEC-019) grades a single orchestration run from the
permission, secret, sandbox, audit and policy metrics that run collected.

:func:`compute_evidenced_posture` (issue #4989) answers the other question an
operator asks -- "how is this install doing" -- and answers it *only* from
facts the chain evidences. It consumes the per-control coverage report
(:func:`bernstein.core.compliance.coverage.assess_control_coverage`) and derives
nothing itself, so the number moves when the install produces evidence and does
not move when someone switches a control on. A control that is enabled but
produced no chain event contributes nothing.

The document names every input: the control, its weight, whether the chain
evidenced it, and the content hash of each chain event that did. It carries the
version of the weight table that produced it, because a score without its
weights cannot be recomputed. It carries its own denominator -- the weight that
was *measurable*, not the weight that exists -- so a high score over a small
surface reads as a high score over a small surface.

Determinism: the document is canonical JSON (sorted keys, minimal separators)
and every field is a pure function of the chain, so a third party recomputing
from the same entries gets byte-identical bytes and the same HMAC.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING, Any, Literal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bernstein.core.compliance.coverage import (
    ControlCoverageResult,
    ControlCoverageStatus,
    assess_control_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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
        match m.name:
            case "permissions":
                recs.append("Reduce permission escalations by tightening agent scopes.")
            case "secrets":
                recs.append("Ensure all detected secrets are blocked before they leak.")
            case "sandbox":
                recs.append("Investigate sandbox violations and harden isolation.")
            case "audit_integrity":
                recs.append("Repair audit log gaps and re-verify integrity chain.")
            case "policy_compliance":
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
        if m.score >= 80:
            score_style = "green"
        elif m.score >= 60:
            score_style = "yellow"
        else:
            score_style = "red"
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


# ---------------------------------------------------------------------------
# Evidenced posture (issue #4989): a score derived only from chain evidence
# ---------------------------------------------------------------------------

#: Shape version of the evidenced-posture document. Bump only on a shape change.
EVIDENCED_POSTURE_VERSION = 1

#: Version of the weight table below. It travels in every document: a score
#: read without the weights that produced it cannot be recomputed, and two
#: installs on different weight tables are not comparable numbers.
POSTURE_WEIGHTS_VERSION = "evidenced-posture-weights/1"

#: Per-control weights. Data, not code -- a control is a row, and re-weighting
#: is an edit here plus a bump of POSTURE_WEIGHTS_VERSION.
CONTROL_WEIGHTS: dict[str, float] = {
    "eu-ai-act-art-12": 1.0,
    "eu-ai-act-art-14": 1.0,
    "eu-ai-act-art-73": 1.0,
    "soc2-cc6-1": 1.0,
    "soc2-cc8-1": 1.0,
}

#: Weight for a measurable control the table does not name. Uniform rather than
#: zero: a control the chain can evidence but the table forgot must still show
#: up in the denominator, or adding a control would silently raise the score.
DEFAULT_CONTROL_WEIGHT = 1.0

#: Decimal places the score is rounded to, fixed so two surfaces computing the
#: same fraction serialise the same digits.
_SCORE_PRECISION = 6


@dataclass(frozen=True, slots=True)
class PostureContribution:
    """One control's contribution to the score, and the events behind it."""

    policy_id: str
    control_id: str
    weight: float
    evidenced: bool
    chain_events: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "control_id": self.control_id,
            "weight": self.weight,
            "evidenced": self.evidenced,
            "chain_events": list(self.chain_events),
        }


@dataclass(frozen=True, slots=True)
class EvidencedPosture:
    """A posture score and every input that produced it.

    Attributes:
        score: ``evidenced_weight / measurable_weight`` as a percentage, or
            ``None`` when nothing was measurable. Zero over zero is absent
            evidence; rendering it as ``0`` or ``100`` is how a console comes
            to claim what it cannot show.
        weights_version: The weight table that produced *score*.
        evidenced_weight: Numerator -- weight of the controls the chain evidences.
        measurable_weight: Denominator -- weight of the controls that were
            measurable at all.
        measurable_controls: Count behind *measurable_weight*.
        registered_controls: Controls the coverage report considered, measurable
            or not. Read against *measurable_controls* it says how much of the
            surface the score is silent about.
        contributions: One row per measurable control, sorted by control id.
    """

    score: float | None
    weights_version: str
    evidenced_weight: float
    measurable_weight: float
    measurable_controls: int
    registered_controls: int
    contributions: tuple[PostureContribution, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the unsigned canonical payload."""
        return {
            "v": EVIDENCED_POSTURE_VERSION,
            "score": self.score,
            "weights_version": self.weights_version,
            "evidenced_weight": self.evidenced_weight,
            "measurable_weight": self.measurable_weight,
            "measurable_controls": self.measurable_controls,
            "registered_controls": self.registered_controls,
            "contributions": [c.to_dict() for c in self.contributions],
        }


def compute_evidenced_posture(coverage: Sequence[ControlCoverageResult]) -> EvidencedPosture:
    """Score *coverage* using only the controls the chain could speak to.

    A control the install cannot evidence at all
    (:attr:`~bernstein.core.compliance.coverage.ControlCoverageStatus.NOT_EVIDENCEABLE`)
    is held out of both numerator and denominator: scoring it would be scoring
    the absence of a measurement. Every other control is in the denominator,
    and only a control with chain evidence is in the numerator.

    Args:
        coverage: Per-control coverage results, as returned by
            :func:`~bernstein.core.compliance.coverage.assess_control_coverage`.

    Returns:
        The :class:`EvidencedPosture` for that report.
    """
    contributions: list[PostureContribution] = []
    evidenced_weight = 0.0
    measurable_weight = 0.0

    for result in sorted(coverage, key=lambda r: (r.control_id, r.policy_id)):
        if result.status is ControlCoverageStatus.NOT_EVIDENCEABLE:
            continue
        weight = CONTROL_WEIGHTS.get(result.policy_id, DEFAULT_CONTROL_WEIGHT)
        evidenced = result.status is ControlCoverageStatus.EVIDENCED
        measurable_weight += weight
        if evidenced:
            evidenced_weight += weight
        contributions.append(
            PostureContribution(
                policy_id=result.policy_id,
                control_id=result.control_id,
                weight=weight,
                evidenced=evidenced,
                # Only an evidenced control names events. A partially evidenced
                # one has no event satisfying the required behaviour, so naming
                # anything there would name evidence for a different control.
                chain_events=result.evidence_refs if evidenced else (),
            )
        )

    score = None if measurable_weight == 0 else round(evidenced_weight / measurable_weight * 100.0, _SCORE_PRECISION)

    return EvidencedPosture(
        score=score,
        weights_version=POSTURE_WEIGHTS_VERSION,
        evidenced_weight=evidenced_weight,
        measurable_weight=measurable_weight,
        measurable_controls=len(contributions),
        registered_controls=len(coverage),
        contributions=tuple(contributions),
    )


def collect_evidenced_posture(workdir: Path) -> EvidencedPosture:
    """Project the evidenced posture of the install rooted at *workdir*.

    Reads ``<workdir>/.sdd/lineage`` and nothing else. No configuration file is
    consulted, so enabling a control cannot move the number.
    """
    from bernstein.core.lineage.store import LineageStore

    store = LineageStore(workdir / ".sdd" / "lineage")
    entries = [entry for entry, _ in store.read_log()]
    return compute_evidenced_posture(assess_control_coverage(entries))


def evidenced_posture_json(workdir: Path, *, hmac_key: bytes) -> str:
    """Return the signed, canonical evidenced-posture document for *workdir*.

    **The** entry point: the operator command prints exactly these bytes, so
    the screen and an offline recomputation from ``.sdd`` cannot differ. The
    signature is HMAC-SHA256 over the canonical bytes of the document without
    its ``signature`` field, keyed with the audit-chain key.

    Args:
        workdir: Project root containing ``.sdd``.
        hmac_key: Audit-chain HMAC key to sign the document with.
    """
    payload = collect_evidenced_posture(workdir).to_dict()
    payload["signature"] = _hmac.new(hmac_key, _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return _canonical_bytes(payload).decode("utf-8")


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def format_evidenced_posture(posture: EvidencedPosture) -> str:
    """Render an evidenced posture as a Rich-formatted string."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=100)

    headline = "not measurable" if posture.score is None else f"{posture.score:.1f}/100"
    console.print(
        Text.assemble(
            ("Evidenced posture: ", "bold"),
            (headline, "bold"),
        )
    )
    console.print(
        f"{posture.evidenced_weight:g}/{posture.measurable_weight:g} weight over "
        f"{posture.measurable_controls} of {posture.registered_controls} controls  |  "
        f"weights {posture.weights_version}\n"
    )

    table = Table(title="Contributions", expand=True)
    table.add_column("Control", style="cyan")
    table.add_column("Weight", justify="right")
    table.add_column("Evidenced", justify="center")
    table.add_column("Chain events")
    for c in posture.contributions:
        table.add_row(
            c.control_id,
            f"{c.weight:g}",
            "[green]yes[/green]" if c.evidenced else "[red]no[/red]",
            "\n".join(c.chain_events) or "-",
        )
    console.print(table)
    return buf.getvalue()
