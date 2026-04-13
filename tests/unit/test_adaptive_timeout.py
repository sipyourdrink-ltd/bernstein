"""Tests for bernstein.core.orchestration.adaptive_timeout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.orchestration.adaptive_timeout import (
    TimeoutEstimate,
    TimeoutFactors,
    clamp_timeout,
    estimate_timeout,
    get_historical_average,
)
from bernstein.core.tasks.models import Complexity, Scope, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    role: str = "backend",
    scope: Scope = Scope.MEDIUM,
    complexity: Complexity = Complexity.MEDIUM,
    model: str | None = None,
    owned_files: list[str] | None = None,
) -> Task:
    """Create a minimal Task for timeout tests."""
    return Task(
        id="t-1",
        title="test task",
        description="a test",
        role=role,
        scope=scope,
        complexity=complexity,
        model=model,
        owned_files=owned_files or [],
    )


def _write_archive(path: Path, records: list[dict[str, object]]) -> None:
    """Write JSONL records to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# TimeoutFactors / TimeoutEstimate — frozen dataclass basics
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_timeout_factors_frozen(self) -> None:
        f = TimeoutFactors(
            complexity_score=0.5,
            scope_multiplier=1800.0,
            model_speed_factor=1.0,
            historical_avg_s=None,
            file_count=0,
        )
        with pytest.raises(AttributeError):
            f.complexity_score = 0.9  # type: ignore[misc]

    def test_timeout_estimate_frozen(self) -> None:
        f = TimeoutFactors(0.5, 1800.0, 1.0, None, 0)
        est = TimeoutEstimate(
            timeout_s=1800.0,
            min_timeout_s=300.0,
            max_timeout_s=7200.0,
            confidence=0.5,
            factors=f,
        )
        with pytest.raises(AttributeError):
            est.timeout_s = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Scope base timeout
# ---------------------------------------------------------------------------


class TestScopeBaseTimeout:
    def test_small_scope(self) -> None:
        task = _make_task(scope=Scope.SMALL, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="sonnet")
        # small base = 900, complexity 1.0, model 1.0 → 900
        assert est.timeout_s == pytest.approx(900.0)

    def test_medium_scope(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="sonnet")
        assert est.timeout_s == pytest.approx(1800.0)

    def test_large_scope(self) -> None:
        task = _make_task(scope=Scope.LARGE, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="sonnet")
        assert est.timeout_s == pytest.approx(3600.0)


# ---------------------------------------------------------------------------
# Complexity multiplier
# ---------------------------------------------------------------------------


class TestComplexityMultiplier:
    def test_low_complexity(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.LOW)
        est = estimate_timeout(task, model="sonnet")
        # 1800 * 0.7 = 1260
        assert est.timeout_s == pytest.approx(1260.0)

    def test_high_complexity(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.HIGH)
        est = estimate_timeout(task, model="sonnet")
        # 1800 * 1.5 = 2700
        assert est.timeout_s == pytest.approx(2700.0)


# ---------------------------------------------------------------------------
# Model speed factor
# ---------------------------------------------------------------------------


class TestModelSpeedFactor:
    def test_haiku_faster(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="haiku")
        # 1800 * 1.0 * 0.5 = 900
        assert est.timeout_s == pytest.approx(900.0)

    def test_opus_slower(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="opus")
        # 1800 * 1.0 * 1.5 = 2700
        assert est.timeout_s == pytest.approx(2700.0)

    def test_unknown_model_defaults_to_1(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="gemini-pro")
        assert est.timeout_s == pytest.approx(1800.0)

    def test_model_from_task(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM, model="opus")
        est = estimate_timeout(task)
        assert est.timeout_s == pytest.approx(2700.0)

    def test_explicit_model_overrides_task(self) -> None:
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM, model="opus")
        est = estimate_timeout(task, model="haiku")
        assert est.timeout_s == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# File count
# ---------------------------------------------------------------------------


class TestFileCount:
    def test_files_add_30s_each(self) -> None:
        task = _make_task(
            scope=Scope.SMALL,
            complexity=Complexity.MEDIUM,
            owned_files=["a.py", "b.py", "c.py"],
        )
        est = estimate_timeout(task, model="sonnet")
        # 900 * 1.0 * 1.0 + 3*30 = 990
        assert est.timeout_s == pytest.approx(990.0)

    def test_no_files(self) -> None:
        task = _make_task(scope=Scope.SMALL, complexity=Complexity.MEDIUM)
        est = estimate_timeout(task, model="sonnet")
        assert est.factors.file_count == 0
        assert est.timeout_s == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# Historical calibration
# ---------------------------------------------------------------------------


class TestHistoricalCalibration:
    def test_historical_overrides_when_larger(self) -> None:
        task = _make_task(scope=Scope.SMALL, complexity=Complexity.LOW)
        # computed: 900 * 0.7 = 630, historical: 1000 * 1.5 = 1500
        est = estimate_timeout(task, model="sonnet", historical_data=1000.0)
        assert est.timeout_s == pytest.approx(1500.0)
        assert est.confidence == pytest.approx(0.8)

    def test_historical_does_not_override_when_smaller(self) -> None:
        task = _make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        # computed: 3600 * 1.5 = 5400, historical: 100 * 1.5 = 150
        est = estimate_timeout(task, model="sonnet", historical_data=100.0)
        assert est.timeout_s == pytest.approx(5400.0)
        assert est.confidence == pytest.approx(0.8)

    def test_no_historical_gives_lower_confidence(self, tmp_path: Path) -> None:
        task = _make_task()
        est = estimate_timeout(task, model="sonnet", historical_data=None, archive_path=tmp_path / "nonexistent.jsonl")
        assert est.confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# get_historical_average
