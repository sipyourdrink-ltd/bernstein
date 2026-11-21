"""Regression tests for the protocol compatibility baseline."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_compatibility.py"
COMBINATION = "3.12+mcp1.0+a2a0.2"


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_compatibility", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("baseline_format", ["bootstrapped", "keyed"])
@pytest.mark.parametrize(
    ("status", "expected_status", "expected_changes"),
    [("fail", "breaking_change", [f"{COMBINATION}: regression from pass to fail"]), ("pass", "compatible", [])],
)
def test_bootstrapped_baseline_detects_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checker: ModuleType,
    baseline_format: str,
    status: str,
    expected_status: str,
    expected_changes: list[str],
) -> None:
    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--current-results", str(current), "--baseline", str(baseline)])

    row = {"python": "3.12", "mcp": "1.0", "a2a": "0.2", "status": "pass"}
    current.write_text(json.dumps({"results": [row]}), encoding="utf-8")
    if baseline_format == "keyed":
        baseline.write_text(json.dumps({"compatibility": {COMBINATION: {"status": "pass"}}}), encoding="utf-8")
    assert checker.main()["status"] == "compatible"

    current.write_text(json.dumps({"results": [{**row, "status": status}]}), encoding="utf-8")
    report = checker.main()
    assert report["status"] == expected_status
    assert report["breaking_changes"] == expected_changes
    assert report["new_compatibilities"] == []
