"""Tests for public benchmark policy and docs generation."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

from benchmarks.swe_bench.metrics import ScenarioSummary
from benchmarks.swe_bench.public_site import build_public_context, load_summaries, render_public_html
from benchmarks.swe_bench.report import generate_from_results_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "benchmarks"
_HYGIENE_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_pr_text_hygiene.py"

# The real public-docs banned-claim phrases (which name specific third-party
# products) are never embedded in tracked source. They are loaded at runtime
# from an env var or a gitignored local fixture file -- the same pattern
# scripts/check_pr_text_hygiene.py already uses for the PR-text hygiene gate.
# When neither source is provisioned, the check falls back to these inert
# stand-ins so the read/loop/assert logic below still gets exercised.
_PUBLIC_DOCS_DENYLIST_ENV_VAR = "BENCHMARK_PUBLIC_DOCS_DENYLIST"
_PUBLIC_DOCS_DENYLIST_LOCAL_FILE = _FIXTURES_DIR / "local" / "public_docs_denylist.json"
_STRUCTURAL_PLACEHOLDER_DENYLIST = [
    "Structural-Placeholder-Public-Claim-Alpha",
    "Structural-Placeholder-Public-Claim-Beta",
]


def _load_hygiene_module() -> ModuleType:
    """Load scripts/check_pr_text_hygiene.py so its deny-list loaders can be reused."""
    spec = importlib.util.spec_from_file_location(
        "check_pr_text_hygiene_for_public_site_test",
        _HYGIENE_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    sys.modules.pop(spec.name, None)
    return module


def _resolve_public_docs_denylist() -> tuple[list[str], str]:
    """Resolve the banned-claim phrase list plus a description of its source.

    Source order: env var, then a gitignored local fixture file, then the
    neutral structural placeholders defined above. The public docs are
    always scanned -- only the phrase source changes.
    """
    hygiene = _load_hygiene_module()
    env_phrases = hygiene.load_denylist_from_env(_PUBLIC_DOCS_DENYLIST_ENV_VAR)
    if env_phrases:
        return env_phrases, f"env:{_PUBLIC_DOCS_DENYLIST_ENV_VAR}"
    if _PUBLIC_DOCS_DENYLIST_LOCAL_FILE.exists():
        return hygiene.load_denylist(_PUBLIC_DOCS_DENYLIST_LOCAL_FILE), str(_PUBLIC_DOCS_DENYLIST_LOCAL_FILE)
    return list(_STRUCTURAL_PLACEHOLDER_DENYLIST), "structural placeholders (no fixture provisioned)"


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
    assert "beats every other tool" not in content


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
    assert "beats every other tool" not in html
    assert "39.0%" not in html


def test_public_docs_guard_banned_claims_absent() -> None:
    banned, source = _resolve_public_docs_denylist()
    public_docs = [
        _REPO_ROOT / "docs" / "benchmarks" / "leaderboard.html",
        _REPO_ROOT / "docs" / "blog" / "multi-agent-benchmark.md",
        _REPO_ROOT / "benchmarks" / "README.md",
    ]

    for path in public_docs:
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{phrase!r} leaked into {path} (denylist source: {source})"

    leaderboard = (_REPO_ROOT / "docs" / "benchmarks" / "leaderboard.html").read_text(encoding="utf-8")
    # The leaderboard page documents methodology and reproducibility, not headline numbers
    # from checked-in mock artifacts. Verified eval artifacts are the only public source.
    assert "verified SWE-Bench eval artifacts" in leaderboard
