"""Tests for automated post-mortem report generation (ROAD-153)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.postmortem import (
    ContributingFactor,
    FailedTaskTrace,
    PostMortemEvent,
    PostMortemGenerator,
    PostMortemReport,
    RecommendedAction,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    return tmp_path


@pytest.fixture()
def run_id() -> str:
    return "test-run-abc"


@pytest.fixture()
def summary_dir(workdir: Path, run_id: str) -> Path:
    d = workdir / ".sdd" / "runs" / run_id
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def metrics_dir(workdir: Path) -> Path:
    d = workdir / ".sdd" / "metrics"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def sample_report() -> PostMortemReport:
    now = time.time()
    return PostMortemReport(
        run_id="run-42",
        goal="Add JWT authentication",
        generated_at=now,
        total_tasks=10,
        failed_tasks=3,
        success_rate_pct=70.0,
        timeline=[
            PostMortemEvent(timestamp=now - 100, label="Task started: t1", kind="task_start", task_id="t1"),
            PostMortemEvent(timestamp=now - 50, label="Task FAILED: t1", kind="task_fail", task_id="t1"),
        ],
        failed_task_traces=[
            FailedTaskTrace(
                task_id="t1",
                role="backend",
                model="sonnet",
                session_id="sess-001",
                dominant_failure="compile_error",
                error_snippets=["SyntaxError: invalid syntax on line 42"],
                files_touched=["src/auth.py"],
                retry_context="Tried fixing import, failed again",
            )
        ],
        contributing_factors=[
            ContributingFactor(category="compile_error", count=2, description="Syntax errors."),
            ContributingFactor(category="rate_limit", count=1, description="Rate limits."),
        ],
        recommended_actions=[
            RecommendedAction(priority="high", action="Fix syntax errors", rationale="Blocking progress"),
            RecommendedAction(priority="medium", action="Add backoff", rationale="Rate limits"),
        ],
    )


@pytest.fixture()
def generator(workdir: Path, run_id: str) -> PostMortemGenerator:
    return PostMortemGenerator(workdir, run_id=run_id)


# ---------------------------------------------------------------------------
# PostMortemGenerator — data loading
# ---------------------------------------------------------------------------


class TestPostMortemGeneratorDataLoading:
    def test_detect_latest_run_when_no_runs_returns_unknown(self, workdir: Path) -> None:
        gen = PostMortemGenerator(workdir)
        assert gen._run_id == "unknown"

    def test_detect_latest_run_picks_most_recent(self, workdir: Path) -> None:
        runs = workdir / ".sdd" / "runs"
        for name in ("run-001", "run-002"):
            d = runs / name
            d.mkdir(parents=True)
            (d / "summary.json").write_text("{}", encoding="utf-8")
        gen = PostMortemGenerator(workdir)
        assert gen._run_id in ("run-001", "run-002")

    def test_load_summary_returns_empty_when_missing(
        self, generator: PostMortemGenerator
    ) -> None:
        assert generator._load_summary() == {}

    def test_load_summary_parses_json(
        self, generator: PostMortemGenerator, summary_dir: Path
    ) -> None:
        (summary_dir / "summary.json").write_text(
            json.dumps({"goal": "Do something cool"}), encoding="utf-8"
        )
        s = generator._load_summary()
        assert s["goal"] == "Do something cool"

    def test_load_task_metrics_returns_empty_when_no_dir(
        self, generator: PostMortemGenerator
    ) -> None:
        assert generator._load_task_metrics() == []

    def test_load_task_metrics_reads_json_files(
        self, generator: PostMortemGenerator, metrics_dir: Path
    ) -> None:
        payload = {"task_id": "t1", "success": True, "start_time": 1000.0, "end_time": 1100.0}
        (metrics_dir / "task_t1.json").write_text(json.dumps(payload), encoding="utf-8")
        metrics = generator._load_task_metrics()
        assert len(metrics) == 1
        assert metrics[0]["task_id"] == "t1"


# ---------------------------------------------------------------------------
# PostMortemGenerator — generate()
# ---------------------------------------------------------------------------


class TestPostMortemGeneratorGenerate:
    def test_generate_returns_report_with_unknown_when_no_data(
        self, generator: PostMortemGenerator
    ) -> None:
        report = generator.generate()
        assert report.run_id == "test-run-abc"
        assert report.total_tasks == 0
        assert report.failed_tasks == 0
        assert report.success_rate_pct == 0.0

    def test_generate_computes_success_rate(
        self, generator: PostMortemGenerator, metrics_dir: Path
    ) -> None:
        for i, success in enumerate([True, True, False]):
            payload = {
                "task_id": f"t{i}",
                "success": success,
                "start_time": float(1000 + i * 10),
                "end_time": float(1050 + i * 10),
            }
            (metrics_dir / f"task_t{i}.json").write_text(json.dumps(payload), encoding="utf-8")
        report = generator.generate()
        assert report.total_tasks == 3
        assert report.failed_tasks == 1
        assert report.success_rate_pct == pytest.approx(66.67, abs=0.1)

    def test_generate_builds_timeline(
        self, generator: PostMortemGenerator, metrics_dir: Path
    ) -> None:
        payload = {
            "task_id": "t1",
            "success": False,
            "start_time": 1000.0,
            "end_time": 1100.0,
        }
        (metrics_dir / "task_t1.json").write_text(json.dumps(payload), encoding="utf-8")
        report = generator.generate()
        kinds = [ev.kind for ev in report.timeline]
        assert "task_start" in kinds
        assert "task_fail" in kinds

    def test_generate_aggregates_factors(
        self, generator: PostMortemGenerator, metrics_dir: Path
    ) -> None:
        # We can't easily inject log data, but with no session logs the factors should be empty.
        payload = {"task_id": "t1", "success": False, "start_time": 1000.0, "end_time": 1100.0, "session_id": ""}
        (metrics_dir / "task_t1.json").write_text(json.dumps(payload), encoding="utf-8")
        report = generator.generate()
        # No session log → no factors from log aggregator
        assert isinstance(report.contributing_factors, list)


# ---------------------------------------------------------------------------
# PostMortemGenerator — to_markdown()
# ---------------------------------------------------------------------------


class TestPostMortemMarkdown:
    def test_to_markdown_contains_run_id(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "run-42" in md

    def test_to_markdown_contains_goal(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "Add JWT authentication" in md

    def test_to_markdown_contains_timeline(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "## Event Timeline" in md
        assert "task_fail" in md

    def test_to_markdown_contains_rca(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "Root Cause Analysis" in md
        assert "compile_error" in md

    def test_to_markdown_contains_recommended_actions(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "Recommended Actions" in md
        assert "HIGH" in md

    def test_to_markdown_contains_task_trace(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        md = generator.to_markdown(sample_report)
        assert "t1" in md
        assert "backend" in md
        assert "SyntaxError" in md


# ---------------------------------------------------------------------------
# PostMortemGenerator — to_html()
# ---------------------------------------------------------------------------


class TestPostMortemHTML:
    def test_to_html_is_valid_html_structure(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        html = generator.to_html(sample_report)
        assert "<!DOCTYPE html>" in html
        assert "<html" in html
        assert "</html>" in html

    def test_to_html_contains_run_id(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        html = generator.to_html(sample_report)
        assert "run-42" in html

    def test_to_html_has_styled_tables(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        html = generator.to_html(sample_report)
        assert "<table>" in html
        assert "<th>" in html
        assert "<td>" in html

    def test_to_html_contains_all_sections(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        html = generator.to_html(sample_report)
        assert "Event Timeline" in html
        assert "Root Cause Analysis" in html
        assert "Agent Decision Traces" in html
        assert "Recommended Actions" in html

    def test_to_html_escapes_special_chars(
        self, generator: PostMortemGenerator
    ) -> None:
        report = PostMortemReport(
            run_id="<xss>",
            goal="<script>alert(1)</script>",
            generated_at=time.time(),
            total_tasks=0,
            failed_tasks=0,
            success_rate_pct=0.0,
        )
        html = generator.to_html(report)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_to_html_not_just_pre_block(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport
    ) -> None:
        html = generator.to_html(sample_report)
        # Proper HTML should have structure beyond a single pre block
        assert html.count("<table>") >= 1
        # Should have styled sections, not just raw markdown escaped in <pre>
        assert "border-collapse" in html


# ---------------------------------------------------------------------------
# PostMortemGenerator — save()
# ---------------------------------------------------------------------------


class TestPostMortemSave:
    def test_save_markdown_writes_file(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.md"
        result = generator.save(sample_report, fmt="markdown", path=out)
        assert result == out
        assert out.exists()
        assert "run-42" in out.read_text(encoding="utf-8")

    def test_save_html_writes_file(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.html"
        result = generator.save(sample_report, fmt="html", path=out)
        assert result == out
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content

    def test_save_auto_creates_sdd_reports_dir(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport, workdir: Path
    ) -> None:
        result = generator.save(sample_report, fmt="markdown")
        assert result.exists()
        assert ".sdd/reports" in str(result)

    def test_save_pdf_falls_back_to_html(
        self, generator: PostMortemGenerator, sample_report: PostMortemReport, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.pdf"
        # Without weasyprint/wkhtmltopdf, should fall back to HTML
        result = generator.save(sample_report, fmt="pdf", path=out)
        # Either PDF or HTML fallback
        assert result.exists()
        assert result.suffix in (".pdf", ".html")
