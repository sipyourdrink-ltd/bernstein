"""Tests for complexity-performance correlation analysis."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bernstein.core.quality.complexity_correlation import (
    AgentOutcome,
    AnalysisReport,
    CorrelationResult,
    FileMetrics,
    _count_cyclomatic,
    _count_fan_out,
    _p_value_approx,
    _pearson_r,
    build_analysis_report,
    compute_file_metrics,
    correlate_complexity_with_outcomes,
    identify_high_risk_files,
    render_correlation_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, name: str, code: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(code), encoding="utf-8")
    return p


def _metric(
    path: str = "a.py",
    cc: int = 5,
    fi: int = 0,
    fo: int = 2,
    churn: int = 3,
    lc: int = 100,
) -> FileMetrics:
    return FileMetrics(
        file_path=path,
        cyclomatic_complexity=cc,
        fan_in=fi,
        fan_out=fo,
        churn_count=churn,
        line_count=lc,
    )


def _outcome(
    task: str = "t1",
    path: str = "a.py",
    success: bool = True,
    dur: float = 60.0,
    cost: float = 0.10,
    retries: int = 0,
) -> AgentOutcome:
    return AgentOutcome(
        task_id=task,
        file_path=path,
        success=success,
        duration_s=dur,
        cost_usd=cost,
        retries=retries,
    )


# ---------------------------------------------------------------------------
# FileMetrics dataclass
# ---------------------------------------------------------------------------


class TestFileMetrics:
    def test_frozen(self) -> None:
        m = _metric()
        with pytest.raises(AttributeError):
            m.cyclomatic_complexity = 99  # type: ignore[misc]

    def test_fields(self) -> None:
        m = _metric(path="x.py", cc=10, fi=2, fo=3, churn=5, lc=200)
        assert m.file_path == "x.py"
        assert m.cyclomatic_complexity == 10
        assert m.fan_in == 2
        assert m.fan_out == 3
        assert m.churn_count == 5
        assert m.line_count == 200


# ---------------------------------------------------------------------------
# AgentOutcome dataclass
# ---------------------------------------------------------------------------


class TestAgentOutcome:
    def test_frozen(self) -> None:
        o = _outcome()
        with pytest.raises(AttributeError):
            o.success = False  # type: ignore[misc]

    def test_fields(self) -> None:
        o = _outcome(task="t-99", path="b.py", success=False, dur=120.0, cost=0.50, retries=3)
        assert o.task_id == "t-99"
        assert o.file_path == "b.py"
        assert o.success is False
        assert o.duration_s == 120.0
        assert o.cost_usd == 0.50
        assert o.retries == 3


# ---------------------------------------------------------------------------
# CorrelationResult / AnalysisReport dataclass
# ---------------------------------------------------------------------------


class TestCorrelationResult:
    def test_frozen(self) -> None:
        cr = CorrelationResult(
            metric_name="x", correlation_coefficient=0.5, p_value=0.01, sample_size=30, insight="ok"
        )
        with pytest.raises(AttributeError):
            cr.p_value = 0.99  # type: ignore[misc]


class TestAnalysisReport:
    def test_frozen(self) -> None:
        r = AnalysisReport(correlations=(), recommendations=(), high_risk_files=())
        with pytest.raises(AttributeError):
            r.correlations = ()  # type: ignore[misc]

    def test_tuple_fields(self) -> None:
        r = AnalysisReport(
            correlations=(
                CorrelationResult("a", 0.1, 0.5, 10, "weak"),
            ),
            recommendations=("do X",),
            high_risk_files=("f.py",),
        )
        assert len(r.correlations) == 1
        assert r.recommendations == ("do X",)
        assert r.high_risk_files == ("f.py",)


# ---------------------------------------------------------------------------
# compute_file_metrics
# ---------------------------------------------------------------------------


class TestComputeFileMetrics:
    def test_simple_file(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "simple.py", """\
            x = 1
            y = 2
        """)
        m = compute_file_metrics(p)
        assert m.cyclomatic_complexity == 0
        assert m.fan_out == 0
        assert m.line_count >= 2

    def test_branching(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "branch.py", """\
            def f(x):
                if x > 0:
                    for i in range(x):
                        while True:
                            break
        """)
        m = compute_file_metrics(p)
        assert m.cyclomatic_complexity == 3  # if + for + while

    def test_bool_ops(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "boolops.py", """\
            if a and b and c:
                pass
        """)
        m = compute_file_metrics(p)
        # if=1, BoolOp with 3 values=2 extra branches
        assert m.cyclomatic_complexity == 3

    def test_fan_out_imports(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "imports.py", """\
            import os
            import sys
            from pathlib import Path
            from collections import defaultdict
        """)
        m = compute_file_metrics(p)
        assert m.fan_out == 4  # os, sys, pathlib, collections

    def test_churn_and_fan_in_passthrough(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "passthrough.py", "x = 1\n")
        m = compute_file_metrics(p, churn_count=42, fan_in=7)
        assert m.churn_count == 42
        assert m.fan_in == 7

    def test_syntax_error_returns_zero_complexity(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.py"
        p.write_text("def (\n", encoding="utf-8")
        m = compute_file_metrics(p)
        assert m.cyclomatic_complexity == 0
        assert m.fan_out == 0
        assert m.line_count >= 1

    def test_file_not_found_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            compute_file_metrics("/nonexistent/file.py")

    def test_async_nodes(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "async_nodes.py", """\
            import asyncio
            async def f():
                async for x in aiter():
                    async with ctx() as c:
                        pass
        """)
        m = compute_file_metrics(p)
        # async for + async with = 2
        assert m.cyclomatic_complexity == 2

    def test_except_handler(self, tmp_path: Path) -> None:
        p = _write_py(tmp_path, "exc.py", """\
            try:
                pass
            except ValueError:
                pass
            except TypeError:
                pass
        """)
        m = compute_file_metrics(p)
        assert m.cyclomatic_complexity == 2  # two except handlers


# ---------------------------------------------------------------------------
# Internal helpers: _count_cyclomatic, _count_fan_out
# ---------------------------------------------------------------------------


class TestCountCyclomatic:
    def test_empty_module(self) -> None:
        import ast

        tree = ast.parse("")
        assert _count_cyclomatic(tree) == 0

    def test_with_assert(self) -> None:
        import ast

        tree = ast.parse("assert True\nassert False")
        assert _count_cyclomatic(tree) == 2


class TestCountFanOut:
    def test_no_imports(self) -> None:
        import ast

        tree = ast.parse("x = 1")
        assert _count_fan_out(tree) == 0

    def test_dedup_same_top_level(self) -> None:
        import ast

        tree = ast.parse("import os\nimport os.path")
        assert _count_fan_out(tree) == 1  # both are "os"


# ---------------------------------------------------------------------------
# _pearson_r / _p_value_approx
# ---------------------------------------------------------------------------


class TestPearsonR:
    def test_perfect_positive(self) -> None:
        r = _pearson_r([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
        assert abs(r - 1.0) < 1e-9

    def test_perfect_negative(self) -> None:
        r = _pearson_r([1.0, 2.0, 3.0], [30.0, 20.0, 10.0])
        assert abs(r - (-1.0)) < 1e-9

    def test_no_correlation(self) -> None:
        r = _pearson_r([1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 2.0, 4.0])
        assert abs(r) < 0.9  # not perfectly correlated

    def test_single_element(self) -> None:
        assert _pearson_r([1.0], [2.0]) == 0.0

    def test_constant_input(self) -> None:
        r = _pearson_r([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])
        assert r == 0.0  # constant → undefined → 0


class TestPValueApprox:
    def test_small_n_returns_one(self) -> None:
        assert _p_value_approx(0.5, 2) == 1.0

    def test_perfect_correlation(self) -> None:
        p = _p_value_approx(1.0, 10)
        assert p == 0.0

    def test_zero_correlation(self) -> None:
        p = _p_value_approx(0.0, 10)
        assert p == 1.0

    def test_moderate_correlation(self) -> None:
        p = _p_value_approx(0.5, 30)
        assert 0.0 < p < 1.0


# ---------------------------------------------------------------------------
# correlate_complexity_with_outcomes
# ---------------------------------------------------------------------------


class TestCorrelateComplexityWithOutcomes:
    def test_no_paired_data(self) -> None:
        metrics = [_metric(path="a.py")]
        outcomes = [_outcome(path="z.py")]
        results = correlate_complexity_with_outcomes(metrics, outcomes)
        assert len(results) == 5  # one per metric field
        assert all(r.sample_size == 0 for r in results)

    def test_paired_data(self) -> None:
        metrics = [_metric(path="a.py", cc=10), _metric(path="b.py", cc=2)]
        outcomes = [
            _outcome(path="a.py", success=False),
            _outcome(path="b.py", success=True),
        ]
        results = correlate_complexity_with_outcomes(metrics, outcomes)
        # cyclomatic_complexity should correlate positively with failure
        cc_result = next(r for r in results if r.metric_name == "cyclomatic_complexity")
        assert cc_result.sample_size == 2
        assert cc_result.correlation_coefficient > 0  # higher cc → more failure

    def test_empty_metrics(self) -> None:
        results = correlate_complexity_with_outcomes([], [])
        assert len(results) == 5
        assert all(r.sample_size == 0 for r in results)

    def test_all_success_constant_outcome(self) -> None:
        metrics = [_metric(path="a.py", cc=10), _metric(path="b.py", cc=2)]
        outcomes = [
            _outcome(path="a.py", success=True),
            _outcome(path="b.py", success=True),
        ]
        results = correlate_complexity_with_outcomes(metrics, outcomes)
        cc_result = next(r for r in results if r.metric_name == "cyclomatic_complexity")
        # All success → ys are constant (0.0) → r = 0.0
        assert cc_result.correlation_coefficient == 0.0


# ---------------------------------------------------------------------------
# identify_high_risk_files
# ---------------------------------------------------------------------------


class TestIdentifyHighRiskFiles:
    def test_above_threshold(self) -> None:
        metrics = [
            _metric(path="big.py", cc=20),
            _metric(path="small.py", cc=3),
        ]
        result = identify_high_risk_files(metrics, threshold=10)
        assert result == ["big.py"]

    def test_all_below(self) -> None:
        metrics = [_metric(path="a.py", cc=5), _metric(path="b.py", cc=3)]
        assert identify_high_risk_files(metrics, threshold=10) == []

    def test_sorted_output(self) -> None:
        metrics = [
            _metric(path="z.py", cc=20),
            _metric(path="a.py", cc=20),
            _metric(path="m.py", cc=20),
        ]
        result = identify_high_risk_files(metrics, threshold=5)
        assert result == ["a.py", "m.py", "z.py"]

    def test_default_threshold(self) -> None:
        metrics = [_metric(path="a.py", cc=16)]
        result = identify_high_risk_files(metrics)
        assert result == ["a.py"]

    def test_empty(self) -> None:
        assert identify_high_risk_files([]) == []


# ---------------------------------------------------------------------------
# build_analysis_report
# ---------------------------------------------------------------------------


class TestBuildAnalysisReport:
    def test_significant_positive(self) -> None:
        correlations = [
            CorrelationResult("cyclomatic_complexity", 0.7, 0.001, 50, "strong positive"),
        ]
        report = build_analysis_report(correlations, ["big.py"])
        assert any("Reduce" in r for r in report.recommendations)
        assert "big.py" in report.high_risk_files

    def test_significant_negative(self) -> None:
        correlations = [
            CorrelationResult("line_count", -0.4, 0.02, 40, "moderate negative"),
        ]
        report = build_analysis_report(correlations, [])
        assert any("better outcomes" in r for r in report.recommendations)

    def test_no_significant(self) -> None:
        correlations = [
            CorrelationResult("fan_out", 0.1, 0.6, 10, "negligible"),
        ]
        report = build_analysis_report(correlations, [])
        assert any("No statistically significant" in r for r in report.recommendations)

    def test_report_fields_are_tuples(self) -> None:
        report = build_analysis_report([], [])
        assert isinstance(report.correlations, tuple)
        assert isinstance(report.recommendations, tuple)
        assert isinstance(report.high_risk_files, tuple)


# ---------------------------------------------------------------------------
# render_correlation_report
# ---------------------------------------------------------------------------


class TestRenderCorrelationReport:
    def test_contains_sections(self) -> None:
        report = AnalysisReport(
            correlations=(
                CorrelationResult("cyclomatic_complexity", 0.5, 0.01, 30, "strong"),
            ),
            recommendations=("Reduce complexity",),
            high_risk_files=("big.py",),
        )
        md = render_correlation_report(report)
        assert "# Complexity-Performance Correlation Report" in md
        assert "## Correlations" in md
        assert "## Recommendations" in md
        assert "## High-Risk Files" in md
        assert "`big.py`" in md

    def test_no_high_risk_section_when_empty(self) -> None:
        report = AnalysisReport(
            correlations=(),
            recommendations=("ok",),
            high_risk_files=(),
        )
        md = render_correlation_report(report)
        assert "## High-Risk Files" not in md

    def test_table_header(self) -> None:
        report = AnalysisReport(
            correlations=(
                CorrelationResult("fan_in", 0.2, 0.3, 10, "weak"),
            ),
            recommendations=(),
            high_risk_files=(),
        )
        md = render_correlation_report(report)
        assert "| Metric | r | p-value | N | Insight |" in md
        assert "fan_in" in md
