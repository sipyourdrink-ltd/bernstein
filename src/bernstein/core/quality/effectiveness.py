"""Effectiveness scoring for completed agent sessions."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.models import AgentSession, Scope, Task, TaskStatus

if TYPE_CHECKING:
    from bernstein.core.agent_log_aggregator import AgentLogSummary


@dataclass(frozen=True)
class EffectivenessScore:
    """Composite effectiveness score for an agent session."""

    session_id: str
    task_id: str
    role: str
    model: str
    effort: str
    time_score: int
    quality_score: int
    efficiency_score: int
    retry_score: int
    completion_score: int
    total: int
    grade: str
    wall_time_s: float
    estimated_time_s: float
    tokens_used: int
    retry_count: int
    fix_count: int
    gate_pass_rate: float


EFFECTIVENESS_WEIGHTS: dict[str, float] = {
    "time": 0.20,
    "quality": 0.35,
    "efficiency": 0.15,
    "retry": 0.15,
    "completion": 0.15,
}


class EffectivenessScorer:
    """Score completed agent sessions and learn preferred configurations."""

    HISTORY_FILE = Path(".sdd/metrics/effectiveness.jsonl")

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._history_path = workdir / self.HISTORY_FILE

    def score(
        self,
        session: AgentSession,
        task: Task,
        gate_report: object | None,
        log_summary: AgentLogSummary | None,
    ) -> EffectivenessScore:
        """Calculate an effectiveness score from runtime/session outcomes."""
        wall_time_s = max(0.0, self._session_end_ts(session) - session.spawn_ts)
        estimated_time_s = max(float(task.estimated_minutes * 60), 1.0)
        tokens_used = max(session.tokens_used, 0)
        retry_count = self._retry_count(task)
        fix_count = 1 if task.title.startswith("Fix: ") else 0
        gate_pass_rate, quality_score = self._quality_from_gate_report(gate_report)

        score = EffectivenessScore(
            session_id=session.id,
            task_id=task.id,
            role=task.role,
            model=session.model_config.model,
            effort=session.model_config.effort,
            time_score=self._time_score(wall_time_s, estimated_time_s),
            quality_score=quality_score,
            efficiency_score=self._efficiency_score(tokens_used, task.scope),
            retry_score=max(0, 100 - (retry_count * 25) - (fix_count * 20)),
            completion_score=self._completion_score(task, log_summary),
            total=0,
            grade="F",
            wall_time_s=round(wall_time_s, 2),
            estimated_time_s=round(estimated_time_s, 2),
            tokens_used=tokens_used,
            retry_count=retry_count,
            fix_count=fix_count,
            gate_pass_rate=gate_pass_rate,
        )
        total = round(
            (score.time_score * EFFECTIVENESS_WEIGHTS["time"])
            + (score.quality_score * EFFECTIVENESS_WEIGHTS["quality"])
            + (score.efficiency_score * EFFECTIVENESS_WEIGHTS["efficiency"])
            + (score.retry_score * EFFECTIVENESS_WEIGHTS["retry"])
            + (score.completion_score * EFFECTIVENESS_WEIGHTS["completion"])
        )
        result = cast(EffectivenessScore, replace(score, total=total, grade=self._grade(total)))
        return result

    def record(self, score: EffectivenessScore) -> None:
        """Append one effectiveness record to JSONL history."""
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": datetime.now(UTC).isoformat(), **asdict(score)}
        with self._history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def best_config_for_role(self, role: str) -> tuple[str, str] | None:
        """Return the best model/effort pair for a role when data is sufficient."""
        records = [record for record in self._history() if record.role == role]
        if len(records) < 5:
            return None
        grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
        for record in records[-50:]:
            grouped[(record.model, record.effort)].append(record.total)
        best_key = max(grouped, key=lambda key: sum(grouped[key]) / len(grouped[key]))
        return best_key

    def export_for_bandit(self, role: str) -> dict[str, float]:
        """Export success rates per model for bandit seeding.

        Computes the fraction of sessions scoring above the "good" threshold
        (grade B or better, i.e. total >= 80) for each model used in the
        given role.  Only models with at least 3 observations are included
        to avoid noisy priors.

        Args:
            role: Task role to export data for.

        Returns:
            Mapping of model name to success rate (0.0-1.0).
        """
        records = [record for record in self._history() if record.role == role]
        if not records:
            return {}
        # Use last 50 records to match best_config_for_role window
        recent = records[-50:]
        model_results: dict[str, list[bool]] = defaultdict(list)
        for record in recent:
            model_results[record.model].append(record.total >= 80)
        return {model: sum(results) / len(results) for model, results in model_results.items() if len(results) >= 3}

    def average_for_model(self, model: str) -> float:
        """Return average effectiveness for a model across all roles."""
        records = [record.total for record in self._history() if record.model == model]
        if not records:
            return 0.0
        return sum(records) / len(records)

    def trends(self, window: int = 20) -> dict[str, str]:
        """Return per-role trend direction over the last ``window`` scores."""
        by_role: dict[str, list[int]] = defaultdict(list)
        for record in self._history():
            by_role[record.role].append(record.total)
        return {role: self._trend(scores[-window:]) for role, scores in by_role.items()}

    def recent(self, limit: int = 20) -> list[EffectivenessScore]:
        """Return the most recent recorded scores."""
        history = self._history()
        return history[-limit:]

    def _history(self) -> list[EffectivenessScore]:
        """Load historical scores from JSONL."""
        if not self._history_path.exists():
            return []
        records: list[EffectivenessScore] = []
        for raw_line in self._history_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                raw_data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw_data, dict):
                continue
            data = cast("dict[str, Any]", raw_data)
            try:
                records.append(
                    EffectivenessScore(
                        session_id=str(data["session_id"]),
                        task_id=str(data["task_id"]),
                        role=str(data["role"]),
                        model=str(data["model"]),
                        effort=str(data["effort"]),
                        time_score=int(data["time_score"]),
                        quality_score=int(data["quality_score"]),
                        efficiency_score=int(data["efficiency_score"]),
                        retry_score=int(data["retry_score"]),
                        completion_score=int(data["completion_score"]),
                        total=int(data["total"]),
                        grade=str(data["grade"]),
                        wall_time_s=float(data["wall_time_s"]),
                        estimated_time_s=float(data["estimated_time_s"]),
                        tokens_used=int(data["tokens_used"]),
                        retry_count=int(data["retry_count"]),
                        fix_count=int(data["fix_count"]),
                        gate_pass_rate=float(data["gate_pass_rate"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _session_end_ts(self, session: AgentSession) -> float:
        """Choose the best available end timestamp for a session."""
        if session.heartbeat_ts > session.spawn_ts:
            return session.heartbeat_ts
        return time.time()

    def _time_score(self, wall_time_s: float, estimated_time_s: float) -> int:
        """Score runtime relative to the estimate."""
        ratio = wall_time_s / estimated_time_s
        if ratio <= 1.0:
            return 100
        if ratio <= 1.5:
            return 85
        if ratio <= 2.0:
            return 75
        if ratio <= 3.0:
            return 50
        return 40

    def _efficiency_score(self, tokens_used: int, scope: Scope) -> int:
        """Score token efficiency against a rough scope budget."""
        budget = {
            Scope.SMALL: 20_000,
            Scope.MEDIUM: 60_000,
            Scope.LARGE: 200_000,
        }[scope]
        if tokens_used <= 0:
            return 50
        ratio = tokens_used / float(budget)
        if ratio <= 0.5:
            return 100
        if ratio <= 1.0:
            return 85
        if ratio <= 1.5:
            return 70
        if ratio <= 2.0:
            return 55
        return 35

    def _completion_score(self, task: Task, log_summary: AgentLogSummary | None) -> int:
        """Score how fully the agent completed the task."""
        if task.status == TaskStatus.DONE:
            return 100
        if task.result_summary:
            return 80
        if log_summary is not None and log_summary.files_modified:
            return 60
        return 25

    def _quality_from_gate_report(self, gate_report: object | None) -> tuple[float, int]:
        """Extract gate pass rate and quality score from supported gate-report shapes."""
        if gate_report is None:
            return (0.0, 50)

        report = cast("Any", gate_report)
        total = self._extract_quality_total(report)
        pass_rate = self._extract_pass_rate(report, total)
        return (pass_rate, total)

    @staticmethod
    def _extract_quality_total(report: Any) -> int:
        """Extract numeric quality total from a gate report."""
        if hasattr(report, "quality_score") and report.quality_score is not None:
            return int(getattr(report.quality_score, "total", 50))
        if hasattr(report, "overall_pass"):
            return 100 if bool(report.overall_pass) else 0
        if hasattr(report, "passed"):
            return 100 if bool(report.passed) else 0
        return 50

    @staticmethod
    def _extract_pass_rate(report: Any, total: int) -> float:
        """Extract pass rate from a gate report's results list."""
        if hasattr(report, "results"):
            results = list(cast("list[Any]", report.results))
            passes = sum(
                1 for result in results if str(getattr(result, "status", "")) in {"pass", "skipped", "bypassed"}
            )
            return (passes / len(results)) if results else 0.0
        if hasattr(report, "gate_results"):
            results = list(cast("list[Any]", report.gate_results))
            passes = sum(1 for result in results if bool(getattr(result, "passed", False)))
            return (passes / len(results)) if results else 0.0
        return 1.0 if total == 100 else 0.0

    def _retry_count(self, task: Task) -> int:
        """Return the retry count for a task.

        audit-017: prefer the typed ``task.retry_count`` field; fall back to
        a legacy ``[RETRY N]`` title prefix only when the typed field is 0
        so historical tasks still score correctly.
        """
        count = task.retry_count
        if count:
            return count
        match = re.match(r"^\[RETRY (\d+)\]\s*", task.title)
        return int(match.group(1)) if match else 0

    def _grade(self, total: int) -> str:
        """Map a numeric total to a letter grade."""
        if total >= 90:
            return "A"
        if total >= 80:
            return "B"
        if total >= 70:
            return "C"
        if total >= 60:
            return "D"
        return "F"

    def _trend(self, values: list[int]) -> str:
        """Compute coarse trend direction."""
        if len(values) < 3:
            return "stable"
        midpoint = len(values) // 2
        first = sum(values[:midpoint]) / max(midpoint, 1)
        second = sum(values[midpoint:]) / max(len(values) - midpoint, 1)
        delta = second - first
        if delta > 5:
            return "improving"
        if delta < -5:
            return "declining"
        return "stable"
