"""Local helpfulness attribution for skill activations.

This module joins the local activation log with local task completion
metrics and writes a compact Beta-Bernoulli summary to
``.sdd/skills/helpfulness.json``. The report is derived entirely from
project files and does not perform network I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.observability.metric_collector import iter_metric_files
from bernstein.core.skills.activation_log import activation_log_path

_REPORT_SUBPATH: Final[tuple[str, ...]] = (".sdd", "skills", "helpfulness.json")
_PRIOR_ALPHA: Final[float] = 1.0
_PRIOR_BETA: Final[float] = 1.0


@dataclass(frozen=True)
class HelpfulnessStats:
    """Beta-Bernoulli score for one skill or one skill-role pair."""

    observations: int
    successes: int
    failures: int
    alpha: float
    beta: float
    posterior_mean: float

    def as_payload(self) -> dict[str, int | float]:
        """Render stats as stable JSON payload fields."""
        return {
            "observations": self.observations,
            "successes": self.successes,
            "failures": self.failures,
            "alpha": self.alpha,
            "beta": self.beta,
            "posterior_mean": self.posterior_mean,
        }


@dataclass(frozen=True)
class SkillHelpfulness:
    """Helpfulness summary for one skill."""

    skill: str
    observations: int
    successes: int
    failures: int
    alpha: float
    beta: float
    posterior_mean: float
    by_role: dict[str, HelpfulnessStats] = field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        """Render the skill summary as a stable JSON object."""
        return {
            "skill": self.skill,
            "observations": self.observations,
            "successes": self.successes,
            "failures": self.failures,
            "alpha": self.alpha,
            "beta": self.beta,
            "posterior_mean": self.posterior_mean,
            "by_role": {role: stats.as_payload() for role, stats in sorted(self.by_role.items())},
        }


@dataclass(frozen=True)
class HelpfulnessReport:
    """Complete local skill helpfulness report."""

    schema_version: int
    generated_at: str
    unmatched_activations: int
    skills: dict[str, SkillHelpfulness]

    def as_payload(self) -> dict[str, object]:
        """Render the report as a stable JSON object."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "unmatched_activations": self.unmatched_activations,
            "skills": {skill: stats.as_payload() for skill, stats in sorted(self.skills.items())},
        }


@dataclass
class _Counter:
    successes: int = 0
    failures: int = 0

    def record(self, success: bool) -> None:
        """Record one Bernoulli outcome."""
        if success:
            self.successes += 1
        else:
            self.failures += 1

    def freeze(self) -> HelpfulnessStats:
        """Convert the mutable counter into posterior stats."""
        alpha = _PRIOR_ALPHA + self.successes
        beta = _PRIOR_BETA + self.failures
        observations = self.successes + self.failures
        return HelpfulnessStats(
            observations=observations,
            successes=self.successes,
            failures=self.failures,
            alpha=alpha,
            beta=beta,
            posterior_mean=alpha / (alpha + beta),
        )


def helpfulness_path(workdir: Path) -> Path:
    """Return the local helpfulness report path for *workdir*."""
    return workdir.joinpath(*_REPORT_SUBPATH)


def build_helpfulness_report(
    workdir: Path,
    *,
    now: datetime | None = None,
) -> HelpfulnessReport:
    """Build a local skill helpfulness report from activation and task metrics.

    Args:
        workdir: Project root containing ``.sdd``.
        now: Optional timestamp override for deterministic tests.

    Returns:
        A report containing one entry per skill with at least one matched
        task completion outcome.
    """
    outcomes = _load_task_outcomes(workdir / ".sdd" / "metrics")
    skill_counts: dict[str, _Counter] = {}
    role_counts: dict[str, dict[str, _Counter]] = {}
    unmatched = 0

    for activation in _iter_activation_rows(activation_log_path(workdir)):
        skill = _string_field(activation, "skill")
        task_id = _string_field(activation, "task_id")
        if not skill or not task_id:
            continue
        outcome = outcomes.get(task_id)
        if outcome is None:
            unmatched += 1
            continue
        role = _string_field(activation, "role") or outcome.role
        skill_counts.setdefault(skill, _Counter()).record(outcome.success)
        role_counts.setdefault(skill, {}).setdefault(role, _Counter()).record(outcome.success)

    skills = {
        skill: _freeze_skill(skill, counter, role_counts.get(skill, {}))
        for skill, counter in sorted(skill_counts.items())
    }
    return HelpfulnessReport(
        schema_version=1,
        generated_at=_format_timestamp(now or datetime.now(tz=UTC)),
        unmatched_activations=unmatched,
        skills=skills,
    )


def write_helpfulness_report(
    workdir: Path,
    *,
    report: HelpfulnessReport | None = None,
    now: datetime | None = None,
) -> Path:
    """Write ``.sdd/skills/helpfulness.json`` and return its path."""
    current_report = report or build_helpfulness_report(workdir, now=now)
    path = helpfulness_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current_report.as_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class _TaskOutcome:
    success: bool
    role: str
    timestamp: float


def _freeze_skill(skill: str, counter: _Counter, roles: dict[str, _Counter]) -> SkillHelpfulness:
    stats = counter.freeze()
    return SkillHelpfulness(
        skill=skill,
        observations=stats.observations,
        successes=stats.successes,
        failures=stats.failures,
        alpha=stats.alpha,
        beta=stats.beta,
        posterior_mean=stats.posterior_mean,
        by_role={role: role_counter.freeze() for role, role_counter in sorted(roles.items())},
    )


def _load_task_outcomes(metrics_dir: Path) -> dict[str, _TaskOutcome]:
    outcomes: dict[str, _TaskOutcome] = {}
    for path in iter_metric_files(metrics_dir, "task_completion_time"):
        for row in _iter_jsonl_objects(path):
            labels = _mapping_field(row, "labels")
            if labels is None:
                continue
            task_id = _string_field(labels, "task_id")
            success = _success_field(labels, "success")
            if not task_id or success is None:
                continue
            timestamp = row.get("timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
                timestamp = float("-inf")
            previous = outcomes.get(task_id)
            if previous is None or timestamp >= previous.timestamp:
                outcomes[task_id] = _TaskOutcome(
                    success=success, role=_string_field(labels, "role"), timestamp=timestamp
                )
    return outcomes


def _iter_activation_rows(path: Path) -> list[dict[str, object]]:
    return _iter_jsonl_objects(path)


def _iter_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        row: dict[str, object] = {}
        for key, value in parsed.items():
            if isinstance(key, str):
                row[key] = value
        rows.append(row)
    return rows


def _mapping_field(row: dict[str, object], key: str) -> dict[str, object] | None:
    value = row.get(key)
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for nested_key, nested_value in value.items():
        if isinstance(nested_key, str):
            out[nested_key] = nested_value
    return out


def _string_field(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _success_field(row: dict[str, object], key: str) -> bool | None:
    value = _string_field(row, key).strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return None


def _format_timestamp(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    rendered = ts.astimezone(UTC).isoformat(timespec="milliseconds")
    return rendered[: -len("+00:00")] + "Z" if rendered.endswith("+00:00") else rendered


__all__ = [
    "HelpfulnessReport",
    "HelpfulnessStats",
    "SkillHelpfulness",
    "build_helpfulness_report",
    "helpfulness_path",
    "write_helpfulness_report",
]
