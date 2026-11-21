"""Recorded calibration metadata stays separate from deterministic hard drift."""

import json

import pytest

from bernstein.eval.bench.goal_drift_suite import (
    DriftContract,
    DriftTrajectoryCurve,
    GoalDriftReplayAdapter,
    GoalDriftTask,
    evaluate_trajectory_drift,
)
from bernstein.eval.calibration import CalibrationRecord, compute_report


def _task() -> GoalDriftTask:
    return GoalDriftTask(
        "calibration-fixture", "Local fixture", DriftContract(("src/allowed.py",), (), (), "", ""), steps=("read",)
    )


def _records() -> tuple[CalibrationRecord, ...]:
    return (
        CalibrationRecord(100, "judge", "intent/v1", 0.9, True),
        CalibrationRecord(101, "judge", "intent/v1", 0.2, False),
    )


def _payload() -> list[dict]:
    return [
        {
            "ts": r.timestamp,
            "decision_kind": r.decision_kind,
            "policy_path": r.policy_path,
            "predicted_prob": r.predicted_prob,
            "observed_outcome": r.observed_outcome,
            "metadata": {"unrelated": "do-not-copy"},
        }
        for r in _records()
    ]


def _events(path: str = "src/allowed.py") -> list[dict]:
    return [{"step": 0, "touched_paths": [path], "intent_score": 0.8}]


def test_recorded_calibration_reports_existing_version_count_and_metrics() -> None:
    curve = evaluate_trajectory_drift(_task(), _events(), intent_calibration_records=_records())
    summary = curve.to_dict()["intent_calibration"]
    assert summary["status"] == "available"
    assert summary["version"] == "intent/v1"
    assert summary["sample_count"] == 2
    assert summary["report"] == compute_report(_records()).to_dict()
    assert summary["report"]["brier"] == pytest.approx(0.025)


@pytest.mark.parametrize("path", ["src/allowed.py", "outside.py"])
def test_missing_calibration_is_unavailable_and_preserves_hard_result(path: str) -> None:
    bare = evaluate_trajectory_drift(_task(), _events(path))
    calibrated = evaluate_trajectory_drift(_task(), _events(path), intent_calibration_records=_records())
    assert bare.to_dict()["intent_calibration"]["status"] == "unavailable"
    assert bare.passed_gate is calibrated.passed_gate
    assert bare.step_measurements == calibrated.step_measurements
    assert bare.max_hard_drift == calibrated.max_hard_drift
    assert bare.intent_scores == calibrated.intent_scores


def test_same_recorded_values_and_json_roundtrip_have_identical_output() -> None:
    one = evaluate_trajectory_drift(_task(), _events(), intent_calibration_records=_records())
    two = evaluate_trajectory_drift(_task(), _events(), intent_calibration_records=_records())

    def canonical(curve):
        return json.dumps(curve.to_dict(), sort_keys=True, separators=(",", ":"))

    assert canonical(one) == canonical(two)
    assert canonical(DriftTrajectoryCurve.from_dict(json.loads(canonical(one)))) == canonical(one)


def test_adapter_records_only_consumed_values_and_scorer_replays_them() -> None:
    task = _task().to_bench_task()
    adapter = GoalDriftReplayAdapter(
        trajectory_data={
            task.id: {
                "events": _events(),
                "intent_calibration_records": _payload(),
            }
        }
    )
    receipt = adapter.run_task(task, {})
    assert receipt["drift_curve"]["intent_calibration"]["sample_count"] == 2
    assert all("metadata" not in row for row in receipt["intent_calibration_records"])
    passed, _, output = GoalDriftReplayAdapter().score_task(task, json.loads(json.dumps(receipt)))
    assert passed
    assert output["drift_curve"] == receipt["drift_curve"]


@pytest.mark.parametrize("raw", [None, [], [{"policy_path": "missing-other-fields"}], "invalid"])
def test_invalid_recorded_calibration_does_not_change_hard_gate(raw: object) -> None:
    task = _task().to_bench_task()
    adapter = GoalDriftReplayAdapter(trajectory_data={"events": _events(), "intent_calibration_records": raw})
    receipt = adapter.run_task(task, {})
    assert receipt["passed_gate"]
    assert receipt["drift_curve"]["intent_calibration"]["status"] == "unavailable"
    assert adapter.score_task(task, receipt)[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("ts", float("inf"), id="nonfinite-timestamp"),
        pytest.param("ts", True, id="boolean-timestamp"),
        pytest.param("predicted_prob", 1.1, id="probability-out-of-range"),
        pytest.param("observed_outcome", 1, id="nonboolean-outcome"),
    ],
)
@pytest.mark.parametrize("path", ["src/allowed.py", "outside.py"])
def test_malformed_second_calibration_row_discards_the_population(field: str, value: object, path: str) -> None:
    goal = _task()
    task = goal.to_bench_task()
    events = _events(path)
    baseline = evaluate_trajectory_drift(goal, events).to_dict()
    rows = _payload()
    rows[1][field] = value
    adapter = GoalDriftReplayAdapter(trajectory_data={"events": events, "intent_calibration_records": rows})

    receipt = adapter.run_task(task, {})
    assert receipt["intent_calibration_records"] == []
    assert receipt["passed_gate"] is baseline["passed_gate"]

    restored = json.loads(json.dumps(receipt, allow_nan=False))
    passed, _, scored = GoalDriftReplayAdapter().score_task(task, restored)
    assert passed is baseline["passed_gate"]
    for curve in (receipt["drift_curve"], scored["drift_curve"]):
        assert curve["intent_calibration"]["status"] == "unavailable"
        assert curve["intent_calibration"]["sample_count"] == 0
        assert curve == baseline


def test_mixed_versions_are_unavailable() -> None:
    records = (*_records(), CalibrationRecord(102, "judge", "intent/v2", 0.5, True))
    curve = evaluate_trajectory_drift(_task(), _events(), intent_calibration_records=records)
    assert curve.to_dict()["intent_calibration"]["status"] == "unavailable"
    assert curve.passed_gate


def test_scorer_rederives_metadata_instead_of_trusting_stored_curve() -> None:
    task = _task().to_bench_task()
    adapter = GoalDriftReplayAdapter(trajectory_data={"events": _events(), "intent_calibration_records": _payload()})
    receipt = adapter.run_task(task, {})
    expected = receipt["drift_curve"]["intent_calibration"]
    receipt["drift_curve"]["intent_calibration"] = {"status": "available", "version": "invented", "sample_count": 999}
    _, _, output = GoalDriftReplayAdapter().score_task(task, receipt)
    assert output["drift_curve"]["intent_calibration"] == expected


def test_exported_metadata_does_not_alias_curve_state() -> None:
    curve = evaluate_trajectory_drift(_task(), _events(), intent_calibration_records=_records())
    before = json.dumps(curve.to_dict(), sort_keys=True)
    exported = curve.to_dict()
    exported["intent_calibration"]["report"]["buckets"].clear()
    exported["intent_calibration"]["sample_count"] = 999
    assert json.dumps(curve.to_dict(), sort_keys=True) == before
