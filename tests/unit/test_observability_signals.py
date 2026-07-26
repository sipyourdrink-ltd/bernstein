"""Unit tests for evolution observability signals and detector wiring.

Covers ``src/bernstein/evolution/observability_signals.py`` and the
``OpportunityDetector.identify_observability_opportunities`` wiring:

- ``detect_regressions`` flags a security increase, stays quiet on a
  flat / improved corpus, and reports a missing baseline as an explicit
  ``baseline_present=False`` scan rather than a clean empty result,
- ``OpportunityDetector`` only emits observability opportunities when a
  snapshots directory is configured (opt-in; default stays a no-op).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from bernstein.evolution import observability_signals as sig
from bernstein.evolution.detector import OpportunityDetector


def _metric(name: str, numeric: float, status: str) -> dict[str, Any]:
    return {"name": name, "value": str(numeric), "numeric": numeric, "threshold": "0", "threshold_status": status}


def _backend(name: str, status: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {"backend": name, "status": status, "detail": "", "error": None, "metrics": metrics}


def _write(dir_: Path, day: str, *backends: dict[str, Any]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{day}.json").write_text(json.dumps({"summary": {}, "backends": list(backends)}), encoding="utf-8")


# --------------------------------------------------------------------------
# detect_regressions
# --------------------------------------------------------------------------


def test_detect_regressions_flags_security_increase(tmp_path: Path) -> None:
    _write(tmp_path, "2026-07-10", _backend("code-scanning", "ok", [_metric("high_alerts", 0.0, "ok")]))
    _write(tmp_path, "2026-07-11", _backend("code-scanning", "warn", [_metric("high_alerts", 2.0, "warn")]))

    scan = sig.detect_regressions(tmp_path)

    assert scan.baseline_present is True
    assert len(scan.regressions) == 1
    assert scan.regressions[0].kind == "security"
    assert scan.regressions[0].backend == "code-scanning"
    assert scan.regressions[0].severity == "high"
    assert scan.regressions[0].delta == 2.0


def test_detect_regressions_quiet_on_flat_and_improved(tmp_path: Path) -> None:
    _write(tmp_path, "2026-07-10", _backend("code-scanning", "warn", [_metric("high_alerts", 3.0, "warn")]))
    _write(tmp_path, "2026-07-11", _backend("code-scanning", "ok", [_metric("high_alerts", 1.0, "warn")]))

    scan = sig.detect_regressions(tmp_path)

    assert scan.baseline_present is True
    assert scan.regressions == []


def test_detect_regressions_skips_first_observation(tmp_path: Path) -> None:
    # high_alerts only appears in the newer snapshot -> first observation, not a regression.
    _write(tmp_path, "2026-07-10", _backend("code-scanning", "ok", [_metric("open_alerts", 0.0, "ok")]))
    _write(tmp_path, "2026-07-11", _backend("code-scanning", "warn", [_metric("high_alerts", 3.0, "warn")]))

    scan = sig.detect_regressions(tmp_path)

    assert scan.baseline_present is True
    assert scan.regressions == []


def test_detect_regressions_reports_missing_baseline(tmp_path: Path) -> None:
    """A single snapshot is an absent baseline, not a clean comparison."""

    _write(tmp_path, "2026-07-11", _backend("code-scanning", "fail", [_metric("critical_alerts", 5.0, "fail")]))

    scan = sig.detect_regressions(tmp_path)

    assert scan.baseline_present is False
    assert scan.regressions == []


def test_detect_regressions_reports_empty_corpus(tmp_path: Path) -> None:
    scan = sig.detect_regressions(tmp_path / "missing")

    assert scan.baseline_present is False
    assert scan.regressions == []


# --------------------------------------------------------------------------
# OpportunityDetector wiring (opt-in)
# --------------------------------------------------------------------------


def _collector_stub() -> MagicMock:
    collector = MagicMock()
    collector.get_recent_cost_metrics.return_value = []
    collector.get_recent_task_metrics.return_value = []
    return collector


def test_detector_no_op_without_snapshots_dir() -> None:
    detector = OpportunityDetector(_collector_stub())

    assert detector.identify_observability_opportunities() == []


def test_detector_emits_opportunity_for_regression(tmp_path: Path) -> None:
    _write(tmp_path, "2026-07-10", _backend("code-scanning", "ok", [_metric("critical_alerts", 0.0, "ok")]))
    _write(tmp_path, "2026-07-11", _backend("code-scanning", "fail", [_metric("critical_alerts", 2.0, "fail")]))

    detector = OpportunityDetector(_collector_stub(), observability_snapshots_dir=tmp_path)
    opps = detector.identify_observability_opportunities()

    assert len(opps) == 1
    assert opps[0].affected_components == ["code-scanning"]
    assert opps[0].risk_level == "high"
    assert "critical_alerts" in opps[0].title

    # It also flows through the umbrella identify_opportunities().
    assert any("critical_alerts" in o.title for o in detector.identify_opportunities())
