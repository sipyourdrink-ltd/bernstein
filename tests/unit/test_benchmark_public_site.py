"""Tests for public benchmark policy and docs generation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from benchmarks.swe_bench.metrics import ScenarioSummary
from benchmarks.swe_bench.public_site import build_public_context, load_summaries, render_public_html
from benchmarks.swe_bench.report import generate_from_results_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "benchmarks"


def _load_fixture(name: str) -> ScenarioSummary:
    data = json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return ScenarioSummary.from_dict(data)


def _write_summaries(results_dir: Path, summaries: list[ScenarioSummary]) -> None:
    for summary in summaries:
        path = results_dir / f"{summary.scenario_name}_summary.json"
        path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")


def _mock_public_summaries() -> list[ScenarioSummary]:
    base = _load_fixture("mock_summary.json")
    overrides = {
        "solo-sonnet": ("sonnet", 0.24, 12, 0.14, 7.0),
        "solo-opus": ("opus", 0.38, 19, 1.20, 60.0),
        "bernstein-sonnet": ("sonnet", 0.39, 20, 0.42, 21.0),
        "bernstein-mixed": ("haiku, sonnet", 0.37, 18, 0.16, 8.0),
    }
    return [
        replace(
            base,
            scenario_name=name,
            resolved=resolved,
            failed=base.sample_size - resolved,
            resolve_rate=resolve_rate,
            mean_cost_per_instance_usd=mean_cost,
            total_cost_usd=total_cost,
            scenarios=[name],
            model_family=model_family,
        )
        for name, (model_family, resolve_rate, resolved, mean_cost, total_cost) in overrides.items()
    ]


def _verified_public_summaries() -> list[ScenarioSummary]:
    base = _load_fixture("verified_summary.json")
    overrides = {
        "solo-sonnet": ("sonnet", 0.26, 13, 0.14, 7.0),
        "solo-opus": ("opus", 0.40, 20, 1.20, 60.0),
        "bernstein-sonnet": ("sonnet", 0.44, 22, 0.44, 22.0),
        "bernstein-mixed": ("haiku, sonnet", 0.42, 21, 0.18, 9.0),
    }
    return [
        replace(
            base,
            scenario_name=name,
            resolved=resolved,
            failed=base.sample_size - resolved,
            resolve_rate=resolve_rate,
            mean_cost_per_instance_usd=mean_cost,
            total_cost_usd=total_cost,
            scenarios=[name],
            model_family=model_family,
        )
        for name, (model_family, resolve_rate, resolved, mean_cost, total_cost) in overrides.items()
    ]


def test_legacy_summary_defaults_to_unverified_preview() -> None:
    summary = ScenarioSummary.from_dict(
        {
            "scenario_name": "solo-sonnet",
            "total_instances": 10,
            "resolved": 2,
            "failed": 8,
            "errors": 0,
            "skipped": 0,
            "resolve_rate": 0.2,
            "mean_wall_time_s": 10.0,
            "median_wall_time_s": 10.0,
            "total_cost_usd": 1.0,
            "mean_cost_per_instance_usd": 0.1,
            "mean_tokens_per_instance": 1000.0,
        }
    )

    assert summary.verified is False
    assert summary.source_type == "mock"
    assert summary.is_verified_public_result is False
    assert "Legacy summary" in summary.notes


def test_mock_results_render_methodology_without_public_claims(tmp_path: Path) -> None:
    _write_summaries(tmp_path, _mock_public_summaries())

    report_path = generate_from_results_dir(tmp_path)
    content = report_path.read_text(encoding="utf-8")

    assert "Verified public benchmark results: in progress" in content
    assert "Publication Blockers" in content
    assert "Rank 1" not in content
    assert "Highest in class" not in content
    assert "beating CrewAI" not in content


def test_verified_results_render_pilot_report(tmp_path: Path) -> None:
    _write_summaries(tmp_path, _verified_public_summaries())

    report_path = generate_from_results_dir(tmp_path)
    content = report_path.read_text(encoding="utf-8")

    assert "Verified Pilot Results (n=50)" in content
    assert "abc123def456" in content
    assert "2026-04-01T10:00:00Z" in content
    assert "Bernstein 3x Sonnet" in content
    assert "Solo Opus" in content


def test_mock_html_suppresses_banned_claims(tmp_path: Path) -> None:
    _write_summaries(tmp_path, _mock_public_summaries())

    summaries = load_summaries(tmp_path)
    context = build_public_context(summaries)
    html = render_public_html(context)

    assert context.ready is False
    assert "Benchmark Status &amp; Methodology" in html
    assert "Verified public benchmark results: in progress" in html
    assert "Rank 1" not in html
    assert "Highest in class" not in html
    assert "beating CrewAI" not in html
    assert "39.0%" not in html


def test_public_docs_guard_banned_claims_absent() -> None:
    banned = [
        "Rank 1",
        "Highest in class",
        "beating CrewAI",
        "beats CrewAI",
        "beats LangGraph",
        "Bernstein results are simulated",
    ]
    public_docs = [
        _REPO_ROOT / "docs" / "leaderboard.html",
        _REPO_ROOT / "docs" / "blog" / "multi-agent-benchmark.md",
        _REPO_ROOT / "docs" / "blog" / "swe-bench-orchestration-thesis.md",
        _REPO_ROOT / "benchmarks" / "README.md",
        _REPO_ROOT / "benchmarks" / "crewai-langgraph-comparison.md",
        _REPO_ROOT / "benchmarks" / "agent-hq-comparison.md",
        _REPO_ROOT / "docs" / "compare" / "bernstein-vs-github-agent-hq.md",
    ]

    for path in public_docs:
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{phrase!r} leaked into {path}"

    leaderboard = (_REPO_ROOT / "docs" / "leaderboard.html").read_text(encoding="utf-8")
    assert "Verified public benchmark results: in progress" in leaderboard
