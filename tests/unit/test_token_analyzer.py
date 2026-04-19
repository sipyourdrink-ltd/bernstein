"""Tests for TokenUsageAnalyzer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.token_analyzer import (
    TokenAnalysis,
    TokenUsageAnalyzer,
    to_markdown,
)


def _make_record(
    task_id: str,
    title: str = "",
    model: str = "sonnet",
    tokens_prompt: int = 5000,
    tokens_completion: int = 1000,
    cost_usd: float = 0.0,
    status: str = "done",
) -> dict[str, Any]:
    """Helper: build a task metric record dict."""
    return {
        "task_id": task_id,
        "title": title or task_id,
        "model": model,
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "cost_usd": cost_usd,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Basic analysis
# ---------------------------------------------------------------------------


def test_analyze_basic_stats(tmp_path: Path) -> None:
    """Analyzer computes correct totals for simple records."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", tokens_prompt=10000, tokens_completion=2000, cost_usd=0.05),
        _make_record("t2", tokens_prompt=8000, tokens_completion=3000, cost_usd=0.03),
    ]
    result = analyzer.analyze(records)

    assert result.total_tokens_prompt == 18000
    assert result.total_tokens_completion == 5000
    assert result.total_cost_usd == pytest.approx(0.08)
    assert len(result.task_stats) == 2


def test_analyze_io_ratio(tmp_path: Path) -> None:
    """IO ratio is computed correctly."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", tokens_prompt=9000, tokens_completion=3000),
    ]
    result = analyzer.analyze(records)
    assert result.overall_io_ratio == pytest.approx(3.0)
    assert result.task_stats[0].io_ratio == pytest.approx(3.0)


def test_analyze_zero_output_ratio(tmp_path: Path) -> None:
    """IO ratio caps at 999.0 when output is zero."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", tokens_prompt=5000, tokens_completion=0),
    ]
    result = analyzer.analyze(records)
    assert result.task_stats[0].io_ratio == pytest.approx(999.0)


def test_analyze_deduplicates_by_task_id(tmp_path: Path) -> None:
    """When multiple records share the same task_id, only the last is kept."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", tokens_prompt=1000, tokens_completion=100, cost_usd=0.01),
        _make_record("t1", tokens_prompt=2000, tokens_completion=200, cost_usd=0.02),
    ]
    result = analyzer.analyze(records)
    assert len(result.task_stats) == 1
    assert result.task_stats[0].tokens_prompt == 2000


# ---------------------------------------------------------------------------
# Waste pattern detection
# ---------------------------------------------------------------------------


def test_detect_high_io_ratio(tmp_path: Path) -> None:
    """Tasks with input:output ratio above threshold are flagged."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record(
            "bloated",
            title="Bloated prompt task",
            tokens_prompt=50000,
            tokens_completion=200,
        ),
    ]
    result = analyzer.analyze(records)

    high_ratio_patterns = [wp for wp in result.waste_patterns if wp.pattern == "high_io_ratio"]
    assert len(high_ratio_patterns) == 1
    assert high_ratio_patterns[0].task_id == "bloated"
    assert "reducing context" in high_ratio_patterns[0].detail


def test_detect_minimal_output(tmp_path: Path) -> None:
    """Tasks with very low output tokens are flagged."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record(
            "tiny",
            title="Tiny output task",
            tokens_prompt=10000,
            # Below MINIMAL_OUTPUT_THRESHOLD but > 0 (and ratio < 10 to avoid
            # triggering the high-ratio pattern too).
            tokens_completion=50,
        ),
    ]
    result = analyzer.analyze(records)

    minimal_patterns = [wp for wp in result.waste_patterns if wp.pattern == "minimal_output"]
    assert len(minimal_patterns) == 1
    assert minimal_patterns[0].task_id == "tiny"
    assert "50 output tokens" in minimal_patterns[0].detail


def test_detect_repeated_retries(tmp_path: Path) -> None:
    """Multiple tasks with the same title are flagged as retries."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", title="Fix login bug", tokens_prompt=5000, tokens_completion=2000),
        _make_record("t2", title="Fix login bug", tokens_prompt=5000, tokens_completion=2000),
        _make_record("t3", title="Fix login bug", tokens_prompt=5000, tokens_completion=2000),
    ]
    result = analyzer.analyze(records)

    retry_patterns = [wp for wp in result.waste_patterns if wp.pattern == "repeated_retry"]
    assert len(retry_patterns) == 1
    assert "3 attempts" in retry_patterns[0].detail


