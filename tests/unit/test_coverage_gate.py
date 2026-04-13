"""Unit tests for coverage delta gate behavior."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.coverage_gate import CoverageGate


def _gate(tmp_path: Path, *, base_ref: str = "main", command: str = "coverage-cmd") -> CoverageGate:
    return CoverageGate(tmp_path, tmp_path, base_ref=base_ref, coverage_command=command)


def test_evaluate_passes_when_delta_is_non_negative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: 80.0)
    monkeypatch.setattr(gate, "measure_current", lambda: 81.0)

    result = gate.evaluate()

    assert result.passed is True
    assert result.delta_pct == pytest.approx(1.0)
    assert result.detail == "Coverage: 80.0% -> 81.0% (delta: +1.0%)"


def test_evaluate_fails_when_delta_is_negative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    monkeypatch.setattr(gate, "_load_or_measure_baseline", lambda: 85.0)
    monkeypatch.setattr(gate, "measure_current", lambda: 80.0)

    result = gate.evaluate()

    assert result.passed is False
    assert result.delta_pct == pytest.approx(-5.0)


def test_load_or_measure_baseline_uses_valid_cache(tmp_path: Path) -> None:
    gate = _gate(tmp_path, base_ref="main", command="cmd-a")
    baseline_path = tmp_path / ".sdd" / "cache" / "coverage_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps({"base_ref": "main", "coverage_command": "cmd-a", "baseline_pct": 77.3}),
        encoding="utf-8",
    )

    baseline = gate._load_or_measure_baseline()

    assert baseline == pytest.approx(77.3)


def test_load_or_measure_baseline_invalidates_cache_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = _gate(tmp_path, base_ref="release", command="cmd-b")
    baseline_path = tmp_path / ".sdd" / "cache" / "coverage_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps({"base_ref": "main", "coverage_command": "cmd-a", "baseline_pct": 99.9}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "measure_baseline", lambda: 66.6)

    baseline = gate._load_or_measure_baseline()
    persisted = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert baseline == pytest.approx(66.6)
    assert persisted["base_ref"] == "release"
    assert persisted["coverage_command"] == "cmd-b"
    assert persisted["baseline_pct"] == pytest.approx(66.6)


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
