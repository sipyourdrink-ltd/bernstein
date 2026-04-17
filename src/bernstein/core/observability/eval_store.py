"""Evaluation framework for per-model accuracy tracking.

Tracks task outcomes with model, role, complexity, and quality gate
results. Stored in .sdd/metrics/evaluations.jsonl (append-only). Feeds
into the bandit router's reward model and supports benchmark mode.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

EvalResult = Literal["pass", "fail", "retry"]


@dataclass
class EvalRecord:
    task_id: str
    model: str
    role: str
    complexity: str
    result: EvalResult
    duration_s: float = 0.0
    cost_usd: float = 0.0
    step_count: int = 0
    quality_gate_results: dict[str, bool] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


def append_eval_record(record: EvalRecord, metrics_dir: Path) -> Path:
    """Append a record to the JSONL eval log."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    path = metrics_dir / "evaluations.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    return path


def load_eval_records(metrics_dir: Path, limit: int = 0) -> list[EvalRecord]:
    """Load all records. If limit > 0, return only the most recent limit."""
    path = metrics_dir / "evaluations.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if limit > 0:
        lines = lines[-limit:]
    records: list[EvalRecord] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(EvalRecord(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


@dataclass
class ModelAccuracy:
    model: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    retried: int = 0
    avg_duration_s: float = 0.0
    avg_cost_usd: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def per_model_accuracy(records: list[EvalRecord]) -> dict[str, ModelAccuracy]:
    by_model: dict[str, list[EvalRecord]] = defaultdict(list)
    for r in records:
        by_model[r.model].append(r)
    result: dict[str, ModelAccuracy] = {}
    for model, items in by_model.items():
        total = len(items)
        passed = sum(1 for i in items if i.result == "pass")
        failed = sum(1 for i in items if i.result == "fail")
        retried = sum(1 for i in items if i.result == "retry")
        avg_duration = sum(i.duration_s for i in items) / total if total else 0.0
        avg_cost = sum(i.cost_usd for i in items) / total if total else 0.0
        result[model] = ModelAccuracy(
            model=model,
            total=total,
            passed=passed,
            failed=failed,
            retried=retried,
            avg_duration_s=avg_duration,
            avg_cost_usd=avg_cost,
        )
    return result


def per_role_accuracy(records: list[EvalRecord]) -> dict[str, ModelAccuracy]:
    by_role: dict[str, list[EvalRecord]] = defaultdict(list)
    for r in records:
        by_role[r.role].append(r)
    result: dict[str, ModelAccuracy] = {}
    for role, items in by_role.items():
        total = len(items)
        passed = sum(1 for i in items if i.result == "pass")
        failed = sum(1 for i in items if i.result == "fail")
        retried = sum(1 for i in items if i.result == "retry")
        result[role] = ModelAccuracy(
            model=role,  # reusing dataclass, treat as group name
            total=total,
            passed=passed,
            failed=failed,
            retried=retried,
            avg_duration_s=sum(i.duration_s for i in items) / total if total else 0.0,
            avg_cost_usd=sum(i.cost_usd for i in items) / total if total else 0.0,
        )
    return result


def head_to_head(
    records: list[EvalRecord],
    model_a: str,
    model_b: str,
) -> dict[str, ModelAccuracy]:
    """Compare two models on matching task types (same role + complexity)."""
    filtered = [r for r in records if r.model in (model_a, model_b)]
    return per_model_accuracy(filtered)


def benchmark_summary(
    records: list[EvalRecord],
    baseline_model: str,
) -> dict[str, object]:
    """Compare baseline single-model runs vs bandit-routed runs."""
    baseline = [r for r in records if r.model == baseline_model]
    routed = [r for r in records if r.model != baseline_model]

    def _summary(items: list[EvalRecord]) -> dict[str, float]:
        if not items:
            return {"count": 0, "pass_rate": 0.0, "avg_duration_s": 0.0, "avg_cost_usd": 0.0}
        count = len(items)
        passed = sum(1 for i in items if i.result == "pass")
        return {
            "count": count,
            "pass_rate": passed / count,
            "avg_duration_s": sum(i.duration_s for i in items) / count,
            "avg_cost_usd": sum(i.cost_usd for i in items) / count,
        }

    return {
        "baseline": _summary(baseline),
        "routed": _summary(routed),
        "baseline_model": baseline_model,
    }
