"""Unit tests for coverage delta gate behavior."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bernstein.core.coverage_gate import CoverageGate


def _gate(
    tmp_path: Path,
    *,
    base_ref: str = "main",
    command: str = "coverage-cmd",
    ttl: int | None = None,
) -> CoverageGate:
    return CoverageGate(
        tmp_path,
        tmp_path,
        base_ref=base_ref,
        coverage_command=command,
        baseline_ttl_seconds=ttl,
    )


def _write_baseline(
    tmp_path: Path,
    *,
    base_ref: str = "main",
    command: str = "coverage-cmd",
    baseline_pct: float = 80.0,
    measured_at: float | None = None,
) -> Path:
    baseline_path = tmp_path / ".sdd" / "cache" / "coverage_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "base_ref": base_ref,
        "coverage_command": command,
        "baseline_pct": baseline_pct,
    }
    if measured_at is not None:
        payload["measured_at"] = measured_at
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")
    return baseline_path


def test_default_command_uses_isolated_runner() -> None:
    """Default command must respect CLAUDE.md and use the isolated runner."""
    assert "scripts/run_tests.py" in CoverageGate.DEFAULT_COMMAND
    # The previous default invoked pytest directly which leaks RAM on this
    # repository; regressing to it would re-introduce audit-032.
    assert "coverage run -m pytest tests/unit" not in CoverageGate.DEFAULT_COMMAND


def test_evaluate_passes_when_delta_is_non_negative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _write_baseline(tmp_path, baseline_pct=80.0, measured_at=time.time())
    monkeypatch.setattr(gate, "measure_current", lambda: 81.0)

    result = gate.evaluate()

    assert result.passed is True
    assert result.delta_pct == pytest.approx(1.0)
    assert result.status == "ok"
    assert result.stale is False
    assert result.detail == "Coverage: 80.0% -> 81.0% (delta: +1.0%)"


def test_evaluate_fails_when_delta_is_negative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _write_baseline(tmp_path, baseline_pct=85.0, measured_at=time.time())
    monkeypatch.setattr(gate, "measure_current", lambda: 80.0)

    result = gate.evaluate()

    assert result.passed is False
    assert result.delta_pct == pytest.approx(-5.0)
    assert result.status == "regressed"


def test_evaluate_short_circuits_when_baseline_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Missing baseline must NOT trigger a synchronous re-measurement.

    This is the audit-032 regression: the previous implementation would call
    ``measure_baseline`` inline (git worktree add + full pytest run) and block
    task completion for 5+ minutes.
    """
    gate = _gate(tmp_path)

    def _should_not_be_called() -> float:  # pragma: no cover - assertion path
        raise AssertionError("measure_baseline must not run on the completion path")

    monkeypatch.setattr(gate, "measure_baseline", _should_not_be_called)
    monkeypatch.setattr(gate, "measure_current", _should_not_be_called)

    result = gate.evaluate()

    assert result.passed is True, "gate must not block when baseline is absent"
    assert result.status == "skipped"
    assert result.stale is True
    assert "baseline not available" in result.detail.lower()


def test_evaluate_short_circuits_when_base_ref_mismatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path, base_ref="release")
    _write_baseline(
        tmp_path,
        base_ref="main",
        command="coverage-cmd",
        baseline_pct=99.9,
        measured_at=time.time(),
    )

    def _boom() -> float:  # pragma: no cover - assertion path
        raise AssertionError("measure_baseline must not run inline")

    monkeypatch.setattr(gate, "measure_baseline", _boom)

    result = gate.evaluate()

    assert result.status == "skipped"
    assert result.passed is True
    assert result.stale is True


def test_evaluate_flags_stale_baseline_but_still_compares(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stale-but-present baselines should still compare, with a warning."""
    gate = _gate(tmp_path, ttl=60)  # 60 second TTL
    ancient = time.time() - 3600  # 1 hour old
    _write_baseline(tmp_path, baseline_pct=70.0, measured_at=ancient)
    monkeypatch.setattr(gate, "measure_current", lambda: 72.0)

    result = gate.evaluate()

    assert result.stale is True
    assert result.passed is True
    assert result.delta_pct == pytest.approx(2.0)
    assert "stale" in result.detail.lower()


def test_evaluate_treats_legacy_baseline_without_timestamp_as_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _gate(tmp_path)
    _write_baseline(tmp_path, baseline_pct=75.0, measured_at=None)
    monkeypatch.setattr(gate, "measure_current", lambda: 75.0)

    result = gate.evaluate()

    assert result.stale is True
    assert result.passed is True


def test_refresh_baseline_persists_timestamp_and_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    monkeypatch.setattr(gate, "measure_baseline", lambda: 88.8)

    value = gate.refresh_baseline()

    assert value == pytest.approx(88.8)
    persisted = json.loads((tmp_path / ".sdd" / "cache" / "coverage_baseline.json").read_text(encoding="utf-8"))
    assert persisted["baseline_pct"] == pytest.approx(88.8)
    assert persisted["base_ref"] == "main"
    assert persisted["coverage_command"] == "coverage-cmd"
    assert isinstance(persisted["measured_at"], (int, float))
    assert persisted["measured_at"] <= time.time() + 1


def test_load_or_measure_baseline_uses_valid_cache(tmp_path: Path) -> None:
    gate = _gate(tmp_path, base_ref="main", command="cmd-a")
    _write_baseline(tmp_path, base_ref="main", command="cmd-a", baseline_pct=77.3, measured_at=time.time())

    baseline = gate._load_or_measure_baseline()

    assert baseline == pytest.approx(77.3)


def test_load_or_measure_baseline_invalidates_cache_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = _gate(tmp_path, base_ref="release", command="cmd-b")
    _write_baseline(tmp_path, base_ref="main", command="cmd-a", baseline_pct=99.9, measured_at=time.time())
    monkeypatch.setattr(gate, "measure_baseline", lambda: 66.6)

    baseline = gate._load_or_measure_baseline()
    persisted = json.loads((tmp_path / ".sdd" / "cache" / "coverage_baseline.json").read_text(encoding="utf-8"))

    assert baseline == pytest.approx(66.6)
    assert persisted["base_ref"] == "release"
    assert persisted["coverage_command"] == "cmd-b"
    assert persisted["baseline_pct"] == pytest.approx(66.6)
    assert "measured_at" in persisted


def test_load_cached_baseline_returns_none_on_malformed_json(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    baseline_path = tmp_path / ".sdd" / "cache" / "coverage_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text("{not valid json", encoding="utf-8")

    assert gate._load_cached_baseline() is None


def test_is_stale_respects_ttl(tmp_path: Path) -> None:
    gate = _gate(tmp_path, ttl=100)
    assert gate._is_stale(time.time()) is False
    assert gate._is_stale(time.time() - 1000) is True
    assert gate._is_stale(None) is True


def test_is_stale_with_disabled_ttl_treats_timestamped_cache_as_fresh(tmp_path: Path) -> None:
    gate = _gate(tmp_path, ttl=0)
    assert gate._is_stale(time.time() - 10**9) is False
    # Legacy entries without a timestamp remain stale even with TTL disabled.
    assert gate._is_stale(None) is True


def test_parse_total_pct_reads_percent_from_report(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": 91.25}}), encoding="utf-8")

    assert gate._parse_total_pct(report) == pytest.approx(91.25)


def test_parse_total_pct_raises_on_missing_percent(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"covered_lines": 10}}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="percent_covered"):
        gate._parse_total_pct(report)


def test_parse_total_pct_raises_on_invalid_structure(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"not_totals": {}}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing totals"):
        gate._parse_total_pct(report)
