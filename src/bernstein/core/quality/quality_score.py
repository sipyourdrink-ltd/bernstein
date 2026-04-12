"""Aggregate quality-gate reports into a weighted score."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from bernstein.core.gate_runner import GateReport


@dataclass(frozen=True)
class QualityScore:
    """Aggregated quality score for a task."""

    total: int
    breakdown: dict[str, int]
    grade: str
    trend: str


GATE_WEIGHTS: dict[str, float] = {
    "lint": 0.15,
    "type_check": 0.15,
    "tests": 0.25,
    "security_scan": 0.10,
    "coverage_delta": 0.15,
    "complexity_check": 0.05,
    "dead_code": 0.05,
    "import_cycle": 0.05,
    "pii_scan": 0.05,
}


def _coerce_int(raw: object) -> int | None:
    """Convert a JSON-loaded primitive to ``int`` when valid."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float, str)):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


class QualityScorer:
    """Calculate weighted quality scores from gate reports."""

    HISTORY_FILE = Path(".sdd/metrics/quality_scores.jsonl")

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._history_path = workdir / self.HISTORY_FILE

    def score(self, report: GateReport) -> QualityScore:
        """Calculate a weighted quality score from a gate report."""
        included: list[tuple[str, float, int]] = []
        breakdown: dict[str, int] = {}
        for result in report.results:
            if result.status in {"skipped", "bypassed"}:
                continue
            weight = GATE_WEIGHTS.get(result.name, 0.05)
            points = self._points_for_status(result.status)
            breakdown[result.name] = points
            included.append((result.name, weight, points))

        if not included:
            total = 100
        else:
            weight_sum = sum(weight for _, weight, _ in included)
            total = round(sum((weight / weight_sum) * points for _, weight, points in included))

        return QualityScore(
            total=total,
            breakdown=breakdown,
            grade=self.grade(total),
            trend=self.trend(),
        )

    def record(self, task_id: str, score: QualityScore) -> None:
        """Append a score record to the history file."""
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            **asdict(score),
        }
        with self._history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def trend(self, window: int = 10) -> str:
        """Compute a simple trend over the last ``window`` scores."""
        scores = self._history_totals(window)
        if len(scores) < 2:
            return "stable"
        n = len(scores)
        x_vals = list(range(n))
        mean_x = sum(x_vals) / n
        mean_y = sum(scores) / n
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, scores, strict=False))
        denominator = sum((x - mean_x) ** 2 for x in x_vals)
        slope = numerator / denominator if denominator else 0.0
        if slope > 2.0:
            return "improving"
        if slope < -2.0:
            return "declining"
        return "stable"

    def grade(self, total: int) -> str:
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

    def _history_totals(self, window: int) -> list[int]:
        """Return the most recent historic totals."""
        if not self._history_path.exists():
            return []
        totals: list[int] = []
        for line in self._history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                data = cast("dict[str, object]", raw)
                total = _coerce_int(data.get("total"))
                if total is None:
                    continue
                totals.append(total)
        return totals[-window:]

    def _points_for_status(self, status: str) -> int:
        """Return score points for a gate status."""
        if status == "pass":
            return 100
        if status in {"warn", "timeout"}:
            return 50
        return 0
