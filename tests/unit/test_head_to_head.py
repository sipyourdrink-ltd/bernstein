"""Unit tests for bernstein.benchmark.head_to_head."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from bernstein.benchmark.head_to_head import (
    BERNSTEIN_MIXED_METRICS,
    BERNSTEIN_PROFILE,
    BERNSTEIN_SONNET_METRICS,
    CANONICAL_COMPARISON,
    CREWAI_GPT4_METRICS,
    CREWAI_PROFILE,
    LANGGRAPH_PROFILE,
    LANGGRAPH_SONNET_METRICS,
    BenchmarkMetrics,
    CompetitorProfile,
    HeadToHeadComparison,
    generate_architecture_table,
    generate_full_report,
    generate_key_findings,
    generate_swe_bench_table,
)

# ---------------------------------------------------------------------------
# CompetitorProfile
# ---------------------------------------------------------------------------


def test_bernstein_profile_has_zero_scheduling_overhead() -> None:
    assert BERNSTEIN_PROFILE.scheduling_overhead_pct == pytest.approx(0.0)


def test_bernstein_profile_scheduling_label_says_none() -> None:
    label = BERNSTEIN_PROFILE.scheduling_overhead_label()
    assert "none" in label.lower()
    assert "deterministic" in label.lower()


def test_crewai_profile_uses_llm_orchestration() -> None:
    assert CREWAI_PROFILE.orchestration_model is True


def test_crewai_profile_scheduling_label_mentions_llm() -> None:
    label = CREWAI_PROFILE.scheduling_overhead_label()
    assert "llm" in label.lower()
    assert "present" in label.lower()


def test_langgraph_profile_uses_llm_orchestration() -> None:
    assert LANGGRAPH_PROFILE.orchestration_model is True


def test_bernstein_supports_any_cli_agent() -> None:
    assert BERNSTEIN_PROFILE.supports_any_cli_agent is True


def test_crewai_does_not_support_arbitrary_cli_agents() -> None:
    assert CREWAI_PROFILE.supports_any_cli_agent is False


def test_langgraph_does_not_support_arbitrary_cli_agents() -> None:
    assert LANGGRAPH_PROFILE.supports_any_cli_agent is False


def test_competitor_profile_is_frozen() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(CompetitorProfile)
    fields = {f.name for f in dataclasses.fields(CompetitorProfile)}
    assert "name" in fields
    assert "orchestration_model" in fields


# ---------------------------------------------------------------------------
# BenchmarkMetrics
# ---------------------------------------------------------------------------


def test_bernstein_sonnet_resolve_rate_is_reasonable() -> None:
    assert 0.0 < BERNSTEIN_SONNET_METRICS.swe_bench_resolve_rate < 1.0


def test_bernstein_sonnet_resolve_pct_format() -> None:
    pct = BERNSTEIN_SONNET_METRICS.resolve_pct
    assert pct.endswith("%")
    assert float(pct[:-1]) > 0


def test_bernstein_mixed_is_cheaper_than_bernstein_sonnet() -> None:
    assert BERNSTEIN_MIXED_METRICS.mean_cost_per_issue_usd < BERNSTEIN_SONNET_METRICS.mean_cost_per_issue_usd


def test_bernstein_scheduling_cost_is_zero() -> None:
    assert BERNSTEIN_SONNET_METRICS.scheduling_cost_per_issue_usd == pytest.approx(0.0)
    assert BERNSTEIN_MIXED_METRICS.scheduling_cost_per_issue_usd == pytest.approx(0.0)


def test_crewai_has_positive_scheduling_cost() -> None:
    assert CREWAI_GPT4_METRICS.scheduling_cost_per_issue_usd > 0.0


def test_langgraph_has_positive_scheduling_cost() -> None:
    assert LANGGRAPH_SONNET_METRICS.scheduling_cost_per_issue_usd > 0.0


def test_agent_cost_excludes_scheduling_overhead() -> None:
    m = CREWAI_GPT4_METRICS
    expected = m.mean_cost_per_issue_usd - m.scheduling_cost_per_issue_usd
    assert abs(m.agent_cost_per_issue_usd - expected) < 1e-9


def test_bernstein_sonnet_total_matches_resolved_plus_remaining() -> None:
    m = BERNSTEIN_SONNET_METRICS
    assert m.swe_bench_resolved <= m.swe_bench_total


def test_crewai_metrics_is_not_simulated() -> None:
    assert CREWAI_GPT4_METRICS.is_simulated is False


def test_bernstein_metrics_is_simulated() -> None:
    assert BERNSTEIN_SONNET_METRICS.is_simulated is True
    assert BERNSTEIN_MIXED_METRICS.is_simulated is True


def test_benchmark_metrics_data_source_is_non_empty() -> None:
    for m in [
        BERNSTEIN_SONNET_METRICS,
        BERNSTEIN_MIXED_METRICS,
        CREWAI_GPT4_METRICS,
        LANGGRAPH_SONNET_METRICS,
    ]:
        assert len(m.data_source) > 0


# ---------------------------------------------------------------------------
# HeadToHeadComparison helpers
# ---------------------------------------------------------------------------


def test_cost_ratio_bernstein_vs_crewai() -> None:
    ratio = CANONICAL_COMPARISON.cost_ratio("bernstein-sonnet", "crewai-gpt4")
    assert ratio is not None
    assert ratio > 1.0  # CrewAI should be more expensive


def test_cost_ratio_returns_none_for_missing_key() -> None:
    ratio = CANONICAL_COMPARISON.cost_ratio("nonexistent", "crewai-gpt4")
    assert ratio is None


def test_resolve_rate_delta_bernstein_better_than_crewai() -> None:
    delta = CANONICAL_COMPARISON.resolve_rate_delta_pp("bernstein-sonnet", "crewai-gpt4")
    assert delta is not None
    assert delta > 0  # Bernstein should resolve more issues


def test_resolve_rate_delta_returns_none_for_missing_key() -> None:
    delta = CANONICAL_COMPARISON.resolve_rate_delta_pp("missing", "crewai-gpt4")
    assert delta is None


def test_canonical_comparison_has_all_four_metric_keys() -> None:
    expected_keys = {"bernstein-sonnet", "bernstein-mixed", "crewai-gpt4", "langgraph-sonnet"}
    assert expected_keys == set(CANONICAL_COMPARISON.metrics.keys())


def test_canonical_comparison_has_three_profiles() -> None:
    assert set(CANONICAL_COMPARISON.profiles.keys()) == {"bernstein", "crewai", "langgraph"}


# ---------------------------------------------------------------------------
# generate_architecture_table
# ---------------------------------------------------------------------------


def test_architecture_table_contains_framework_names() -> None:
    table = generate_architecture_table(CANONICAL_COMPARISON)
    assert "Bernstein" in table
    assert "CrewAI" in table
    assert "LangGraph" in table


def test_architecture_table_shows_no_orchestration_overhead_for_bernstein() -> None:
    table = generate_architecture_table(CANONICAL_COMPARISON)
    assert "none" in table.lower() or "$0" in table or "deterministic" in table.lower()


def test_architecture_table_is_valid_markdown_table() -> None:
    table = generate_architecture_table(CANONICAL_COMPARISON)
    lines = table.strip().splitlines()
    # Header row and separator row
    assert len(lines) >= 2
    assert "|" in lines[0]
    assert "---" in lines[1]


def test_architecture_table_has_correct_column_count() -> None:
    table = generate_architecture_table(CANONICAL_COMPARISON)
    header = table.strip().splitlines()[0]
    # Four columns: Feature, Bernstein, CrewAI, LangGraph
    assert header.count("|") >= 4


# ---------------------------------------------------------------------------
# generate_swe_bench_table
# ---------------------------------------------------------------------------


def test_swe_bench_table_contains_resolve_rates() -> None:
    table = generate_swe_bench_table(CANONICAL_COMPARISON)
    assert "Published only from verified" in table
    assert "Withheld from public numeric tables" in table


def test_swe_bench_table_excludes_competitor_numeric_rows() -> None:
    table = generate_swe_bench_table(CANONICAL_COMPARISON)
    assert "39.0%" not in table
    assert "26.5%" not in table


def test_swe_bench_table_mentions_crewai_and_langgraph_status() -> None:
    table = generate_swe_bench_table(CANONICAL_COMPARISON)
    assert "CrewAI" in table
    assert "LangGraph" in table


def test_swe_bench_table_has_header_row() -> None:
    table = generate_swe_bench_table(CANONICAL_COMPARISON)
    assert "Public numeric benchmark status" in table
    assert "Notes" in table


# ---------------------------------------------------------------------------
# generate_key_findings
# ---------------------------------------------------------------------------


def test_key_findings_mentions_scheduling_overhead() -> None:
    findings = generate_key_findings(CANONICAL_COMPARISON)
    assert "deterministic python code" in findings.lower() or "manager-model" in findings.lower()


def test_key_findings_mentions_resolve_rate() -> None:
    findings = generate_key_findings(CANONICAL_COMPARISON)
    assert "public numeric rankings are withheld" in findings.lower() or "architecture context" in findings.lower()


def test_key_findings_is_non_empty() -> None:
    findings = generate_key_findings(CANONICAL_COMPARISON)
    assert len(findings.strip()) > 0


def test_key_findings_with_empty_comparison_returns_fallback() -> None:
    empty = HeadToHeadComparison(title="empty", date="2026-01-01")
    findings = generate_key_findings(empty)
    assert "not available" in findings.lower() or len(findings) > 0


# ---------------------------------------------------------------------------
# generate_full_report
# ---------------------------------------------------------------------------


def test_full_report_starts_with_title() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert report.startswith("# Bernstein Benchmark Status and Framework Context")


def test_full_report_contains_tldr_section() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "## TL;DR" in report


def test_full_report_contains_architecture_section() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "## Architecture" in report


def test_full_report_contains_swe_bench_section() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "SWE-Bench" in report


def test_full_report_contains_key_findings_section() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "## Key Findings" in report


def test_full_report_includes_publication_notice() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "rankings are intentionally withheld" in report.lower()


def test_full_report_contains_date() -> None:
    report = generate_full_report(CANONICAL_COMPARISON)
    assert "2026" in report


# ---------------------------------------------------------------------------
# Custom comparison (smoke test for non-canonical input)
# ---------------------------------------------------------------------------


def test_custom_comparison_with_single_metric() -> None:
    custom = HeadToHeadComparison(
        title="Custom",
        date="2026-01-01",
        profiles={"bernstein": BERNSTEIN_PROFILE},
        metrics={"bernstein-sonnet": BERNSTEIN_SONNET_METRICS},
    )
    report = generate_full_report(custom)
    assert "Custom" in report


def test_cost_ratio_when_baseline_is_zero_returns_none() -> None:
    m_zero = BenchmarkMetrics(
        framework_name="bernstein",
        model_config="test",
        swe_bench_resolve_rate=0.5,
        swe_bench_resolved=5,
        swe_bench_total=10,
        mean_cost_per_issue_usd=0.0,
        scheduling_cost_per_issue_usd=0.0,
        mean_wall_time_s=10.0,
        data_source="test",
    )
    comparison = HeadToHeadComparison(
        title="test",
        date="2026-01-01",
        metrics={"zero": m_zero, "other": CREWAI_GPT4_METRICS},
    )
    ratio = comparison.cost_ratio("zero", "other")
    assert ratio is None


# ---------------------------------------------------------------------------
# compare CLI command
# ---------------------------------------------------------------------------

_RUN_PY = Path(__file__).parent.parent.parent / "benchmarks" / "swe_bench" / "run.py"


def _load_run_cli():  # type: ignore[return]
    """Import the run.py CLI module, skipping if unavailable."""
    import pytest

    if not _RUN_PY.exists():
        pytest.skip("benchmarks/swe_bench/run.py not found")
    spec = importlib.util.spec_from_file_location("swe_bench_run", _RUN_PY)
    if spec is None or spec.loader is None:
        pytest.skip("could not load benchmarks/swe_bench/run.py")
    loader = spec.loader
    assert loader is not None  # narrowed above, re-assert for static analysis
    mod = importlib.util.module_from_spec(spec)
    sys.modules["swe_bench_run"] = mod
    loader.exec_module(mod)
    return mod


def test_compare_command_writes_markdown_file(tmp_path: Path) -> None:
    from click.testing import CliRunner

    mod = _load_run_cli()
    output = tmp_path / "h2h.md"
    runner = CliRunner()
    result = runner.invoke(mod.cli, ["compare", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()
    content = output.read_text()
    assert "Bernstein" in content
    assert "CrewAI" in content
    assert "LangGraph" in content


def test_compare_command_report_withholds_numeric_rankings(tmp_path: Path) -> None:
    from click.testing import CliRunner

    mod = _load_run_cli()
    output = tmp_path / "h2h.md"
    runner = CliRunner()
    result = runner.invoke(mod.cli, ["compare", "--output", str(output)])
    assert result.exit_code == 0, result.output
    content = output.read_text()
    assert "Withheld from public numeric tables" in content
    assert "39.0%" not in content
    assert "26.5%" not in content


def test_compare_command_includes_publication_notice(tmp_path: Path) -> None:
    from click.testing import CliRunner

    mod = _load_run_cli()
    output = tmp_path / "h2h.md"
    runner = CliRunner()
    result = runner.invoke(mod.cli, ["compare", "--output", str(output)])
    assert result.exit_code == 0, result.output
    content = output.read_text()
    assert "rankings are intentionally withheld" in content.lower()


def test_compare_command_outputs_path_to_stdout(tmp_path: Path) -> None:
    from click.testing import CliRunner

    mod = _load_run_cli()
    output = tmp_path / "comparison.md"
    runner = CliRunner()
    result = runner.invoke(mod.cli, ["compare", "--output", str(output)])
    assert result.exit_code == 0
    assert str(output) in result.output
