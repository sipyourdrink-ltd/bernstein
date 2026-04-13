"""Tests for the Internal Quality Metrics Dashboard feature.

Covers:
- MetricsCollector.get_quality_metrics() aggregation
- routes/quality.py helpers (_pct, _compute_per_model, _compute_gate_stats)
- GET /quality and GET /quality/models endpoints
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.metric_collector import MetricsCollector
from fastapi.testclient import TestClient

from bernstein.core.routes.quality import (
    _compute_gate_stats,
    _compute_per_model,
    _pct,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# _pct helper
# ---------------------------------------------------------------------------


def test_pct_empty() -> None:
    assert _pct([], 0.5) == pytest.approx(0.0)


def test_pct_single() -> None:
    assert _pct([42.0], 0.5) == pytest.approx(42.0)
    assert _pct([42.0], 1.0) == pytest.approx(42.0)


def test_pct_known_values() -> None:
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _pct(data, 0.50) == pytest.approx(30.0)
    assert _pct(data, 1.00) == pytest.approx(50.0)


def test_pct_unsorted_input() -> None:
    data = [50.0, 10.0, 30.0, 20.0, 40.0]
    assert _pct(data, 0.50) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# _compute_per_model
# ---------------------------------------------------------------------------


def _completion_record(model: str, duration: float, success: bool = True) -> dict[str, Any]:
    return {
        "timestamp": time.time(),
        "metric_type": "task_completion_time",
        "value": duration,
        "labels": {"model": model, "role": "backend", "success": str(success)},
    }


def _usage_record(model: str, tokens: float) -> dict[str, Any]:
    return {
        "timestamp": time.time(),
        "metric_type": "api_usage",
        "value": tokens,
        "labels": {"model": model, "role": "backend", "task_id": "t1"},
    }


def test_compute_per_model_empty() -> None:
    result = _compute_per_model([], [])
    assert result == {}


def test_compute_per_model_single_model() -> None:
    completions = [
        _completion_record("sonnet", 10.0, success=True),
        _completion_record("sonnet", 20.0, success=True),
        _completion_record("sonnet", 30.0, success=False),
    ]
    usages = [
        _usage_record("sonnet", 100.0),
        _usage_record("sonnet", 200.0),
    ]
    result = _compute_per_model(completions, usages)
    assert "sonnet" in result
    stats = result["sonnet"]
    assert stats["total_tasks"] == 3
    assert abs(stats["success_rate"] - 2 / 3) < 0.001
    assert abs(stats["avg_tokens"] - 150.0) < 0.001
    assert abs(stats["avg_completion_seconds"] - 20.0) < 0.001
    assert stats["p50_completion_seconds"] == pytest.approx(20.0)


def test_compute_per_model_multiple_models() -> None:
    completions = [
        _completion_record("opus", 60.0),
        _completion_record("haiku", 5.0),
        _completion_record("haiku", 8.0),
    ]
    result = _compute_per_model(completions, [])
    assert set(result.keys()) == {"opus", "haiku"}
    assert result["opus"]["total_tasks"] == 1
    assert result["haiku"]["total_tasks"] == 2


def test_compute_per_model_unknown_model_label() -> None:
    rec = _completion_record("", 10.0)
    rec["labels"]["model"] = ""
    result = _compute_per_model([rec], [])
    assert "unknown" in result


# ---------------------------------------------------------------------------
# _compute_gate_stats
# ---------------------------------------------------------------------------


def _gate_record(gate: str, result: str) -> dict[str, Any]:
    return {"timestamp": time.time(), "task_id": "t1", "gate": gate, "result": result}


def test_compute_gate_stats_empty() -> None:
    assert _compute_gate_stats([]) == {}


def test_compute_gate_stats_all_pass() -> None:
    records = [_gate_record("lint", "pass"), _gate_record("lint", "pass")]
    stats = _compute_gate_stats(records)
    assert stats["lint"]["total"] == 2
    assert stats["lint"]["pass"] == 2
    assert stats["lint"]["pass_rate"] == pytest.approx(1.0)


def test_compute_gate_stats_mixed() -> None:
    records = [
        _gate_record("lint", "pass"),
        _gate_record("lint", "blocked"),
        _gate_record("tests", "pass"),
        _gate_record("tests", "flagged"),
        _gate_record("tests", "pass"),
    ]
    stats = _compute_gate_stats(records)
    assert stats["lint"]["blocked"] == 1
    assert stats["lint"]["pass_rate"] == pytest.approx(0.5)
    assert stats["tests"]["pass"] == 2
    assert abs(stats["tests"]["pass_rate"] - 2 / 3) < 0.001


# ---------------------------------------------------------------------------
# MetricsCollector.get_quality_metrics()
# ---------------------------------------------------------------------------


def _make_collector_with_tasks(tmp_path: Path) -> MetricsCollector:
    mc = MetricsCollector(metrics_dir=tmp_path)
    # Task 1: sonnet, success
    mc.start_task("t1", "backend", "sonnet", "anthropic")
    mc._task_metrics["t1"].start_time = 1000.0
    mc._task_metrics["t1"].end_time = 1060.0  # 60s
    mc._task_metrics["t1"].tokens_used = 500
    mc._task_metrics["t1"].success = True
    mc._task_metrics["t1"].janitor_passed = True
    # Task 2: sonnet, failure
    mc.start_task("t2", "backend", "sonnet", "anthropic")
    mc._task_metrics["t2"].start_time = 2000.0
    mc._task_metrics["t2"].end_time = 2030.0  # 30s
    mc._task_metrics["t2"].tokens_used = 300
    mc._task_metrics["t2"].success = False
    mc._task_metrics["t2"].janitor_passed = False
    # Task 3: haiku, success
    mc.start_task("t3", "frontend", "haiku", "anthropic")
    mc._task_metrics["t3"].start_time = 3000.0
    mc._task_metrics["t3"].end_time = 3010.0  # 10s
    mc._task_metrics["t3"].tokens_used = 100
    mc._task_metrics["t3"].success = True
    mc._task_metrics["t3"].janitor_passed = True
    return mc


def test_get_quality_metrics_empty(tmp_path: Path) -> None:
    mc = MetricsCollector(metrics_dir=tmp_path)
    result = mc.get_quality_metrics()
    assert result["per_model"] == {}
    assert result["overall"]["total_tasks"] == 0
    assert result["guardrail_pass_rate"] == pytest.approx(1.0)
    assert result["review_rejection_rate"] == pytest.approx(0.0)


def test_get_quality_metrics_per_model(tmp_path: Path) -> None:
    mc = _make_collector_with_tasks(tmp_path)
    result = mc.get_quality_metrics()

    assert "sonnet" in result["per_model"]
    assert "haiku" in result["per_model"]

    sonnet = result["per_model"]["sonnet"]
    assert sonnet["total_tasks"] == 2
    assert abs(sonnet["success_rate"] - 0.5) < 0.001
    assert abs(sonnet["avg_tokens"] - 400.0) < 0.001  # (500 + 300) / 2
    assert abs(sonnet["avg_completion_seconds"] - 45.0) < 0.001  # (60 + 30) / 2

    haiku = result["per_model"]["haiku"]
    assert haiku["total_tasks"] == 1
    assert haiku["success_rate"] == pytest.approx(1.0)


def test_get_quality_metrics_overall(tmp_path: Path) -> None:
    mc = _make_collector_with_tasks(tmp_path)
    result = mc.get_quality_metrics()

    overall = result["overall"]
    assert overall["total_tasks"] == 3
    assert abs(overall["success_rate"] - 2 / 3) < 0.001
    assert abs(overall["janitor_pass_rate"] - 2 / 3) < 0.001


def test_get_quality_metrics_guardrail_and_rejection(tmp_path: Path) -> None:
    mc = _make_collector_with_tasks(tmp_path)
    result = mc.get_quality_metrics()

    assert abs(result["guardrail_pass_rate"] - 2 / 3) < 0.001
    assert abs(result["review_rejection_rate"] - 1 / 3) < 0.001


def test_get_quality_metrics_completion_percentiles(tmp_path: Path) -> None:
    mc = MetricsCollector(metrics_dir=tmp_path)
    for i, dur in enumerate([10.0, 20.0, 30.0, 40.0, 50.0]):
        mc.start_task(f"t{i}", "backend", "sonnet", "anthropic")
        mc._task_metrics[f"t{i}"].start_time = float(i * 1000)
        mc._task_metrics[f"t{i}"].end_time = float(i * 1000) + dur
        mc._task_metrics[f"t{i}"].success = True

    result = mc.get_quality_metrics()
    sonnet = result["per_model"]["sonnet"]
    assert sonnet["p50_completion_seconds"] == pytest.approx(30.0)
    # p99 of 5 elements: idx = int(0.99 * 4) = 3 → 40.0
    assert sonnet["p99_completion_seconds"] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# API endpoint integration tests
# ---------------------------------------------------------------------------


def _make_test_app(tmp_path: Path) -> Any:
    from bernstein.core.server import create_app

    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    return app


def test_quality_endpoint_empty(tmp_path: Path) -> None:
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert "per_model" in body
    assert "overall" in body
    assert "gate_stats" in body
    assert "guardrail_pass_rate" in body
    assert "review_rejection_rate" in body
    assert "generated_at" in body


def test_quality_endpoint_with_completion_data(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)

    today = time.strftime("%Y-%m-%d", time.gmtime())
    completion_file = metrics_dir / f"task_completion_time_{today}.jsonl"
    records = [
        {
            "timestamp": time.time(),
            "metric_type": "task_completion_time",
            "value": 45.0,
            "labels": {"task_id": "t1", "role": "backend", "model": "sonnet", "success": "True"},
        },
        {
            "timestamp": time.time(),
            "metric_type": "task_completion_time",
            "value": 90.0,
            "labels": {"task_id": "t2", "role": "backend", "model": "sonnet", "success": "False"},
        },
    ]
    completion_file.write_text("\n".join(json.dumps(r) for r in records))

    app = _make_test_app(tmp_path / "tasks.jsonl")
    # Override sdd_dir to point at our tmp dir
    app.state.sdd_dir = tmp_path / ".sdd"

    with TestClient(app) as client:
        resp = client.get("/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert "sonnet" in body["per_model"]
    sonnet = body["per_model"]["sonnet"]
    assert sonnet["total_tasks"] == 2
    assert abs(sonnet["success_rate"] - 0.5) < 0.001
    assert abs(sonnet["avg_completion_seconds"] - 67.5) < 0.001


def test_quality_models_endpoint(tmp_path: Path) -> None:
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/quality/models")
    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert "generated_at" in body


def test_quality_budget_forecast_endpoint(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "tasks.jsonl").write_text(
        json.dumps({"task_id": "done-1", "cost_usd": 0.4}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".sdd").mkdir(exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text('goal: "Ship roadmap"\nbudget: 1.0\n', encoding="utf-8")

    app = _make_test_app(tmp_path / "tasks.jsonl")
    app.state.sdd_dir = tmp_path / ".sdd"

    with TestClient(app) as client:
        create_one = client.post(
            "/tasks",
            json={
                "title": "Implement billing flow",
                "description": "Build enterprise billing support",
                "role": "backend",
                "priority": 1,
                "scope": "large",
                "complexity": "high",
                "status": "open",
            },
        )
        assert create_one.status_code == 201
        response = client.get("/quality/budget-forecast")

    assert response.status_code == 200
    body = response.json()
    assert body["task_count"] == 1
    assert body["current_spend_usd"] == pytest.approx(0.4)
    assert body["projected_total_cost_usd"] >= body["current_spend_usd"]
    assert body["budget_usd"] == pytest.approx(1.0)
    assert "generated_at" in body


def test_quality_endpoint_with_gate_data(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)

    gates_file = metrics_dir / "quality_gates.jsonl"
    gate_records = [
        {"timestamp": time.time(), "task_id": "t1", "gate": "lint", "result": "pass"},
        {"timestamp": time.time(), "task_id": "t2", "gate": "lint", "result": "blocked"},
        {"timestamp": time.time(), "task_id": "t3", "gate": "tests", "result": "pass"},
    ]
    gates_file.write_text("\n".join(json.dumps(r) for r in gate_records))

    app = _make_test_app(tmp_path / "tasks.jsonl")
    app.state.sdd_dir = metrics_dir.parent

    with TestClient(app) as client:
        resp = client.get("/quality")
    assert resp.status_code == 200
    body = resp.json()
    gate_stats = body["gate_stats"]
    assert "lint" in gate_stats
    assert gate_stats["lint"]["total"] == 2
    assert gate_stats["lint"]["pass"] == 1
    assert gate_stats["lint"]["blocked"] == 1
    assert abs(gate_stats["lint"]["pass_rate"] - 0.5) < 0.001
    # Guardrail pass rate should reflect the blocked gate
    assert body["guardrail_pass_rate"] < 1.0


# ---------------------------------------------------------------------------
# /quality/trend endpoint
# ---------------------------------------------------------------------------


def test_quality_trend_endpoint_empty(tmp_path: Path) -> None:
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/quality/trend")
    assert resp.status_code == 200
    body = resp.json()
    assert "series" in body
    assert "granularity" in body
    assert "window_days" in body
    assert "generated_at" in body
    assert isinstance(body["series"], list)


def test_quality_trend_endpoint_with_data(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)

    # Write completion records for two different days
    today = time.strftime("%Y-%m-%d", time.gmtime())
    completion_file = metrics_dir / f"task_completion_time_{today}.jsonl"
    records = [
        {
            "timestamp": time.time() - 3600,  # 1 hour ago
            "metric_type": "task_completion_time",
            "value": 30.0,
            "labels": {"model": "sonnet", "success": "True"},
        },
        {
            "timestamp": time.time() - 1800,  # 30 min ago
            "metric_type": "task_completion_time",
            "value": 60.0,
            "labels": {"model": "sonnet", "success": "False"},
        },
    ]
    completion_file.write_text("\n".join(json.dumps(r) for r in records))

    # Write gate records
    gates_file = metrics_dir / "quality_gates.jsonl"
    gate_records = [
        {"timestamp": time.time() - 3600, "task_id": "t1", "gate": "lint", "result": "pass"},
        {"timestamp": time.time() - 1800, "task_id": "t2", "gate": "lint", "result": "blocked"},
        {"timestamp": time.time() - 900, "task_id": "t3", "gate": "tests", "result": "pass"},
    ]
    gates_file.write_text("\n".join(json.dumps(r) for r in gate_records))

    # Write quality scores
    scores_file = metrics_dir / "quality_scores.jsonl"
    score_records = [
        {"timestamp": time.time() - 3600, "task_id": "t1", "total": 80, "breakdown": {}},
        {"timestamp": time.time() - 1800, "task_id": "t2", "total": 60, "breakdown": {}},
    ]
    scores_file.write_text("\n".join(json.dumps(r) for r in score_records))

    app = _make_test_app(tmp_path / "tasks.jsonl")
    app.state.sdd_dir = tmp_path / ".sdd"

    with TestClient(app) as client:
        resp = client.get("/quality/trend?days=7&granularity=day")
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "day"
    assert body["window_days"] == 7
    series = body["series"]
    assert len(series) >= 1

    # Today's bucket should exist
    today_bucket = next((b for b in series if b["date"] == today), None)
    assert today_bucket is not None
    assert today_bucket["tasks_total"] == 2
    assert today_bucket["tasks_success"] == 1
    assert abs(today_bucket["success_rate"] - 0.5) < 0.001
    assert "lint" in today_bucket["gate_pass_rates"]
    assert abs(today_bucket["gate_pass_rates"]["lint"] - 0.5) < 0.001
    assert today_bucket["avg_quality_score"] == pytest.approx(70.0)


def test_quality_trend_week_granularity(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)

    today = time.strftime("%Y-%m-%d", time.gmtime())
    completion_file = metrics_dir / f"task_completion_time_{today}.jsonl"
    records = [
        {
            "timestamp": time.time(),
            "metric_type": "task_completion_time",
            "value": 10.0,
            "labels": {"model": "haiku", "success": "True"},
        }
    ]
    completion_file.write_text("\n".join(json.dumps(r) for r in records))

    app = _make_test_app(tmp_path / "tasks.jsonl")
    app.state.sdd_dir = tmp_path / ".sdd"

    with TestClient(app) as client:
        resp = client.get("/quality/trend?granularity=week")
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "week"
    # Week bucket dates are always Monday (weekday 0)
    for bucket in body["series"]:
        from datetime import datetime as _dt

        d = _dt.strptime(bucket["date"], "%Y-%m-%d")
        assert d.weekday() == 0, f"Expected Monday bucket, got {bucket['date']} (weekday {d.weekday()})"


def test_quality_endpoint_with_iso8601_timestamps(tmp_path: Path) -> None:
    """Test that /quality endpoint handles ISO 8601 string timestamps (not just Unix floats)."""
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)

    # Write gate records with ISO 8601 string timestamps (as found in production)
    gates_file = metrics_dir / "quality_gates.jsonl"
    gate_records = [
        {"timestamp": "2026-03-29T19:48:43.812896+00:00", "task_id": "t1", "gate": "lint", "result": "pass"},
        {"timestamp": "2026-03-29T19:50:41.230744+00:00", "task_id": "t2", "gate": "lint", "result": "blocked"},
    ]
    gates_file.write_text("\n".join(json.dumps(r) for r in gate_records))

    # Write completion metrics with numeric timestamps (as normally written)
    completion_file = metrics_dir / "task_completion_time_20260329.jsonl"
    completion_records = [
        {
            "timestamp": time.time(),
            "metric_type": "task_completion_time",
            "value": 10.0,
            "labels": {"model": "sonnet", "success": "True"},
        },
    ]
    completion_file.write_text("\n".join(json.dumps(r) for r in completion_records))

    app = _make_test_app(tmp_path / "tasks.jsonl")
    app.state.sdd_dir = metrics_dir.parent

    with TestClient(app) as client:
        resp = client.get("/quality")
    # Should succeed despite string timestamps in gate records
    assert resp.status_code == 200
    body = resp.json()
    assert body["gate_stats"]["lint"]["total"] == 2
    assert body["gate_stats"]["lint"]["pass"] == 1
