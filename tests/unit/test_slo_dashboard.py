# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Tests for SLO dashboard with burn-down rate visualization (#669).

Tests cover:
- SLODefinition / SLOStatus / SLODashboard frozen dataclass construction
- compute_burn_rate with various history inputs
- predict_breach edge cases
- compute_slo_status from archive data for all three metric types
- build_slo_dashboard aggregation and overall health
- get_default_slos returns correct defaults
- render_slo_markdown output structure
- Archive parsing edge cases (empty, malformed, missing)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.observability.slo_dashboard import (
    SLODashboard,
    SLODefinition,
    SLOStatus,
    build_slo_dashboard,
    compute_burn_rate,
    compute_slo_status,
    get_default_slos,
    predict_breach,
    render_slo_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = 1_700_000_000.0  # Fixed reference timestamp


def _write_archive(path: Path, records: list[dict[str, object]]) -> None:
    """Write a list of dicts as JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _make_record(
    task_id: str = "t1",
    status: str = "done",
    created_at: float = _BASE_TS - 3600,
    completed_at: float = _BASE_TS,
    duration_seconds: float = 60.0,
    quality_gate_passed: bool | None = None,
    cost_usd: float | None = 0.5,
) -> dict[str, object]:
    """Build a minimal archive record dict."""
    return {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "role": "backend",
        "status": status,
        "created_at": created_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "result_summary": None,
        "cost_usd": cost_usd,
        "assigned_agent": "agent-1",
        "owned_files": [],
        "quality_gate_passed": quality_gate_passed,
    }


def _task_completion_def(target: float = 95.0, window: int = 7) -> SLODefinition:
    return SLODefinition(name="Task Completion", target_pct=target, metric="task_completion", window_days=window)


def _quality_gate_def(target: float = 90.0, window: int = 7) -> SLODefinition:
    return SLODefinition(name="Quality Gate", target_pct=target, metric="quality_gate", window_days=window)


def _latency_def(target: float = 99.0, window: int = 7) -> SLODefinition:
    return SLODefinition(name="Latency p99", target_pct=target, metric="latency", window_days=window)


# ---------------------------------------------------------------------------
# Frozen dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_slo_definition_frozen(self) -> None:
        defn = _task_completion_def()
        with pytest.raises(AttributeError):
            defn.target_pct = 99.0  # type: ignore[misc]

    def test_slo_status_frozen(self) -> None:
        status = SLOStatus(
            definition=_task_completion_def(),
            current_pct=96.0,
            error_budget_remaining_pct=80.0,
            burn_rate_per_day=0.5,
            days_until_breach=None,
            status="healthy",
        )
        with pytest.raises(AttributeError):
            status.current_pct = 50.0  # type: ignore[misc]

    def test_slo_dashboard_frozen(self) -> None:
        dashboard = SLODashboard(slos=(), overall_health="healthy", generated_at=_BASE_TS)
        with pytest.raises(AttributeError):
            dashboard.overall_health = "critical"  # type: ignore[misc]

    def test_slo_definition_fields(self) -> None:
        defn = SLODefinition(name="test", target_pct=95.0, metric="task_completion", window_days=7)
        assert defn.name == "test"
        assert defn.target_pct == 95.0
        assert defn.metric == "task_completion"
        assert defn.window_days == 7

    def test_slo_status_fields(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=92.0,
            error_budget_remaining_pct=40.0,
            burn_rate_per_day=1.2,
            days_until_breach=33.33,
            status="warning",
        )
        assert status.definition is defn
        assert status.current_pct == 92.0
        assert status.error_budget_remaining_pct == 40.0
        assert status.burn_rate_per_day == pytest.approx(1.2)
        assert status.days_until_breach == pytest.approx(33.33)
        assert status.status == "warning"

    def test_slo_dashboard_slos_is_tuple(self) -> None:
        dashboard = SLODashboard(slos=(), overall_health="healthy", generated_at=_BASE_TS)
        assert isinstance(dashboard.slos, tuple)


# ---------------------------------------------------------------------------
# compute_burn_rate
# ---------------------------------------------------------------------------


class TestComputeBurnRate:
    def test_empty_history(self) -> None:
        assert compute_burn_rate([], 7) == 0.0

    def test_single_point(self) -> None:
        assert compute_burn_rate([(_BASE_TS, 95.0)], 7) == 0.0

    def test_stable_compliance(self) -> None:
        history = [
            (_BASE_TS - 86400, 95.0),
            (_BASE_TS, 95.0),
        ]
        assert compute_burn_rate(history, 7) == pytest.approx(0.0)

    def test_declining_compliance(self) -> None:
        # 95% -> 90% over 5 days = 1.0 pp/day burn rate
        history = [
            (_BASE_TS - 5 * 86400, 95.0),
            (_BASE_TS, 90.0),
        ]
        rate = compute_burn_rate(history, 7)
        assert rate == pytest.approx(1.0)

    def test_improving_compliance_returns_zero(self) -> None:
        # Compliance improving means burn rate is 0 (clamped)
        history = [
            (_BASE_TS - 3 * 86400, 90.0),
            (_BASE_TS, 95.0),
        ]
        assert compute_burn_rate(history, 7) == 0.0

    def test_multiple_points_uses_endpoints(self) -> None:
        history = [
            (_BASE_TS - 4 * 86400, 100.0),
            (_BASE_TS - 2 * 86400, 97.0),
            (_BASE_TS, 92.0),
        ]
        # 100 -> 92 over 4 days = 2.0 pp/day
        rate = compute_burn_rate(history, 7)
        assert rate == pytest.approx(2.0)

    def test_same_timestamp_returns_zero(self) -> None:
        history = [
            (_BASE_TS, 95.0),
            (_BASE_TS, 90.0),
        ]
        assert compute_burn_rate(history, 7) == 0.0


# ---------------------------------------------------------------------------
# predict_breach
# ---------------------------------------------------------------------------


class TestPredictBreach:
    def test_no_burn_returns_none(self) -> None:
        status = SLOStatus(
            definition=_task_completion_def(),
            current_pct=96.0,
            error_budget_remaining_pct=80.0,
            burn_rate_per_day=0.0,
            days_until_breach=None,
            status="healthy",
        )
        assert predict_breach(status) is None

    def test_positive_burn_computes_days(self) -> None:
        status = SLOStatus(
            definition=_task_completion_def(),
            current_pct=94.0,
            error_budget_remaining_pct=50.0,
            burn_rate_per_day=5.0,
            days_until_breach=None,
            status="warning",
        )
        assert predict_breach(status) == pytest.approx(10.0)

    def test_budget_exhausted_returns_zero(self) -> None:
        status = SLOStatus(
            definition=_task_completion_def(),
            current_pct=80.0,
            error_budget_remaining_pct=0.0,
            burn_rate_per_day=2.0,
            days_until_breach=None,
            status="critical",
        )
        assert predict_breach(status) == 0.0

    def test_small_budget_with_high_burn(self) -> None:
        status = SLOStatus(
            definition=_task_completion_def(),
            current_pct=94.5,
            error_budget_remaining_pct=10.0,
            burn_rate_per_day=10.0,
            days_until_breach=None,
            status="warning",
        )
        assert predict_breach(status) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_slo_status
# ---------------------------------------------------------------------------


class TestComputeSloStatus:
    def test_empty_archive_returns_healthy(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        defn = _task_completion_def()
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 100.0
        assert status.status == "healthy"
        assert status.error_budget_remaining_pct == 100.0

    def test_nonexistent_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "does_not_exist.jsonl"
        defn = _task_completion_def()
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 100.0
        assert status.status == "healthy"

    def test_all_tasks_done(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", completed_at=_BASE_TS - 100),
                _make_record("t2", "done", completed_at=_BASE_TS - 200),
                _make_record("t3", "done", completed_at=_BASE_TS - 300),
            ],
        )
        defn = _task_completion_def(target=95.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 100.0
        assert status.status == "healthy"
        assert status.error_budget_remaining_pct == 100.0

    def test_some_failures_within_budget(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [_make_record(f"t{i}", "done", completed_at=_BASE_TS - i * 100) for i in range(19)]
        records.append(_make_record("t19", "failed", completed_at=_BASE_TS - 1900))
        _write_archive(archive, records)

        defn = _task_completion_def(target=95.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # 19/20 = 95%, exactly meeting the target
        assert status.current_pct == 95.0
        assert status.status == "healthy"

    def test_failures_breach_slo(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        # 7 done, 3 failed => 70% completion, target 95%
        records = [_make_record(f"t{i}", "done", completed_at=_BASE_TS - i * 100) for i in range(7)]
        records.extend([_make_record(f"f{i}", "failed", completed_at=_BASE_TS - (7 + i) * 100) for i in range(3)])
        _write_archive(archive, records)

        defn = _task_completion_def(target=95.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 70.0
        assert status.status == "critical"
        assert status.error_budget_remaining_pct == 0.0

    def test_quality_gate_metric(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_record("t1", "done", quality_gate_passed=True, completed_at=_BASE_TS - 100),
            _make_record("t2", "done", quality_gate_passed=True, completed_at=_BASE_TS - 200),
            _make_record("t3", "done", quality_gate_passed=False, completed_at=_BASE_TS - 300),
            _make_record("t4", "failed", quality_gate_passed=False, completed_at=_BASE_TS - 400),
        ]
        _write_archive(archive, records)

        defn = _quality_gate_def(target=90.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # 2 passed out of 4 evaluated = 50%
        assert status.current_pct == 50.0
        assert status.status == "critical"

    def test_latency_metric(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        records = [
            _make_record("t1", "done", duration_seconds=100.0, completed_at=_BASE_TS - 100),
            _make_record("t2", "done", duration_seconds=200.0, completed_at=_BASE_TS - 200),
            _make_record("t3", "done", duration_seconds=250.0, completed_at=_BASE_TS - 300),
            _make_record("t4", "done", duration_seconds=400.0, completed_at=_BASE_TS - 400),
        ]
        _write_archive(archive, records)

        defn = _latency_def(target=99.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # 3 out of 4 within 300s = 75%
        assert status.current_pct == 75.0

    def test_records_outside_window_excluded(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        # One record within window, one outside
        _write_archive(
            archive,
            [
                _make_record("t1", "done", completed_at=_BASE_TS - 86400),  # 1 day ago
                _make_record("t2", "failed", completed_at=_BASE_TS - 30 * 86400),  # 30 days ago
            ],
        )
        defn = _task_completion_def(target=95.0, window=7)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # Only t1 is in window, so 100% completion
        assert status.current_pct == 100.0
        assert status.status == "healthy"

    def test_unknown_metric_defaults_to_100(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(archive, [_make_record("t1", "done", completed_at=_BASE_TS - 100)])
        defn = SLODefinition(name="Unknown", target_pct=90.0, metric="unknown_metric", window_days=7)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 100.0

    def test_definition_preserved_in_status(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(archive, [])
        defn = _task_completion_def()
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.definition is defn


# ---------------------------------------------------------------------------
# build_slo_dashboard
# ---------------------------------------------------------------------------


class TestBuildSloDashboard:
    def test_empty_definitions(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        dashboard = build_slo_dashboard((), archive, now=_BASE_TS)
        assert dashboard.slos == ()
        assert dashboard.overall_health == "healthy"
        assert dashboard.generated_at == _BASE_TS

    def test_all_healthy(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [_make_record(f"t{i}", "done", completed_at=_BASE_TS - i * 100) for i in range(20)],
        )
        defs = get_default_slos()
        dashboard = build_slo_dashboard(defs, archive, now=_BASE_TS)
        assert dashboard.overall_health == "healthy"
        assert len(dashboard.slos) == 3

    def test_one_critical_makes_overall_critical(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        # All tasks fail => task completion is critical
        _write_archive(
            archive,
            [_make_record(f"t{i}", "failed", completed_at=_BASE_TS - i * 100) for i in range(10)],
        )
        defs = (
            _task_completion_def(target=95.0),
            _quality_gate_def(target=90.0),
        )
        dashboard = build_slo_dashboard(defs, archive, now=_BASE_TS)
        assert dashboard.overall_health == "critical"

    def test_generated_at_matches_now(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        dashboard = build_slo_dashboard((), archive, now=12345.0)
        assert dashboard.generated_at == 12345.0


# ---------------------------------------------------------------------------
# get_default_slos
# ---------------------------------------------------------------------------


class TestGetDefaultSlos:
    def test_returns_three_definitions(self) -> None:
        defaults = get_default_slos()
        assert len(defaults) == 3

    def test_returns_tuple(self) -> None:
        defaults = get_default_slos()
        assert isinstance(defaults, tuple)

    def test_task_completion_slo(self) -> None:
        defaults = get_default_slos()
        tc = defaults[0]
        assert tc.name == "Task Completion"
        assert tc.target_pct == 95.0
        assert tc.metric == "task_completion"
        assert tc.window_days == 7

    def test_quality_gate_slo(self) -> None:
        defaults = get_default_slos()
        qg = defaults[1]
        assert qg.name == "Quality Gate Pass"
        assert qg.target_pct == 90.0
        assert qg.metric == "quality_gate"
        assert qg.window_days == 7

    def test_latency_slo(self) -> None:
        defaults = get_default_slos()
        lat = defaults[2]
        assert lat.name == "Latency p99 <300s"
        assert lat.target_pct == 99.0
        assert lat.metric == "latency"
        assert lat.window_days == 7

    def test_all_frozen(self) -> None:
        for defn in get_default_slos():
            with pytest.raises(AttributeError):
                defn.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# render_slo_markdown
# ---------------------------------------------------------------------------


class TestRenderSloMarkdown:
    def test_empty_dashboard(self) -> None:
        dashboard = SLODashboard(slos=(), overall_health="healthy", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "# SLO Dashboard [OK]" in md

    def test_includes_slo_sections(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [_make_record(f"t{i}", "done", completed_at=_BASE_TS - i * 100) for i in range(5)],
        )
        defs = get_default_slos()
        dashboard = build_slo_dashboard(defs, archive, now=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "## Task Completion" in md
        assert "## Quality Gate Pass" in md
        assert "## Latency p99 <300s" in md

    def test_includes_target_and_current(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=96.5,
            error_budget_remaining_pct=70.0,
            burn_rate_per_day=0.5,
            days_until_breach=None,
            status="healthy",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="healthy", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert ">= 95.0%" in md
        assert "96.50%" in md

    def test_critical_status_indicator(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=80.0,
            error_budget_remaining_pct=0.0,
            burn_rate_per_day=3.0,
            days_until_breach=0.0,
            status="critical",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="critical", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "[CRIT]" in md

    def test_breach_projection_hours(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=94.5,
            error_budget_remaining_pct=10.0,
            burn_rate_per_day=20.0,
            days_until_breach=0.5,
            status="warning",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="warning", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "hours" in md

    def test_breach_projection_days(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=94.5,
            error_budget_remaining_pct=50.0,
            burn_rate_per_day=5.0,
            days_until_breach=10.0,
            status="warning",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="warning", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "10.0 days" in md

    def test_on_track_projection(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=98.0,
            error_budget_remaining_pct=90.0,
            burn_rate_per_day=0.0,
            days_until_breach=None,
            status="healthy",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="healthy", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "On track" in md

    def test_includes_generated_timestamp(self) -> None:
        dashboard = SLODashboard(slos=(), overall_health="healthy", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert str(int(_BASE_TS)) in md

    def test_includes_budget_trend_sparkline(self) -> None:
        defn = _task_completion_def()
        status = SLOStatus(
            definition=defn,
            current_pct=96.0,
            error_budget_remaining_pct=80.0,
            burn_rate_per_day=0.5,
            days_until_breach=None,
            status="healthy",
        )
        dashboard = SLODashboard(slos=(status,), overall_health="healthy", generated_at=_BASE_TS)
        md = render_slo_markdown(dashboard)
        assert "Budget trend" in md


# ---------------------------------------------------------------------------
# Archive edge cases
# ---------------------------------------------------------------------------


class TestArchiveEdgeCases:
    def test_malformed_jsonl_line_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_make_record("t1", "done", completed_at=_BASE_TS - 100)) + "\n")
            f.write("NOT VALID JSON\n")
            f.write(json.dumps(_make_record("t2", "done", completed_at=_BASE_TS - 200)) + "\n")

        defn = _task_completion_def()
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 100.0

    def test_quality_gate_none_treated_as_pass_for_done(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", quality_gate_passed=None, completed_at=_BASE_TS - 100),
                _make_record("t2", "done", quality_gate_passed=None, completed_at=_BASE_TS - 200),
            ],
        )
        defn = _quality_gate_def(target=90.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # No explicit gate result on completed tasks => treated as passed
        assert status.current_pct == 100.0

    def test_latency_from_timestamps_when_no_duration(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        rec = _make_record("t1", "done", created_at=_BASE_TS - 500, completed_at=_BASE_TS - 200)
        rec["duration_seconds"] = None  # Force fallback to timestamps
        _write_archive(archive, [rec])

        defn = _latency_def(target=99.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        # Duration = 500 - 200 = 300s, threshold is 300s => within threshold
        assert status.current_pct == 100.0

    def test_warning_status_near_target(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        # 19/20 = 95%, target is 97% => below target, budget_consumed = 2% of 3% total
        records = [_make_record(f"t{i}", "done", completed_at=_BASE_TS - i * 100) for i in range(19)]
        records.append(_make_record("f0", "failed", completed_at=_BASE_TS - 1900))
        _write_archive(archive, records)

        defn = _task_completion_def(target=97.0)
        status = compute_slo_status(defn, archive, now=_BASE_TS)
        assert status.current_pct == 95.0
        assert status.status == "warning"
        assert status.error_budget_remaining_pct > 0.0
