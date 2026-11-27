"""Check the calibration replay example through its command-line entry point."""

import json

from tests.unit.examples import run_demo


def test_replay_reproduces_calibration_and_preserves_the_hard_verdict(tmp_path):
    result = run_demo("intent-calibration", tmp_path)
    assert result.returncode == 0, result.stderr
    cases = json.loads(result.stdout)
    with_rows = cases["with_calibration"]
    without_rows = cases["without_calibration"]

    summary = with_rows["summary"]
    assert summary["status"] == "available"
    assert summary["version"] == "intent/v1"
    assert summary["sample_count"] == 2
    assert summary["report"]["brier"] == 0.0625
    assert without_rows["summary"] == {"status": "unavailable", "version": None, "sample_count": 0, "report": None}
    assert with_rows["replay_matches_receipt"] is True
    assert without_rows["replay_matches_receipt"] is True
    assert with_rows["passed_gate"] is without_rows["passed_gate"] is True
