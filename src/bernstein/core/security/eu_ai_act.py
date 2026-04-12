"""Task-level EU AI Act risk assessment helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal


class RiskLevel(StrEnum):
    """Task-level EU AI Act risk categories."""

    MINIMAL = "minimal"
    LIMITED = "limited"
    HIGH = "high"
    UNACCEPTABLE = "unacceptable"


class TaskLike(Protocol):
    """Minimal task surface needed for EU AI Act assessment."""

    title: str
    description: str
    role: str


_UNACCEPTABLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "social_scoring": ("social scoring", "citizen score", "trustworthiness score"),
    "realtime_biometric": ("real-time biometric", "biometric surveillance", "live facial recognition"),
    "subliminal_manipulation": ("subliminal", "manipulate behavior", "covert persuasion"),
}

_HIGH_RISK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "authentication": ("auth", "authentication", "authorization", "credential", "identity", "login"),
    "payments": ("payment", "payments", "billing", "checkout", "invoice", "refund"),
    "health": ("health", "medical", "patient", "diagnosis", "clinical", "hospital"),
    "critical": ("critical infrastructure", "critical system", "safety critical", "critical service"),
}

_MINIMAL_KEYWORDS: tuple[str, ...] = (
    "docs",
    "documentation",
    "readme",
    "comment",
    "typo",
    "format",
    "formatting",
    "lint",
    "rename",
)

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.MINIMAL: 0,
    RiskLevel.LIMITED: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.UNACCEPTABLE: 3,
}

_BERNSTEIN_RISK_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


@dataclass(frozen=True)
class TaskRiskAssessment:
    """Structured EU AI Act assessment for a task.

    Attributes:
        risk_level: EU AI Act risk category for the task.
        reasons: Human-readable reasons supporting the classification.
        approval_required: Whether human approval is required before merge.
        bernstein_risk_level: Bernstein-native approval-routing severity.
    """

    risk_level: RiskLevel
    reasons: tuple[str, ...]
    approval_required: bool
    bernstein_risk_level: Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class AssessmentLogRecord:
    """Persisted EU AI Act assessment entry."""

    task_id: str
    title: str
    role: str
    risk_level: str
    approval_required: bool
    bernstein_risk_level: str
    reasons: tuple[str, ...]
    assessed_at: str


@dataclass(frozen=True)
class AssessmentSummary:
    """Aggregated view of persisted EU AI Act assessments."""

    total: int
    counts: dict[str, int]
    latest_high_risk: tuple[AssessmentLogRecord, ...]


def assess_risk(task: TaskLike) -> RiskLevel:
    """Return the EU AI Act risk tier for a task."""

    return assess_task(task).risk_level


def assess_task(task: TaskLike) -> TaskRiskAssessment:
    """Assess a task against lightweight EU AI Act heuristics."""

    haystack = _normalize_task_text(task)

    unacceptable_reasons = _matched_reasons(haystack, _UNACCEPTABLE_KEYWORDS)
    if unacceptable_reasons:
        return TaskRiskAssessment(
            risk_level=RiskLevel.UNACCEPTABLE,
            reasons=unacceptable_reasons,
            approval_required=True,
            bernstein_risk_level="critical",
        )

    high_risk_reasons = _matched_reasons(haystack, _HIGH_RISK_KEYWORDS)
    if high_risk_reasons:
        return TaskRiskAssessment(
            risk_level=RiskLevel.HIGH,
            reasons=high_risk_reasons,
            approval_required=True,
            bernstein_risk_level="high",
        )

    if _is_minimal_task(haystack):
        return TaskRiskAssessment(
            risk_level=RiskLevel.MINIMAL,
            reasons=("low-impact maintenance or documentation work",),
            approval_required=False,
            bernstein_risk_level="low",
        )

    return TaskRiskAssessment(
        risk_level=RiskLevel.LIMITED,
        reasons=("general software change outside EU AI Act high-risk domains",),
        approval_required=False,
        bernstein_risk_level="medium",
    )


def merge_eu_ai_act_risk(current: str, derived: RiskLevel) -> RiskLevel:
    """Return the stricter of an explicit and derived EU AI Act risk."""

    try:
        explicit = RiskLevel(current)
    except ValueError:
        explicit = RiskLevel.MINIMAL
    return explicit if _RISK_ORDER[explicit] >= _RISK_ORDER[derived] else derived


def merge_bernstein_risk(current: str, derived: str) -> str:
    """Return the stricter Bernstein-native risk value."""

    current_key = current if current in _BERNSTEIN_RISK_ORDER else "low"
    return current_key if _BERNSTEIN_RISK_ORDER[current_key] >= _BERNSTEIN_RISK_ORDER[derived] else derived


def build_log_record(task_id: str, task: TaskLike, assessment: TaskRiskAssessment) -> AssessmentLogRecord:
    """Build a persisted log record for an assessed task."""

    return AssessmentLogRecord(
        task_id=task_id,
        title=task.title,
        role=task.role,
        risk_level=assessment.risk_level.value,
        approval_required=assessment.approval_required,
        bernstein_risk_level=assessment.bernstein_risk_level,
        reasons=assessment.reasons,
        assessed_at=datetime.now(tz=UTC).isoformat(),
    )


def append_assessment_log(sdd_dir: Path, record: AssessmentLogRecord) -> None:
    """Append an EU AI Act assessment record to JSONL audit storage."""

    target = sdd_dir / "metrics" / "eu_ai_act_assessments.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=True) + "\n")


def read_assessment_records(sdd_dir: Path) -> tuple[AssessmentLogRecord, ...]:
    """Read persisted EU AI Act assessment records."""

    target = sdd_dir / "metrics" / "eu_ai_act_assessments.jsonl"
    if not target.exists():
        return ()

    records: list[AssessmentLogRecord] = []
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        raw_record = cast("dict[str, object]", raw)
        reasons_raw = raw_record.get("reasons", ())
        if not isinstance(reasons_raw, list):
            reasons_raw = []
        reasons = tuple(str(reason) for reason in cast("list[object]", reasons_raw))
        try:
            records.append(
                AssessmentLogRecord(
                    task_id=str(raw_record["task_id"]),
                    title=str(raw_record["title"]),
                    role=str(raw_record["role"]),
                    risk_level=str(raw_record["risk_level"]),
                    approval_required=bool(raw_record["approval_required"]),
                    bernstein_risk_level=str(raw_record["bernstein_risk_level"]),
                    reasons=reasons,
                    assessed_at=str(raw_record["assessed_at"]),
                )
            )
        except KeyError:
            continue
    return tuple(records)


def summarize_assessments(sdd_dir: Path) -> AssessmentSummary:
    """Summarize persisted EU AI Act assessment records."""

    records = read_assessment_records(sdd_dir)
    counts = {level.value: 0 for level in RiskLevel}
    for record in records:
        if record.risk_level in counts:
            counts[record.risk_level] += 1
    latest_high_risk = tuple(
        record
        for record in reversed(records)
        if record.risk_level in {RiskLevel.HIGH.value, RiskLevel.UNACCEPTABLE.value}
    )[:5]
    return AssessmentSummary(total=len(records), counts=counts, latest_high_risk=latest_high_risk)


def _normalize_task_text(task: TaskLike) -> str:
    return re.sub(r"\s+", " ", f"{task.title} {task.description} {task.role}").lower()


def _matched_reasons(haystack: str, rules: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    reasons: list[str] = []
    for label, keywords in rules.items():
        if any(keyword in haystack for keyword in keywords):
            reasons.append(label.replace("_", " "))
    return tuple(reasons)


def _is_minimal_task(haystack: str) -> bool:
    return any(keyword in haystack for keyword in _MINIMAL_KEYWORDS)