def test_no_waste_for_efficient_tasks(tmp_path: Path) -> None:
    """Efficient tasks produce no waste patterns."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", title="Good task", tokens_prompt=3000, tokens_completion=1000),
    ]
    result = analyzer.analyze(records)

    assert len(result.waste_patterns) == 0


# ---------------------------------------------------------------------------
# Model spend
# ---------------------------------------------------------------------------


def test_model_spend_aggregation(tmp_path: Path) -> None:
    """Per-model spend is aggregated and sorted by cost descending."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", model="opus", tokens_prompt=10000, tokens_completion=2000, cost_usd=0.10),
        _make_record("t2", model="sonnet", tokens_prompt=5000, tokens_completion=1000, cost_usd=0.03),
        _make_record("t3", model="opus", tokens_prompt=8000, tokens_completion=1500, cost_usd=0.08),
    ]
    result = analyzer.analyze(records)

    assert len(result.model_spend) == 2
    # Opus first (more expensive total).
    assert result.model_spend[0].model == "opus"
    assert result.model_spend[0].task_count == 2
    assert result.model_spend[0].total_cost_usd == pytest.approx(0.18)
    # Sonnet second.
    assert result.model_spend[1].model == "sonnet"
    assert result.model_spend[1].task_count == 1


# ---------------------------------------------------------------------------
# Top 5 hungry
# ---------------------------------------------------------------------------


def test_top5_sorted_by_total_tokens(tmp_path: Path) -> None:
    """Top 5 hungry tasks are sorted by total token count descending."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [_make_record(f"t{i}", tokens_prompt=i * 1000, tokens_completion=i * 100) for i in range(1, 8)]
    result = analyzer.analyze(records)

    assert len(result.top_5_hungry) == 5
    # Highest first.
    assert result.top_5_hungry[0].task_id == "t7"
    assert result.top_5_hungry[4].task_id == "t3"


# ---------------------------------------------------------------------------
# Cost fallback
# ---------------------------------------------------------------------------


def test_cost_computed_from_model_pricing(tmp_path: Path) -> None:
    """When cost_usd is 0 but tokens exist, cost is computed from pricing."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", model="sonnet", tokens_prompt=1_000_000, tokens_completion=100_000, cost_usd=0.0),
    ]
    result = analyzer.analyze(records)

    # sonnet: input $3/1M, output $15/1M -> 3.0 + 1.5 = 4.5
    assert abs(result.task_stats[0].cost_usd - 4.5) < 0.01


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def test_markdown_output_contains_sections(tmp_path: Path) -> None:
    """Markdown report contains all expected sections."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    records = [
        _make_record("t1", title="Task one", tokens_prompt=50000, tokens_completion=200, cost_usd=0.05),
        _make_record("t2", title="Task two", tokens_prompt=3000, tokens_completion=1000, cost_usd=0.02),
    ]
    analysis = analyzer.analyze(records)
    md = to_markdown(analysis)

    assert "# Token Usage Report" in md
    assert "## Summary" in md
    assert "## Spend by Model" in md
    assert "## Top 5 Most Token-Hungry Tasks" in md
    assert "## Suggestions" in md
    assert "Task one" in md
    assert "reducing context" in md


def test_markdown_no_waste_message(tmp_path: Path) -> None:
    """Markdown report says 'no waste' when there are no patterns."""
    analysis = TokenAnalysis(
        total_tokens_prompt=3000,
        total_tokens_completion=1000,
        total_cost_usd=0.01,
        overall_io_ratio=3.0,
    )
    md = to_markdown(analysis)
    assert "No waste patterns detected" in md


def test_markdown_efficiency_labels(tmp_path: Path) -> None:
    """Efficiency label is 'good' when ratio <= 3, 'high' otherwise."""
    good = TokenAnalysis(overall_io_ratio=2.5)
    assert "good" in to_markdown(good)

    bad = TokenAnalysis(overall_io_ratio=5.0)
    assert "high" in to_markdown(bad)


# ---------------------------------------------------------------------------
# Empty data
# ---------------------------------------------------------------------------


def test_analyze_empty_records(tmp_path: Path) -> None:
    """Analyzer handles empty input gracefully."""
    analyzer = TokenUsageAnalyzer(tmp_path)
    result = analyzer.analyze([])

    assert result.total_tokens_prompt == 0
    assert result.total_tokens_completion == 0
    assert result.total_cost_usd == pytest.approx(0.0)
    assert len(result.task_stats) == 0
    assert len(result.waste_patterns) == 0


def test_analyze_from_disk(tmp_path: Path) -> None:
    """Analyzer loads records from tasks.jsonl on disk."""
    import json

    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)
    tasks_file = metrics_dir / "tasks.jsonl"
    records = [
        _make_record("disk-t1", tokens_prompt=5000, tokens_completion=1000, cost_usd=0.03),
    ]
    tasks_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    analyzer = TokenUsageAnalyzer(tmp_path)
    result = analyzer.analyze()  # No records arg — reads from disk.

    assert len(result.task_stats) == 1
    assert result.task_stats[0].task_id == "disk-t1"
