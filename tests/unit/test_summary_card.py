"""Tests for bernstein.cli.summary_card."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bernstein.cli.summary_card import (
    RunSummaryData,
    _fmt_duration,
    build_summary_card,
    print_summary_card,
    write_summary_json,
)
from rich.console import Console

if TYPE_CHECKING:
    from rich.table import Table

# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


def test_fmt_duration_seconds() -> None:
    assert _fmt_duration(45.7) == "45s"


def test_fmt_duration_minutes() -> None:
    assert _fmt_duration(125.0) == "2m 5s"


def test_fmt_duration_hours() -> None:
    assert _fmt_duration(3725.0) == "1h 2m 5s"


# ---------------------------------------------------------------------------
# RunSummaryData
# ---------------------------------------------------------------------------


def test_estimated_time_saved_is_double() -> None:
    data = RunSummaryData(
        run_id="x",
        tasks_completed=3,
        tasks_total=3,
        tasks_failed=0,
        wall_clock_seconds=300.0,
        total_cost_usd=0.0,
        quality_score=None,
    )
    assert data.estimated_time_saved_seconds == pytest.approx(600.0)


def test_to_dict_includes_estimated_time_saved() -> None:
    data = RunSummaryData(
        run_id="x",
        tasks_completed=2,
        tasks_total=4,
        tasks_failed=2,
        wall_clock_seconds=60.0,
        total_cost_usd=0.05,
        quality_score=0.5,
    )
    d = data.to_dict()
    assert d["estimated_time_saved_seconds"] == pytest.approx(120.0)
    assert d["tasks_completed"] == 2
    assert d["tasks_failed"] == 2
    assert d["total_cost_usd"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# build_summary_card
# ---------------------------------------------------------------------------


def _render(table: Table, width: int = 80) -> str:
    buf = io.StringIO()
    Console(file=buf, width=width, highlight=False).print(table)
    return buf.getvalue()


def test_card_contains_task_counts() -> None:
    data = RunSummaryData(
        run_id="r1",
        tasks_completed=4,
        tasks_total=5,
        tasks_failed=1,
        wall_clock_seconds=120.0,
        total_cost_usd=0.02,
        quality_score=0.9,
    )
    table = build_summary_card(data)
    rendered = _render(table)
    assert "4/5" in rendered
    assert "2m 0s" in rendered  # total time


def test_card_no_failed_row_when_zero_failures() -> None:
    data = RunSummaryData(
        run_id="r2",
        tasks_completed=3,
        tasks_total=3,
        tasks_failed=0,
        wall_clock_seconds=60.0,
        total_cost_usd=0.0,
        quality_score=None,
    )
    table = build_summary_card(data)
    rendered = _render(table)
    assert "Tasks failed" not in rendered


def test_card_quality_score_present_when_provided() -> None:
    data = RunSummaryData(
        run_id="r3",
        tasks_completed=5,
        tasks_total=5,
        tasks_failed=0,
        wall_clock_seconds=30.0,
        total_cost_usd=0.0,
        quality_score=0.75,
    )
    table = build_summary_card(data)
    rendered = _render(table)
    assert "75%" in rendered


def test_card_quality_score_absent_when_none() -> None:
    data = RunSummaryData(
        run_id="r4",
        tasks_completed=1,
        tasks_total=1,
        tasks_failed=0,
        wall_clock_seconds=10.0,
        total_cost_usd=0.0,
        quality_score=None,
    )
    table = build_summary_card(data)
    rendered = _render(table)
    assert "Quality score" not in rendered


def test_card_renders_at_80_columns() -> None:
    data = RunSummaryData(
        run_id="r5",
        tasks_completed=10,
        tasks_total=10,
        tasks_failed=0,
        wall_clock_seconds=600.0,
        total_cost_usd=1.23,
        quality_score=1.0,
    )
    table = build_summary_card(data)
    rendered = _render(table, width=80)
    # Should not raise and should contain expected content
    assert "10/10" in rendered


# ---------------------------------------------------------------------------
# print_summary_card — smoke test (no assertion on output, just no error)
# ---------------------------------------------------------------------------


def test_print_summary_card_no_error() -> None:
    data = RunSummaryData(
        run_id="r6",
        tasks_completed=2,
        tasks_total=2,
        tasks_failed=0,
        wall_clock_seconds=45.0,
        total_cost_usd=0.005,
        quality_score=None,
    )
    buf = io.StringIO()
    con = Console(file=buf, width=80)
    print_summary_card(data, console=con)
    assert len(buf.getvalue()) > 0


# ---------------------------------------------------------------------------
# write_summary_json
# ---------------------------------------------------------------------------


def test_write_summary_json_creates_file() -> None:
    data = RunSummaryData(
        run_id="20260330-120000",
        tasks_completed=3,
        tasks_total=4,
        tasks_failed=1,
        wall_clock_seconds=180.0,
        total_cost_usd=0.012,
        quality_score=0.8,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_summary_json(data, "20260330-120000", Path(tmpdir))
        assert path.exists()
        assert path.name == "summary.json"
        assert path.parent.name == "20260330-120000"

        result = json.loads(path.read_text())
        assert result["run_id"] == "20260330-120000"
        assert result["tasks_completed"] == 3
        assert result["tasks_failed"] == 1
        assert result["estimated_time_saved_seconds"] == pytest.approx(360.0)
        assert result["quality_score"] == pytest.approx(0.8)


def test_write_summary_json_creates_parent_dirs() -> None:
    data = RunSummaryData(
        run_id="run-abc",
        tasks_completed=0,
        tasks_total=0,
        tasks_failed=0,
        wall_clock_seconds=0.0,
        total_cost_usd=0.0,
        quality_score=None,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        # Pass a nested sdd_dir that doesn't yet exist
        sdd_dir = Path(tmpdir) / "nested" / ".sdd"
        path = write_summary_json(data, "run-abc", sdd_dir)
        assert path.exists()
