"""Tests for the evaluation framework (per-model accuracy tracking)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.observability.eval_store import (
    EvalRecord,
    ModelAccuracy,
    append_eval_record,
    benchmark_summary,
    head_to_head,
    load_eval_records,
    per_model_accuracy,
    per_role_accuracy,
)


def _make_record(
    task_id: str = "t1",
    model: str = "opus",
    role: str = "backend",
    complexity: str = "medium",
    result: str = "pass",
    duration_s: float = 1.0,
    cost_usd: float = 0.01,
    step_count: int = 1,
) -> EvalRecord:
    return EvalRecord(
        task_id=task_id,
        model=model,
        role=role,
        complexity=complexity,
        result=result,  # type: ignore[arg-type]
        duration_s=duration_s,
        cost_usd=cost_usd,
        step_count=step_count,
        quality_gate_results={"tests": True},
    )


# ---------- append / load roundtrip ----------


def test_append_creates_metrics_dir(tmp_path: Path) -> None:
    metrics_dir = tmp_path / "metrics"
    assert not metrics_dir.exists()
    append_eval_record(_make_record(), metrics_dir)
    assert metrics_dir.exists()
    assert (metrics_dir / "evaluations.jsonl").exists()


def test_append_and_load_roundtrip(tmp_path: Path) -> None:
    record = _make_record(task_id="t-42", model="sonnet", duration_s=3.5, cost_usd=0.05)
    append_eval_record(record, tmp_path)
    loaded = load_eval_records(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].task_id == "t-42"
    assert loaded[0].model == "sonnet"
    assert loaded[0].duration_s == pytest.approx(3.5)
    assert loaded[0].cost_usd == pytest.approx(0.05)


def test_append_multiple_records(tmp_path: Path) -> None:
    for i in range(5):
        append_eval_record(_make_record(task_id=f"t-{i}"), tmp_path)
    loaded = load_eval_records(tmp_path)
    assert len(loaded) == 5
    assert [r.task_id for r in loaded] == [f"t-{i}" for i in range(5)]


def test_append_returns_path(tmp_path: Path) -> None:
    path = append_eval_record(_make_record(), tmp_path)
    assert path == tmp_path / "evaluations.jsonl"
    assert path.exists()


def test_jsonl_format_sorted_keys(tmp_path: Path) -> None:
    append_eval_record(_make_record(task_id="k1"), tmp_path)
    content = (tmp_path / "evaluations.jsonl").read_text()
    parsed = json.loads(content.strip())
    assert parsed["task_id"] == "k1"
    # sort_keys=True means keys in alphabetical order
    keys = list(parsed.keys())
    assert keys == sorted(keys)


# ---------- load edge cases ----------


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_eval_records(tmp_path) == []


def test_load_with_limit_returns_most_recent(tmp_path: Path) -> None:
    for i in range(10):
        append_eval_record(_make_record(task_id=f"t-{i}"), tmp_path)
    loaded = load_eval_records(tmp_path, limit=3)
    assert len(loaded) == 3
    assert [r.task_id for r in loaded] == ["t-7", "t-8", "t-9"]


def test_load_limit_zero_returns_all(tmp_path: Path) -> None:
    for i in range(4):
        append_eval_record(_make_record(task_id=f"t-{i}"), tmp_path)
    loaded = load_eval_records(tmp_path, limit=0)
    assert len(loaded) == 4


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    append_eval_record(_make_record(task_id="a"), tmp_path)
    path = tmp_path / "evaluations.jsonl"
    # Inject blank line
    with path.open("a", encoding="utf-8") as f:
        f.write("\n   \n")
    append_eval_record(_make_record(task_id="b"), tmp_path)
    loaded = load_eval_records(tmp_path)
    assert [r.task_id for r in loaded] == ["a", "b"]


def test_load_skips_malformed_json(tmp_path: Path) -> None:
    append_eval_record(_make_record(task_id="good"), tmp_path)
    path = tmp_path / "evaluations.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
        f.write('{"missing_required_fields": true}\n')
    append_eval_record(_make_record(task_id="good2"), tmp_path)
    loaded = load_eval_records(tmp_path)
    assert [r.task_id for r in loaded] == ["good", "good2"]


# ---------- per_model_accuracy ----------


def test_per_model_accuracy_empty_returns_empty_dict() -> None:
    assert per_model_accuracy([]) == {}


def test_per_model_accuracy_pass_rate() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="opus", result="pass"),
        _make_record(model="opus", result="fail"),
        _make_record(model="opus", result="retry"),
    ]
    acc = per_model_accuracy(records)
    assert "opus" in acc
    assert acc["opus"].total == 4
    assert acc["opus"].passed == 2
    assert acc["opus"].failed == 1
    assert acc["opus"].retried == 1
    assert acc["opus"].pass_rate == pytest.approx(0.5)


def test_per_model_accuracy_pass_rate_zero_when_no_records() -> None:
    acc = ModelAccuracy(model="haiku")
    assert acc.pass_rate == 0


def test_per_model_accuracy_pass_rate_perfect() -> None:
    records = [_make_record(model="opus", result="pass") for _ in range(3)]
    acc = per_model_accuracy(records)
    assert acc["opus"].pass_rate == pytest.approx(1.0)


def test_per_model_accuracy_averages_duration_and_cost() -> None:
    records = [
        _make_record(model="opus", duration_s=2.0, cost_usd=0.10),
        _make_record(model="opus", duration_s=4.0, cost_usd=0.20),
        _make_record(model="opus", duration_s=6.0, cost_usd=0.30),
    ]
    acc = per_model_accuracy(records)
    assert acc["opus"].avg_duration_s == pytest.approx(4.0)
    assert acc["opus"].avg_cost_usd == pytest.approx(0.20)


def test_per_model_accuracy_splits_by_model() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="sonnet", result="fail"),
        _make_record(model="haiku", result="pass"),
    ]
    acc = per_model_accuracy(records)
    assert set(acc.keys()) == {"opus", "sonnet", "haiku"}
    assert acc["opus"].passed == 1
    assert acc["sonnet"].failed == 1
    assert acc["haiku"].passed == 1


# ---------- per_role_accuracy ----------


def test_per_role_accuracy_groups_by_role() -> None:
    records = [
        _make_record(role="backend", result="pass"),
        _make_record(role="backend", result="fail"),
        _make_record(role="qa", result="pass"),
    ]
    acc = per_role_accuracy(records)
    assert set(acc.keys()) == {"backend", "qa"}
    assert acc["backend"].total == 2
    assert acc["backend"].passed == 1
    assert acc["backend"].failed == 1
    assert acc["qa"].total == 1


def test_per_role_accuracy_empty_returns_empty() -> None:
    assert per_role_accuracy([]) == {}


def test_per_role_accuracy_averages_duration() -> None:
    records = [
        _make_record(role="backend", duration_s=1.0, cost_usd=0.01),
        _make_record(role="backend", duration_s=3.0, cost_usd=0.03),
    ]
    acc = per_role_accuracy(records)
    assert acc["backend"].avg_duration_s == pytest.approx(2.0)
    assert acc["backend"].avg_cost_usd == pytest.approx(0.02)


# ---------- head_to_head ----------


def test_head_to_head_filters_to_two_models() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="sonnet", result="pass"),
        _make_record(model="haiku", result="fail"),
        _make_record(model="gemini", result="pass"),
    ]
    result = head_to_head(records, "opus", "sonnet")
    assert set(result.keys()) == {"opus", "sonnet"}
    assert "haiku" not in result
    assert "gemini" not in result


def test_head_to_head_empty_when_no_match() -> None:
    records = [_make_record(model="opus")]
    result = head_to_head(records, "sonnet", "haiku")
    assert result == {}


def test_head_to_head_preserves_counts() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="opus", result="pass"),
        _make_record(model="sonnet", result="fail"),
    ]
    result = head_to_head(records, "opus", "sonnet")
    assert result["opus"].passed == 2
    assert result["sonnet"].failed == 1


# ---------- benchmark_summary ----------


def test_benchmark_summary_separates_baseline_from_routed() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="opus", result="fail"),
        _make_record(model="sonnet", result="pass"),
        _make_record(model="haiku", result="pass"),
    ]
    summary = benchmark_summary(records, baseline_model="opus")
    assert summary["baseline_model"] == "opus"
    baseline = summary["baseline"]
    routed = summary["routed"]
    assert isinstance(baseline, dict)
    assert isinstance(routed, dict)
    assert baseline["count"] == 2
    assert baseline["pass_rate"] == pytest.approx(0.5)
    assert routed["count"] == 2
    assert routed["pass_rate"] == pytest.approx(1.0)


def test_benchmark_summary_empty_records() -> None:
    summary = benchmark_summary([], baseline_model="opus")
    baseline = summary["baseline"]
    routed = summary["routed"]
    assert isinstance(baseline, dict)
    assert isinstance(routed, dict)
    assert baseline["count"] == 0
    assert baseline["pass_rate"] == 0
    assert routed["count"] == 0
    assert routed["pass_rate"] == 0


def test_benchmark_summary_only_baseline() -> None:
    records = [
        _make_record(model="opus", result="pass"),
        _make_record(model="opus", result="pass"),
    ]
    summary = benchmark_summary(records, baseline_model="opus")
    baseline = summary["baseline"]
    routed = summary["routed"]
    assert isinstance(baseline, dict)
    assert isinstance(routed, dict)
    assert baseline["count"] == 2
    assert routed["count"] == 0


def test_benchmark_summary_duration_and_cost() -> None:
    records = [
        _make_record(model="opus", duration_s=10.0, cost_usd=0.50),
        _make_record(model="sonnet", duration_s=2.0, cost_usd=0.05),
        _make_record(model="haiku", duration_s=4.0, cost_usd=0.15),
    ]
    summary = benchmark_summary(records, baseline_model="opus")
    baseline = summary["baseline"]
    routed = summary["routed"]
    assert isinstance(baseline, dict)
    assert isinstance(routed, dict)
    assert baseline["avg_duration_s"] == pytest.approx(10.0)
    assert baseline["avg_cost_usd"] == pytest.approx(0.50)
    assert routed["avg_duration_s"] == pytest.approx(3.0)
    assert routed["avg_cost_usd"] == pytest.approx(0.10)


# ---------- EvalRecord dataclass ----------


def test_eval_record_default_timestamp_set() -> None:
    r = _make_record()
    assert r.timestamp > 0.0


def test_eval_record_default_quality_gates_empty() -> None:
    r = EvalRecord(
        task_id="t",
        model="opus",
        role="backend",
        complexity="low",
        result="pass",
    )
    assert r.quality_gate_results == {}
    assert r.duration_s == 0
    assert r.cost_usd == 0
    assert r.step_count == 0


def test_eval_record_quality_gate_results_roundtrip(tmp_path: Path) -> None:
    record = EvalRecord(
        task_id="t",
        model="opus",
        role="qa",
        complexity="high",
        result="pass",
        quality_gate_results={"tests": True, "lint": False, "types": True},
    )
    append_eval_record(record, tmp_path)
    loaded = load_eval_records(tmp_path)
    assert loaded[0].quality_gate_results == {"tests": True, "lint": False, "types": True}
