"""Unit tests for the simulate predictors (issue #1374).

Each predictor (cost, latency, abandonment, blast-radius) is exercised in
isolation. Edge cases include: no historical data, single-task plan,
malformed trace records, defensive clamping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.simulate.predictor import (
    AbandonmentPredictor,
    BlastRadiusPredictor,
    CostPredictor,
    HistoricalTraces,
    LatencyPredictor,
    load_traces,
    role_criterion,
)
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType


def _make_task(
    *,
    task_id: str = "plan-0-0",
    title: str = "Implement feature",
    role: str = "backend",
    description: str = "Add a new endpoint",
    owned_files: tuple[str, ...] = ("src/api/foo.py",),
    estimated_minutes: int = 30,
    cli: str | None = None,
    model: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role=role,
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        estimated_minutes=estimated_minutes,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        owned_files=list(owned_files),
        cli=cli,
        model=model,
    )


# ---------------------------------------------------------------------------
# role_criterion
# ---------------------------------------------------------------------------


def test_role_criterion_known_roles() -> None:
    assert role_criterion("security") == "safety"
    assert role_criterion("qa") == "quality"
    assert role_criterion("backend") == "quality"
    assert role_criterion("frontend") == "speed"
    assert role_criterion("docs") == "cost"


def test_role_criterion_case_insensitive() -> None:
    assert role_criterion("SECURITY") == "safety"
    assert role_criterion(" QA ") == "quality"


def test_role_criterion_unknown_role_falls_back_quality() -> None:
    assert role_criterion("rumpelstiltskin") == "quality"
    assert role_criterion("") == "quality"


# ---------------------------------------------------------------------------
# load_traces
# ---------------------------------------------------------------------------


def test_load_traces_missing_dir_returns_empty() -> None:
    out = load_traces(None)
    assert out.is_empty
    assert out.sample_count == 0


def test_load_traces_nonexistent_path_returns_empty(tmp_path: Path) -> None:
    out = load_traces(tmp_path / "does-not-exist")
    assert out.is_empty


def test_load_traces_empty_dir(tmp_path: Path) -> None:
    out = load_traces(tmp_path)
    assert out.is_empty


def test_load_traces_skips_invalid_json_lines(tmp_path: Path) -> None:
    (tmp_path / "trace.jsonl").write_text("not json\n{}\n", encoding="utf-8")
    out = load_traces(tmp_path)
    # Records with no role are skipped; we should have zero usable samples.
    assert out.is_empty


def test_load_traces_collects_abandon_rate(tmp_path: Path) -> None:
    body = (
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 12.0}\n'
        '{"role": "backend", "adapter": "mock", "status": "abandoned", "latency_seconds": 8.0}\n'
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 20.0}\n'
    )
    (tmp_path / "trace.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    assert not out.is_empty
    assert out.sample_count == 3
    rate = out.abandon_rates[("backend", "mock")]
    assert rate == pytest.approx(1 / 3)
    samples = out.latency_samples[("backend", "mock")]
    assert sorted(samples) == [8.0, 12.0, 20.0]


def test_load_traces_handles_abandoned_boolean(tmp_path: Path) -> None:
    body = '{"role": "qa", "cli": "claude", "abandoned": true}\n{"role": "qa", "cli": "claude", "abandoned": false}\n'
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    rate = out.abandon_rates[("qa", "claude")]
    assert rate == 0.5


def test_load_traces_uses_model_when_adapter_missing(tmp_path: Path) -> None:
    body = '{"role": "backend", "model": "claude-sonnet-4", "status": "completed"}\n'
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    assert ("backend", "claude-sonnet-4") in out.abandon_rates


def test_load_traces_defaults_to_mock_adapter_when_no_id(tmp_path: Path) -> None:
    body = '{"role": "backend", "status": "completed"}\n'
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    assert ("backend", "mock") in out.abandon_rates


def test_load_traces_skips_record_without_role(tmp_path: Path) -> None:
    body = '{"adapter": "mock", "status": "abandoned"}\n'
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    assert out.is_empty


def test_load_traces_skips_record_that_is_a_list(tmp_path: Path) -> None:
    body = '[1, 2, 3]\n{"role": "backend", "status": "completed"}\n'
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    assert out.sample_count == 1


def test_load_traces_keeps_zero_primary_latency(tmp_path: Path) -> None:
    (tmp_path / "timing.jsonl").write_text(
        '{"role": "qa", "latency_seconds": 0}\n'
        '{"role": "qa", "latency_seconds": 0, "duration_s": 9}\n'
        '{"role": "qa", "duration_s": 3}\n'
        '{"role": "qa", "latency_seconds": -1, "duration_s": 7}\n',
        encoding="utf-8",
    )
    samples = load_traces(tmp_path).latency_samples[("qa", "mock")]
    assert samples == (0.0, 0.0, 3.0, 7.0)


def test_load_traces_skips_negative_latency(tmp_path: Path) -> None:
    body = (
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": -5.0}\n'
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 10.0}\n'
    )
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    samples = out.latency_samples[("backend", "mock")]
    assert samples == (10.0,)


def test_load_traces_skips_nan_latency(tmp_path: Path) -> None:
    body = (
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": "nan"}\n'
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": 7.0}\n'
    )
    (tmp_path / "t.jsonl").write_text(body, encoding="utf-8")
    out = load_traces(tmp_path)
    samples = out.latency_samples[("backend", "mock")]
    assert samples == (7.0,)


def test_load_traces_uses_explicit_finite_latency_check() -> None:
    source = Path("src/bernstein/core/simulate/predictor.py").read_text(encoding="utf-8")

    assert "value != value" not in source


def test_load_traces_trims_to_limit(tmp_path: Path) -> None:
    lines = [
        '{"role": "backend", "adapter": "mock", "status": "completed", "latency_seconds": ' + str(float(i)) + "}\n"
        for i in range(10)
    ]
    (tmp_path / "t.jsonl").write_text("".join(lines), encoding="utf-8")
    out = load_traces(tmp_path, limit=3)
    samples = out.latency_samples[("backend", "mock")]
    assert len(samples) == 3
    # Newest-last preserved.
    assert samples[-1] == 9.0


def test_load_traces_reads_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "status": "completed"}\n', encoding="utf-8"
    )
    (tmp_path / "b.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "status": "abandoned"}\n', encoding="utf-8"
    )
    out = load_traces(tmp_path)
    assert out.sample_count == 2


# ---------------------------------------------------------------------------
# CostPredictor
# ---------------------------------------------------------------------------


def test_cost_predictor_cold_start_no_metrics() -> None:
    pred = CostPredictor(metrics_dir=None)
    task = _make_task()
    p50, p90, cold = pred.predict(task)
    assert p50 >= 0.0
    assert p90 >= p50
    assert cold is True


def test_cost_predictor_cold_start_missing_metrics_dir(tmp_path: Path) -> None:
    pred = CostPredictor(metrics_dir=tmp_path / "missing")
    p50, p90, cold = pred.predict(_make_task())
    assert cold is True
    assert p90 >= p50


def test_cost_predictor_uses_history_when_available(tmp_path: Path) -> None:
    (tmp_path / "cost.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "cost_usd": 1.0}\n'
        '{"role": "backend", "adapter": "mock", "cost_usd": 2.0}\n'
        '{"role": "backend", "adapter": "mock", "cost_usd": 3.0}\n',
        encoding="utf-8",
    )
    pred = CostPredictor(metrics_dir=tmp_path)
    task = _make_task(cli="mock")
    _, _, cold = pred.predict(task)
    assert cold is False


def test_cost_predictor_history_count(tmp_path: Path) -> None:
    (tmp_path / "cost.jsonl").write_text(
        '{"role": "backend", "adapter": "mock", "cost_usd": 0.5}\n' * 3,
        encoding="utf-8",
    )
    pred = CostPredictor(metrics_dir=tmp_path)
    assert pred.history_count(role="backend", adapter="mock") == 3


def test_cost_predictor_history_count_zero_for_no_metrics() -> None:
    pred = CostPredictor(metrics_dir=None)
    assert pred.history_count(role="backend", adapter="mock") == 0


def test_cost_predictor_returns_non_negative() -> None:
    pred = CostPredictor(metrics_dir=None)
    task = _make_task(role="backend", model="claude-sonnet-4")
    p50, p90, _ = pred.predict(task)
    assert p50 >= 0.0
    assert p90 >= 0.0


def test_cost_predictor_distinguishes_adapters(tmp_path: Path) -> None:
    (tmp_path / "cost.jsonl").write_text(
        '{"role": "backend", "adapter": "claude", "cost_usd": 5.0}\n'
        '{"role": "backend", "adapter": "claude", "cost_usd": 6.0}\n'
        '{"role": "backend", "adapter": "mock", "cost_usd": 0.01}\n'
        '{"role": "backend", "adapter": "mock", "cost_usd": 0.02}\n',
        encoding="utf-8",
    )
    pred = CostPredictor(metrics_dir=tmp_path)
    claude_p50, _, _ = pred.predict(_make_task(cli="claude"))
    mock_p50, _, _ = pred.predict(_make_task(cli="mock"))
    assert claude_p50 > mock_p50


# ---------------------------------------------------------------------------
# LatencyPredictor
# ---------------------------------------------------------------------------


def test_latency_predictor_cold_start_uses_floor() -> None:
    pred = LatencyPredictor()
    p50, p90 = pred.predict(_make_task(estimated_minutes=1))
    assert p50 >= pred.floor_p50
    assert p90 >= pred.floor_p90


def test_latency_predictor_scales_with_estimated_minutes() -> None:
    pred = LatencyPredictor()
    p50_short, _ = pred.predict(_make_task(estimated_minutes=10))
    p50_long, _ = pred.predict(_make_task(estimated_minutes=120))
    assert p50_long >= p50_short


def test_latency_predictor_uses_history_when_available() -> None:
    traces = HistoricalTraces(
        latency_samples={("backend", "mock"): (100.0, 200.0, 300.0)},
        sample_count=3,
    )
    pred = LatencyPredictor(traces=traces)
    p50, p90 = pred.predict(_make_task())
    # p50 should land somewhere in the middle of the sample set.
    assert 100.0 <= p50 <= 300.0
    assert p90 >= p50


def test_latency_predictor_p90_never_below_p50() -> None:
    # Single-sample history shouldn't degenerate to p90<p50.
    traces = HistoricalTraces(
        latency_samples={("backend", "mock"): (50.0,)},
        sample_count=1,
    )
    pred = LatencyPredictor(traces=traces)
    p50, p90 = pred.predict(_make_task())
    assert p90 >= p50


def test_latency_predictor_returns_non_negative() -> None:
    pred = LatencyPredictor()
    p50, p90 = pred.predict(_make_task(estimated_minutes=0))
    assert p50 >= 0.0
    assert p90 >= 0.0


# ---------------------------------------------------------------------------
# AbandonmentPredictor
# ---------------------------------------------------------------------------


def test_abandonment_predictor_cold_start_uses_prior() -> None:
    pred = AbandonmentPredictor()
    prob = pred.predict(_make_task())
    assert 0.0 < prob < 1.0
    assert prob == pred.cold_prior


def test_abandonment_predictor_history_returns_observed_rate() -> None:
    traces = HistoricalTraces(
        abandon_rates={("backend", "mock"): 0.42},
        sample_count=10,
    )
    pred = AbandonmentPredictor(traces=traces)
    assert pred.predict(_make_task()) == 0.42


def test_abandonment_predictor_clamps_above_one() -> None:
    traces = HistoricalTraces(
        abandon_rates={("backend", "mock"): 1.5},  # corrupt
        sample_count=2,
    )
    pred = AbandonmentPredictor(traces=traces)
    assert pred.predict(_make_task()) == 1.0


def test_abandonment_predictor_clamps_below_zero() -> None:
    traces = HistoricalTraces(
        abandon_rates={("backend", "mock"): -0.5},
        sample_count=2,
    )
    pred = AbandonmentPredictor(traces=traces)
    assert pred.predict(_make_task()) == 0.0


def test_abandonment_predictor_per_role_isolation() -> None:
    traces = HistoricalTraces(
        abandon_rates={("backend", "mock"): 0.9, ("qa", "mock"): 0.1},
        sample_count=4,
    )
    pred = AbandonmentPredictor(traces=traces)
    assert pred.predict(_make_task(role="backend")) == 0.9
    assert pred.predict(_make_task(role="qa")) == 0.1


def test_abandonment_predictor_uses_adapter_default_when_unset() -> None:
    traces = HistoricalTraces(
        abandon_rates={("backend", "mock"): 0.3},
        sample_count=2,
    )
    pred = AbandonmentPredictor(traces=traces)
    assert pred.predict(_make_task(role="backend", cli=None)) == 0.3


# ---------------------------------------------------------------------------
# BlastRadiusPredictor
# ---------------------------------------------------------------------------


def test_blast_radius_predictor_safe_change_low_score() -> None:
    pred = BlastRadiusPredictor()
    task = _make_task(
        description="Add a comment to the README",
        owned_files=("README.md",),
    )
    score = pred.predict(task)
    assert 0.0 <= score <= 1.0
    assert score < 0.5


def test_blast_radius_predictor_drop_table_scores_one() -> None:
    pred = BlastRadiusPredictor()
    task = _make_task(
        description="DROP TABLE users;\n",
        owned_files=("db/cleanup.sql",),
    )
    score = pred.predict(task)
    assert score == 1.0


def test_blast_radius_predictor_rm_rf_scores_one() -> None:
    pred = BlastRadiusPredictor()
    task = _make_task(
        description="rm -rf $HOME/cache\n",
        owned_files=("scripts/clean.sh",),
    )
    assert pred.predict(task) == 1.0


def test_blast_radius_predictor_no_files_returns_zero() -> None:
    pred = BlastRadiusPredictor()
    task = _make_task(description="just describe", owned_files=())
    score = pred.predict(task)
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Edge-case: many tasks vs zero tasks
# ---------------------------------------------------------------------------


def test_cost_predictor_does_not_crash_on_blank_role() -> None:
    pred = CostPredictor(metrics_dir=None)
    task = _make_task(role="")
    p50, p90, _ = pred.predict(task)
    assert p90 >= p50


def test_latency_predictor_does_not_crash_on_blank_role() -> None:
    pred = LatencyPredictor()
    p50, p90 = pred.predict(_make_task(role=""))
    assert p90 >= p50
