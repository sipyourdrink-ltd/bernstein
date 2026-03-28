"""Unit tests for bernstein.evolution.report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.evolution.report import CycleRecord, EvolutionReport, ExperimentRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_cycles(metrics_dir: Path, records: list[dict]) -> None:
    path = metrics_dir / "evolve_cycles.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _write_experiments(evolution_dir: Path, records: list[dict]) -> None:
    path = evolution_dir / "experiments.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


SAMPLE_CYCLES = [
    {
        "cycle": 1,
        "timestamp": 1774651975.0,
        "iso_time": "2026-03-27T22:52:55+00:00",
        "tick": 70,
        "focus_area": "new_features",
        "tasks_completed": 6,
        "tasks_failed": 1,
        "tests_passed": 1137,
        "tests_failed": 4,
        "commits_made": 0,
        "backoff_factor": 1,
        "consecutive_empty": 0,
        "duration_s": 118.36,
    },
    {
        "cycle": 2,
        "timestamp": 1774653033.0,
        "iso_time": "2026-03-27T23:10:33+00:00",
        "tick": 165,
        "focus_area": "test_coverage",
        "tasks_completed": 11,
        "tasks_failed": 1,
        "tests_passed": 1152,
        "tests_failed": 0,
        "commits_made": 1,
        "backoff_factor": 1,
        "consecutive_empty": 0,
        "duration_s": 106.73,
    },
    {
        "cycle": 3,
        "timestamp": 1774653671.0,
        "iso_time": "2026-03-27T23:21:11+00:00",
        "tick": 53,
        "focus_area": "code_quality",
        "tasks_completed": 16,
        "tasks_failed": 1,
        "tests_passed": 1214,
        "tests_failed": 0,
        "commits_made": 1,
        "backoff_factor": 1,
        "consecutive_empty": 0,
        "duration_s": 106.45,
    },
]

SAMPLE_EXPERIMENTS = [
    {
        "proposal_id": "prop-abc123",
        "title": "Reduce batch size",
        "risk_level": "config",
        "accepted": True,
        "delta": 0.12,
        "cost_usd": 0.05,
        "reason": "metrics improved",
        "timestamp": 1774651990.0,
    },
    {
        "proposal_id": "prop-def456",
        "title": "Update system prompt",
        "risk_level": "template",
        "accepted": False,
        "delta": -0.03,
        "cost_usd": 0.07,
        "reason": "delta negative",
        "timestamp": 1774652100.0,
    },
]


# ---------------------------------------------------------------------------
# CycleRecord tests
# ---------------------------------------------------------------------------


class TestCycleRecord:
    def test_from_dict_basic(self) -> None:
        rec = CycleRecord.from_dict(SAMPLE_CYCLES[0])
        assert rec.cycle == 1
        assert rec.focus_area == "new_features"
        assert rec.tasks_completed == 6
        assert rec.tasks_failed == 1
        assert rec.tests_passed == 1137
        assert rec.commits_made == 0

    def test_success_rate(self) -> None:
        rec = CycleRecord.from_dict(SAMPLE_CYCLES[0])
        # 6 completed, 1 failed → 6/7
        assert abs(rec.success_rate - 6 / 7) < 1e-6

    def test_success_rate_zero_tasks(self) -> None:
        rec = CycleRecord.from_dict(
            {
                **SAMPLE_CYCLES[0],
                "tasks_completed": 0,
                "tasks_failed": 0,
            }
        )
        assert rec.success_rate == 0.0

    def test_test_pass_rate(self) -> None:
        rec = CycleRecord.from_dict(SAMPLE_CYCLES[0])
        # 1137 passed, 4 failed
        assert abs(rec.test_pass_rate - 1137 / 1141) < 1e-6

    def test_test_pass_rate_zero(self) -> None:
        rec = CycleRecord.from_dict(
            {
                **SAMPLE_CYCLES[0],
                "tests_passed": 0,
                "tests_failed": 0,
            }
        )
        assert rec.test_pass_rate == 0.0


# ---------------------------------------------------------------------------
# ExperimentRecord tests
# ---------------------------------------------------------------------------


class TestExperimentRecord:
    def test_from_dict(self) -> None:
        rec = ExperimentRecord.from_dict(SAMPLE_EXPERIMENTS[0])
        assert rec.proposal_id == "prop-abc123"
        assert rec.accepted is True
        assert abs(rec.delta - 0.12) < 1e-9
        assert abs(rec.cost_usd - 0.05) < 1e-9


# ---------------------------------------------------------------------------
# EvolutionReport — load
# ---------------------------------------------------------------------------


class TestEvolutionReportLoad:
    def test_load_cycles(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        metrics_dir = state_dir / "metrics"
        metrics_dir.mkdir(parents=True)
        _write_cycles(metrics_dir, SAMPLE_CYCLES)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        assert len(report.cycles) == 3
        assert report.cycles[0].focus_area == "new_features"

    def test_load_experiments(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        evolution_dir = state_dir / "evolution"
        evolution_dir.mkdir()
        _write_experiments(evolution_dir, SAMPLE_EXPERIMENTS)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        assert len(report.experiments) == 2
        assert report.experiments[0].accepted is True
        assert report.experiments[1].accepted is False

    def test_load_missing_files(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        report = EvolutionReport(state_dir=state_dir)
        report.load()
        assert report.cycles == []
        assert report.experiments == []

    def test_load_skips_bad_lines(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        metrics_dir = state_dir / "metrics"
        metrics_dir.mkdir(parents=True)
        path = metrics_dir / "evolve_cycles.jsonl"
        path.write_text(json.dumps(SAMPLE_CYCLES[0]) + "\nNOT VALID JSON\n" + json.dumps(SAMPLE_CYCLES[1]) + "\n")

        report = EvolutionReport(state_dir=state_dir)
        report.load()
        assert len(report.cycles) == 2


# ---------------------------------------------------------------------------
# EvolutionReport — aggregated properties
# ---------------------------------------------------------------------------


class TestEvolutionReportStats:
    @pytest.fixture()
    def report(self, tmp_path: Path) -> EvolutionReport:
        state_dir = tmp_path / ".sdd"
        metrics_dir = state_dir / "metrics"
        metrics_dir.mkdir(parents=True)
        evolution_dir = state_dir / "evolution"
        evolution_dir.mkdir()
        _write_cycles(metrics_dir, SAMPLE_CYCLES)
        _write_experiments(evolution_dir, SAMPLE_EXPERIMENTS)

        r = EvolutionReport(state_dir=state_dir)
        r.load()
        return r

    def test_total_cycles(self, report: EvolutionReport) -> None:
        assert report.total_cycles == 3

    def test_total_tasks_completed(self, report: EvolutionReport) -> None:
        assert report.total_tasks_completed == 6 + 11 + 16

    def test_total_tasks_failed(self, report: EvolutionReport) -> None:
        assert report.total_tasks_failed == 3  # 1+1+1

    def test_total_commits(self, report: EvolutionReport) -> None:
        assert report.total_commits == 2  # 0+1+1

    def test_first_last_tests(self, report: EvolutionReport) -> None:
        assert report.first_tests_passed == 1137
        assert report.last_tests_passed == 1214

    def test_test_delta(self, report: EvolutionReport) -> None:
        assert report.test_delta == 1214 - 1137

    def test_experiments_accepted(self, report: EvolutionReport) -> None:
        assert report.experiments_accepted == 1

    def test_experiments_rejected(self, report: EvolutionReport) -> None:
        assert report.experiments_rejected == 1

    def test_total_experiment_cost(self, report: EvolutionReport) -> None:
        assert abs(report.total_experiment_cost_usd - 0.12) < 1e-9


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------


class TestSparkline:
    def test_empty(self) -> None:
        assert EvolutionReport._sparkline([]) == ""

    def test_all_same(self) -> None:
        s = EvolutionReport._sparkline([5.0, 5.0, 5.0])
        # All same → span=0 → all index 0 → " "
        assert all(c == " " for c in s)
        assert len(s) == 3

    def test_ascending(self) -> None:
        s = EvolutionReport._sparkline([0.0, 0.5, 1.0])
        assert len(s) == 3
        # Ascending: first char should be less than last
        assert s[0] <= s[2]

    def test_length_matches_input(self) -> None:
        s = EvolutionReport._sparkline(list(range(10)))
        assert len(s) == 10


# ---------------------------------------------------------------------------
# Export — Markdown
# ---------------------------------------------------------------------------


class TestExportMarkdown:
    def test_creates_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.md"
        report.export_markdown(out)
        assert out.exists()

    def test_contains_key_sections(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        evolution_dir = state_dir / "evolution"
        evolution_dir.mkdir()
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)
        _write_experiments(evolution_dir, SAMPLE_EXPERIMENTS)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.md"
        report.export_markdown(out)
        content = out.read_text()

        assert "# Bernstein Evolution Report" in content
        assert "## Summary" in content
        assert "## Cycle Breakdown" in content
        assert "## Experiments" in content
        assert "new_features" in content

    def test_no_experiments_section_when_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.md"
        report.export_markdown(out)
        content = out.read_text()
        assert "## Experiments" not in content


# ---------------------------------------------------------------------------
# Export — HTML
# ---------------------------------------------------------------------------


class TestExportHTML:
    def test_creates_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.html"
        report.export_html(out)
        assert out.exists()

    def test_contains_key_content(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.html"
        report.export_html(out)
        content = out.read_text()

        assert "<!DOCTYPE html>" in content
        assert "Bernstein Evolution Report" in content
        assert "new_features" in content
        assert "test_coverage" in content

    def test_experiments_section_included(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        (state_dir / "metrics").mkdir(parents=True)
        evolution_dir = state_dir / "evolution"
        evolution_dir.mkdir()
        _write_cycles(state_dir / "metrics", SAMPLE_CYCLES)
        _write_experiments(evolution_dir, SAMPLE_EXPERIMENTS)

        report = EvolutionReport(state_dir=state_dir)
        report.load()

        out = tmp_path / "report.html"
        report.export_html(out)
        content = out.read_text()

        assert "Experiments" in content
        assert "prop-abc123" in content