# ---------------------------------------------------------------------------


class TestGetHistoricalAverage:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = get_historical_average(
            "backend", "medium", "medium", tmp_path / "nope.jsonl"
        )
        assert result is None

    def test_returns_average_of_matching_records(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                {"role": "backend", "status": "done", "duration_seconds": 100},
                {"role": "backend", "status": "done", "duration_seconds": 200},
                {"role": "frontend", "status": "done", "duration_seconds": 999},
                {"role": "backend", "status": "failed", "duration_seconds": 50},
            ],
        )
        avg = get_historical_average("backend", "medium", "medium", archive)
        assert avg == pytest.approx(150.0)

    def test_skips_zero_duration(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                {"role": "qa", "status": "done", "duration_seconds": 0},
                {"role": "qa", "status": "done", "duration_seconds": 300},
            ],
        )
        avg = get_historical_average("qa", "small", "low", archive)
        assert avg == pytest.approx(300.0)

    def test_returns_none_when_no_matches(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [{"role": "frontend", "status": "done", "duration_seconds": 500}],
        )
        assert get_historical_average("backend", "medium", "medium", archive) is None

    def test_tolerates_malformed_lines(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("w") as fh:
            fh.write("not-json\n")
            fh.write(json.dumps({"role": "qa", "status": "done", "duration_seconds": 200}) + "\n")
        avg = get_historical_average("qa", "small", "low", archive)
        assert avg == pytest.approx(200.0)

    def test_archive_from_estimate(self, tmp_path: Path) -> None:
        """estimate_timeout reads the archive when historical_data is None."""
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                {"role": "backend", "status": "done", "duration_seconds": 5000},
            ],
        )
        task = _make_task(scope=Scope.SMALL, complexity=Complexity.LOW)
        est = estimate_timeout(task, model="sonnet", archive_path=archive)
        # historical: 5000 * 1.5 = 7500 → clamped to max 7200
        assert est.timeout_s == pytest.approx(7200.0)
        assert est.confidence == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# clamp_timeout
# ---------------------------------------------------------------------------


class TestClampTimeout:
    def _make_estimate(self, timeout_s: float) -> TimeoutEstimate:
        f = TimeoutFactors(0.5, 1800.0, 1.0, None, 0)
        return TimeoutEstimate(
            timeout_s=timeout_s,
            min_timeout_s=300.0,
            max_timeout_s=7200.0,
            confidence=0.5,
            factors=f,
        )

    def test_clamp_below_min(self) -> None:
        est = clamp_timeout(self._make_estimate(100.0), min_s=300.0, max_s=7200.0)
        assert est.timeout_s == pytest.approx(300.0)

    def test_clamp_above_max(self) -> None:
        est = clamp_timeout(self._make_estimate(99999.0), min_s=300.0, max_s=7200.0)
        assert est.timeout_s == pytest.approx(7200.0)

    def test_within_bounds_unchanged(self) -> None:
        est = clamp_timeout(self._make_estimate(1500.0), min_s=300.0, max_s=7200.0)
        assert est.timeout_s == pytest.approx(1500.0)

    def test_custom_bounds(self) -> None:
        est = clamp_timeout(self._make_estimate(50.0), min_s=100.0, max_s=200.0)
        assert est.timeout_s == pytest.approx(100.0)
        assert est.min_timeout_s == pytest.approx(100.0)
        assert est.max_timeout_s == pytest.approx(200.0)

    def test_preserves_factors(self) -> None:
        raw = self._make_estimate(1500.0)
        est = clamp_timeout(raw)
        assert est.factors is raw.factors


# ---------------------------------------------------------------------------
# Factors breakdown
# ---------------------------------------------------------------------------


class TestFactorsBreakdown:
    def test_factors_populated(self) -> None:
        task = _make_task(
            scope=Scope.LARGE,
            complexity=Complexity.HIGH,
            owned_files=["x.py"],
        )
        est = estimate_timeout(task, model="opus", historical_data=500.0)
        f = est.factors
        assert f.complexity_score == pytest.approx(1.0)
        assert f.scope_multiplier == pytest.approx(3600.0)
        assert f.model_speed_factor == pytest.approx(1.5)
        assert f.historical_avg_s == pytest.approx(500.0)
        assert f.file_count == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_minimum_floor_enforced(self) -> None:
        """Haiku + small + low complexity gives 900*0.7*0.5=315 > 300, within bounds."""
        task = _make_task(scope=Scope.SMALL, complexity=Complexity.LOW)
        est = estimate_timeout(task, model="haiku")
        # 900 * 0.7 * 0.5 = 315 — above 300, so not clamped
        assert est.timeout_s == pytest.approx(315.0)

    def test_maximum_cap_enforced(self) -> None:
        """Large + high + opus = 3600*1.5*1.5 = 8100 → clamped to 7200."""
        task = _make_task(scope=Scope.LARGE, complexity=Complexity.HIGH)
        est = estimate_timeout(task, model="opus")
        assert est.timeout_s == pytest.approx(7200.0)

    def test_combined_file_count_and_complexity(self) -> None:
        task = _make_task(
            scope=Scope.MEDIUM,
            complexity=Complexity.HIGH,
            owned_files=[f"f{i}.py" for i in range(10)],
        )
        est = estimate_timeout(task, model="sonnet")
        # 1800 * 1.5 + 10*30 = 2700 + 300 = 3000
        assert est.timeout_s == pytest.approx(3000.0)
