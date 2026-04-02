"""Quality metrics routes.

Exposes aggregated internal quality metrics for model routing decisions
and cost justification: success rate per model, average tokens per task,
guardrail/janitor pass rates, review rejection rates, and completion time
distributions (p50/p90/p99).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.cost import forecast_planned_backlog

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.server import TaskStore

router = APIRouter()


def _get_sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _parse_timestamp(value: Any) -> float:
    """Convert timestamp to Unix float, handling both numeric and ISO 8601 formats.

    Args:
        value: Unix timestamp (float/int) or ISO 8601 string.

    Returns:
        Unix timestamp as float, or 0.0 if unparseable.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Try parsing ISO 8601 format (with or without timezone)
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, AttributeError):
            return 0.0
    return 0.0


def _pct(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a list of values.

    Args:
        data: List of numeric values (need not be sorted).
        p: Percentile as a fraction 0.0-1.0.

    Returns:
        Percentile value, or 0.0 for empty input.
    """
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(p * (len(sorted_data) - 1))
    return sorted_data[min(idx, len(sorted_data) - 1)]


def _read_completion_metrics(metrics_dir: Path, days: int = 7) -> list[dict[str, Any]]:
    """Read task completion time records from per-day JSONL files.

    Args:
        metrics_dir: Path to the .sdd/metrics/ directory.
        days: How many days of history to include (default 7).

    Returns:
        List of metric-point dicts each with ``timestamp``, ``value``
        (duration in seconds), and ``labels`` (dict with ``model``,
        ``role``, ``success``).
    """
    records: list[dict[str, Any]] = []
    cutoff = time.time() - days * 86400

    for f in sorted(metrics_dir.glob("task_completion_time_*.jsonl")):
        try:
            for raw in f.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(raw)
                    if _parse_timestamp(rec.get("timestamp", 0)) >= cutoff:
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue

    return records


def _read_api_usage_metrics(metrics_dir: Path, days: int = 7) -> list[dict[str, Any]]:
    """Read token-count records from api_usage JSONL files.

    These are written by ``MetricsCollector.complete_task()`` when
    ``tokens_used > 0``, using ``MetricType.API_USAGE``.

    Args:
        metrics_dir: Path to the .sdd/metrics/ directory.
        days: How many days of history to include.

    Returns:
        List of metric-point dicts with ``value`` = tokens and
        ``labels`` containing ``model`` and ``task_id``.
    """
    records: list[dict[str, Any]] = []
    cutoff = time.time() - days * 86400

    for f in sorted(metrics_dir.glob("api_usage_*.jsonl")):
        try:
            for raw in f.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec: dict[str, Any] = json.loads(raw)
                    if _parse_timestamp(rec.get("timestamp", 0)) >= cutoff:
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue

    return records


def _read_quality_gates(metrics_dir: Path, days: int = 30) -> list[dict[str, Any]]:
    """Read quality gate results from the append-only quality_gates.jsonl file.

    Args:
        metrics_dir: Path to the .sdd/metrics/ directory.
        days: How many days of history to include (default 30).

    Returns:
        List of gate-result dicts each with ``timestamp``, ``task_id``,
        ``gate``, and ``result`` (``"pass"``, ``"blocked"``, or
        ``"flagged"``).
    """
    gates_file = metrics_dir / "quality_gates.jsonl"
    records: list[dict[str, Any]] = []
    if not gates_file.exists():
        return records

    cutoff = time.time() - days * 86400
    try:
        for raw in gates_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec: dict[str, Any] = json.loads(raw)
                if _parse_timestamp(rec.get("timestamp", 0)) >= cutoff:
                    records.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass

    return records


def _compute_per_model(
    completion_records: list[dict[str, Any]],
    usage_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute per-model quality statistics from metric records.

    Args:
        completion_records: Records from task_completion_time JSONL files.
        usage_records: Records from api_usage JSONL files (token counts).

    Returns:
        Dict mapping model name to a stats dict with ``total_tasks``,
        ``success_rate``, ``avg_tokens``, ``avg_completion_seconds``,
        and ``p50/p90/p99_completion_seconds``.
    """
    by_model_durations: dict[str, list[float]] = defaultdict(list)
    by_model_successes: dict[str, list[bool]] = defaultdict(list)

    for rec in completion_records:
        labels: dict[str, str] = rec.get("labels", {})
        model = labels.get("model") or "unknown"
        success = labels.get("success", "True") == "True"
        value = float(rec.get("value", 0.0))
        by_model_durations[model].append(value)
        by_model_successes[model].append(success)

    by_model_tokens: dict[str, list[float]] = defaultdict(list)
    for rec in usage_records:
        labels = rec.get("labels", {})
        model = labels.get("model") or "unknown"
        tokens = float(rec.get("value", 0.0))
        if tokens > 0:
            by_model_tokens[model].append(tokens)

    all_models = set(by_model_durations.keys()) | set(by_model_tokens.keys())
    result: dict[str, Any] = {}

    for model in all_models:
        durations = by_model_durations.get(model, [])
        successes = by_model_successes.get(model, [])
        tokens = by_model_tokens.get(model, [])

        total = len(durations)
        success_count = sum(1 for s in successes if s)

        result[model] = {
            "total_tasks": total,
            "success_rate": success_count / total if total > 0 else 1.0,
            "avg_tokens": sum(tokens) / len(tokens) if tokens else 0.0,
            "avg_completion_seconds": sum(durations) / len(durations) if durations else 0.0,
            "p50_completion_seconds": _pct(durations, 0.50),
            "p90_completion_seconds": _pct(durations, 0.90),
            "p99_completion_seconds": _pct(durations, 0.99),
        }

    return result


def _compute_gate_stats(gate_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate quality gate results by gate name.

    Args:
        gate_records: Records from quality_gates.jsonl.

    Returns:
        Dict mapping gate name to ``{total, pass, blocked, flagged, pass_rate}``.
    """
    by_gate: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "pass": 0, "blocked": 0, "flagged": 0})

    for rec in gate_records:
        gate = rec.get("gate") or "unknown"
        result = rec.get("result", "pass")
        by_gate[gate]["total"] += 1
        if result == "pass":
            by_gate[gate]["pass"] += 1
        elif result == "blocked":
            by_gate[gate]["blocked"] += 1
        elif result == "flagged":
            by_gate[gate]["flagged"] += 1

    return {
        gate: {
            **counts,
            "pass_rate": counts["pass"] / counts["total"] if counts["total"] > 0 else 1.0,
        }
        for gate, counts in by_gate.items()
    }


def _read_current_spend(metrics_dir: Path) -> float:
    """Read cumulative task cost from metrics JSONL."""
    tasks_path = metrics_dir / "tasks.jsonl"
    if not tasks_path.exists():
        return 0.0

    latest_by_task: dict[str, float] = {}
    try:
        for raw in tasks_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            typed_record = cast("dict[str, Any]", record)
            task_id = str(typed_record.get("task_id", "")).strip()
            if not task_id:
                continue
            latest_by_task[task_id] = float(typed_record.get("cost_usd", 0.0) or 0.0)
    except OSError:
        return 0.0

    return sum(latest_by_task.values())


def _load_budget_from_seed(sdd_dir: Path) -> float:
    """Load the configured run budget from ``bernstein.yaml`` when available."""
    from bernstein.core.seed import SeedError, parse_seed

    seed_path = sdd_dir.parent / "bernstein.yaml"
    if not seed_path.exists():
        return 0.0
    try:
        cfg = parse_seed(seed_path)
    except SeedError:
        return 0.0
    return float(cfg.budget_usd or 0.0)


@router.get("/quality")
async def get_quality_metrics(request: Request) -> JSONResponse:
    """Return aggregated internal quality metrics (last 7 days).

    Reads from ``.sdd/metrics/`` JSONL files to compute:

    - ``per_model``: per-model success rate, avg tokens, and completion
      time distribution (p50/p90/p99).
    - ``overall``: aggregate across all models.
    - ``gate_stats``: per-gate pass/blocked/flagged counts (last 30 days).
    - ``guardrail_pass_rate``: fraction of gate checks that passed.
    - ``review_rejection_rate``: fraction of tasks that failed overall.

    Returns an empty structure when no metric data exists yet.
    """
    sdd_dir = _get_sdd_dir(request)
    metrics_dir = sdd_dir / "metrics"

    empty: dict[str, Any] = {
        "per_model": {},
        "overall": {"total_tasks": 0, "success_rate": 1.0},
        "gate_stats": {},
        "guardrail_pass_rate": 1.0,
        "review_rejection_rate": 0.0,
        "generated_at": time.time(),
    }

    if not metrics_dir.exists():
        return JSONResponse(empty)

    completion_records = _read_completion_metrics(metrics_dir)
    usage_records = _read_api_usage_metrics(metrics_dir)
    gate_records = _read_quality_gates(metrics_dir)

    per_model = _compute_per_model(completion_records, usage_records)
    gate_stats = _compute_gate_stats(gate_records)

    all_durations = [float(r.get("value", 0)) for r in completion_records]
    total = len(all_durations)
    success_count = sum(1 for r in completion_records if r.get("labels", {}).get("success", "True") == "True")

    # Guardrail pass rate: fraction of gate checks that weren't blocked/flagged
    gate_violations = sum(counts["blocked"] + counts["flagged"] for counts in gate_stats.values())
    gate_total = sum(counts["total"] for counts in gate_stats.values())
    guardrail_pass_rate = (gate_total - gate_violations) / gate_total if gate_total > 0 else 1.0

    return JSONResponse(
        {
            "per_model": per_model,
            "overall": {
                "total_tasks": total,
                "success_rate": success_count / total if total > 0 else 1.0,
                "avg_completion_seconds": sum(all_durations) / total if total > 0 else 0.0,
                "p50_completion_seconds": _pct(all_durations, 0.50),
                "p90_completion_seconds": _pct(all_durations, 0.90),
                "p99_completion_seconds": _pct(all_durations, 0.99),
            },
            "gate_stats": gate_stats,
            "guardrail_pass_rate": guardrail_pass_rate,
            "review_rejection_rate": (total - success_count) / total if total > 0 else 0.0,
            "generated_at": time.time(),
        }
    )


@router.get("/quality/budget-forecast")
async def get_budget_forecast(request: Request) -> JSONResponse:
    """Return projected spend for the active planned backlog."""
    sdd_dir = _get_sdd_dir(request)
    store = _get_store(request)
    metrics_dir = sdd_dir / "metrics"
    forecast = forecast_planned_backlog(
        store.list_tasks(),
        metrics_dir=metrics_dir if metrics_dir.exists() else None,
        current_spend_usd=_read_current_spend(metrics_dir),
        budget_usd=_load_budget_from_seed(sdd_dir),
    )
    payload = forecast.to_dict()
    payload["generated_at"] = time.time()
    return JSONResponse(payload)


@router.get("/quality/models")
async def get_quality_by_model(request: Request) -> JSONResponse:
    """Return per-model quality breakdown (last 30 days).

    Extended view of model performance for routing configuration and cost
    analysis. Covers a longer window than the default ``/quality`` summary.
    """
    sdd_dir = _get_sdd_dir(request)
    metrics_dir = sdd_dir / "metrics"

    if not metrics_dir.exists():
        return JSONResponse({"models": {}, "generated_at": time.time()})

    completion_records = _read_completion_metrics(metrics_dir, days=30)
    usage_records = _read_api_usage_metrics(metrics_dir, days=30)
    per_model = _compute_per_model(completion_records, usage_records)

    return JSONResponse({"models": per_model, "generated_at": time.time()})
