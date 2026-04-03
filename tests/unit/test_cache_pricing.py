"""Unit tests for per-model cache read-write pricing tiers."""

from __future__ import annotations

from bernstein.core.cost_tracker import CostTracker, estimate_cost


def test_estimate_cost_with_cache_breakdown():
    # sonnet: input $3, output $15, cache_read $0.3, cache_write $3.75 per 1M
    model = "sonnet"
    in_tokens = 100_000
    out_tokens = 50_000
    read_tokens = 200_000
    write_tokens = 10_000

    # Expected cost:
    # (100k/1M * 3) + (50k/1M * 15) + (200k/1M * 0.3) + (10k/1M * 3.75)
    # 0.3 + 0.75 + 0.06 + 0.0375 = 1.1475
    cost = estimate_cost(model, in_tokens, out_tokens, read_tokens, write_tokens)
    assert round(cost, 6) == 1.1475


def test_estimate_cost_fallback_to_input_rate():
    # gpt-5.4: input $2.5, output $15 per 1M. No cache pricing defined.
    # Should use input rate for cache tokens.
    model = "gpt-5.4"
    in_tokens = 100_000
    out_tokens = 50_000
    read_tokens = 200_000

    # Expected: (100k/1M * 2.5) + (50k/1M * 15) + (200k/1M * 2.5)
    # 0.25 + 0.75 + 0.5 = 1.5
    cost = estimate_cost(model, in_tokens, out_tokens, cache_read_tokens=read_tokens)
    assert round(cost, 6) == 1.5


def test_cost_tracker_accumulates_cache_breakdown():
    tracker = CostTracker(run_id="test-run")
    tracker.record(
        agent_id="A1",
        task_id="T1",
        model="sonnet",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=2000,
        cache_write_tokens=100,
    )

    report = tracker.report()
    model_data = next(m for m in report.per_model if m.model == "sonnet")
    assert model_data.cache_read_tokens == 2000
    assert model_data.cache_write_tokens == 100
    assert model_data.total_tokens == 1000 + 500 + 2000 + 100


def test_record_cumulative_with_cache():
    tracker = CostTracker(run_id="test-run")
    # First record
    tracker.record_cumulative(
        "A1",
        "T1",
        "sonnet",
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cache_read_tokens=2000,
        total_cache_write_tokens=100,
    )
    assert tracker.spent_usd > 0

    # Second record with delta
    delta = tracker.record_cumulative(
        "A1",
        "T1",
        "sonnet",
        total_input_tokens=1500,
        total_output_tokens=600,
        total_cache_read_tokens=3000,
        total_cache_write_tokens=200,
    )
    assert delta > 0

    report = tracker.report()
    model_data = next(m for m in report.per_model if m.model == "sonnet")
    assert model_data.cache_read_tokens == 3000
    assert model_data.cache_write_tokens == 200
    assert model_data.total_tokens == 1500 + 600 + 3000 + 200
