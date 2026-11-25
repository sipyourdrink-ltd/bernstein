"""Unit tests for ``bernstein.eval.calibration``.

This suite covers every documented edge case for the calibration primitives:
single-prediction inputs, perfect calibration, perfectly miscalibrated
inputs, NaN / Inf rejection, missing-outcome handling, malformed log lines,
round-tripping, duration parsing, and reliability-diagram structure.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from bernstein.eval.calibration import (
    DEFAULT_LOG_PATH,
    BrierScore,
    CalibrationLogError,
    CalibrationRecord,
    CalibrationReport,
    ReliabilityBucket,
    compute_brier,
    compute_report,
    expected_calibration_error,
    load_log,
    log_decision,
    parse_duration,
    reliability_diagram_data,
)

# ---------------------------------------------------------------------------
# BrierScore
# ---------------------------------------------------------------------------


def test_brier_empty_inputs() -> None:
    """Empty predictions return a zero score with zero samples."""
    result = compute_brier([], [])
    assert result == BrierScore(score=0.0, sample_count=0)


def test_brier_single_prediction_perfect() -> None:
    """A single perfect prediction yields a Brier score of 0."""
    result = compute_brier([1.0], [True])
    assert result.score == 0.0
    assert result.sample_count == 1


def test_brier_single_prediction_perfectly_wrong() -> None:
    """A single perfectly miscalibrated prediction yields the maximum 1.0."""
    result = compute_brier([1.0], [False])
    assert result.score == 1.0


def test_brier_single_prediction_half() -> None:
    """A 0.5 prediction yields a Brier score of 0.25 regardless of outcome."""
    won = compute_brier([0.5], [True]).score
    lost = compute_brier([0.5], [False]).score
    assert won == pytest.approx(0.25)
    assert lost == pytest.approx(0.25)


def test_brier_perfectly_calibrated() -> None:
    """All probabilities of 0 or 1 matching outcomes yield zero error."""
    result = compute_brier([0.0, 1.0, 0.0, 1.0], [False, True, False, True])
    assert result.score == 0.0
    assert result.sample_count == 4


def test_brier_perfectly_anticalibrated() -> None:
    """All probabilities of 0 or 1 mismatched yield 1.0."""
    result = compute_brier([0.0, 1.0, 0.0, 1.0], [True, False, True, False])
    assert result.score == pytest.approx(1.0)


def test_brier_mixed_known_reference() -> None:
    """Reference computation: hand-checked against the textbook formula."""
    preds = [0.9, 0.8, 0.3, 0.5]
    obs = [True, True, False, True]
    expected = ((0.9 - 1) ** 2 + (0.8 - 1) ** 2 + (0.3 - 0) ** 2 + (0.5 - 1) ** 2) / 4
    assert compute_brier(preds, obs).score == pytest.approx(expected, abs=1e-12)


def test_brier_range_within_unit_interval() -> None:
    """Brier outputs are clamped to ``[0, 1]`` by construction."""
    preds = [0.0, 0.25, 0.5, 0.75, 1.0]
    obs = [True, True, False, False, True]
    result = compute_brier(preds, obs)
    assert 0.0 <= result.score <= 1.0


def test_brier_length_mismatch_raises() -> None:
    """Mismatched lengths raise ``CalibrationLogError``."""
    with pytest.raises(CalibrationLogError):
        compute_brier([0.5, 0.5], [True])


def test_brier_rejects_nan_prediction() -> None:
    """NaN predictions are rejected with a descriptive error."""
    with pytest.raises(CalibrationLogError, match="NaN"):
        compute_brier([math.nan], [True])


def test_brier_rejects_inf_prediction() -> None:
    """Infinite predictions are rejected."""
    with pytest.raises(CalibrationLogError, match="infinite"):
        compute_brier([math.inf], [True])


def test_brier_rejects_negative_prediction() -> None:
    """Probabilities outside ``[0, 1]`` are rejected."""
    with pytest.raises(CalibrationLogError, match=r"outside \[0, 1\]"):
        compute_brier([-0.1], [True])


def test_brier_rejects_above_one_prediction() -> None:
    """Probabilities above 1.0 are rejected."""
    with pytest.raises(CalibrationLogError, match=r"outside \[0, 1\]"):
        compute_brier([1.1], [False])


def test_brier_accepts_n_equal_two_minimum() -> None:
    """n=2 is the minimum non-trivial case and must be supported."""
    assert compute_brier([0.0, 1.0], [False, True]).score == 0.0


def test_brier_large_input_does_not_overflow() -> None:
    """Large inputs (10_000 predictions) compute without overflow."""
    n = 10_000
    preds = [0.5] * n
    obs = [True] * (n // 2) + [False] * (n // 2)
    score = compute_brier(preds, obs).score
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(0.25, abs=1e-12)


def test_brier_sample_count_matches_input() -> None:
    """sample_count exactly equals the number of paired observations."""
    result = compute_brier([0.1, 0.2, 0.3], [True, False, True])
    assert result.sample_count == 3


# ---------------------------------------------------------------------------
# Expected Calibration Error
# ---------------------------------------------------------------------------


def test_ece_empty_returns_zero() -> None:
    """ECE on empty inputs returns 0.0 - there is no error to report."""
    assert expected_calibration_error([], []) == 0.0


def test_ece_perfect_calibration_yields_zero() -> None:
    """When predictions match observed frequencies exactly, ECE == 0."""
    # All predictions at 0.5; outcomes balanced -> bucket prediction mean
    # (0.5) equals bucket observed mean (0.5).
    preds = [0.5, 0.5, 0.5, 0.5]
    obs = [True, False, True, False]
    assert expected_calibration_error(preds, obs) == pytest.approx(0.0, abs=1e-12)


def test_ece_perfectly_anticalibrated_is_one() -> None:
    """All predictions 0.0 with all wins yields ECE == 1.0."""
    preds = [0.0, 0.0, 0.0, 0.0]
    obs = [True, True, True, True]
    assert expected_calibration_error(preds, obs) == pytest.approx(1.0, abs=1e-12)


def test_ece_reference_calculation() -> None:
    """Hand-computed ECE on a small fixture matches the implementation."""
    # Bucket assignment for bin_count=2:
    #  - [0.0, 0.5): preds 0.1 -> True, 0.3 -> False  (n=2; pred_mean=0.2; obs_mean=0.5)
    #  - [0.5, 1.0]: preds 0.7 -> True, 0.9 -> True   (n=2; pred_mean=0.8; obs_mean=1.0)
    # ECE = (2/4) * |0.2 - 0.5| + (2/4) * |0.8 - 1.0| = 0.15 + 0.10 = 0.25.
    preds = [0.1, 0.3, 0.7, 0.9]
    obs = [True, False, True, True]
    assert expected_calibration_error(preds, obs, bin_count=2) == pytest.approx(0.25, abs=1e-12)


def test_ece_invalid_bin_count_raises() -> None:
    """ECE with bin_count < 1 raises ``ValueError``."""
    with pytest.raises(ValueError, match="bin_count"):
        expected_calibration_error([0.5], [True], bin_count=0)


def test_ece_rejects_nan() -> None:
    """NaN predictions are rejected by ECE too."""
    with pytest.raises(CalibrationLogError):
        expected_calibration_error([math.nan, 0.5], [True, False])


def test_ece_length_mismatch() -> None:
    """ECE length mismatch raises ``CalibrationLogError``."""
    with pytest.raises(CalibrationLogError):
        expected_calibration_error([0.5], [True, False])


def test_ece_within_unit_interval() -> None:
    """ECE outputs lie in ``[0, 1]``."""
    preds = [0.1, 0.4, 0.7, 0.95]
    obs = [True, False, True, True]
    err = expected_calibration_error(preds, obs)
    assert 0.0 <= err <= 1.0


# ---------------------------------------------------------------------------
# Reliability diagram data
# ---------------------------------------------------------------------------


def test_reliability_default_bin_count_returns_ten_buckets() -> None:
    """Default bin_count is 10."""
    buckets = reliability_diagram_data([0.5], [True])
    assert len(buckets) == 10


def test_reliability_buckets_have_monotonic_lower_bounds() -> None:
    """Bucket lower bounds are monotonically non-decreasing."""
    from itertools import pairwise

    buckets = reliability_diagram_data([0.5], [True])
    for prev, curr in pairwise(buckets):
        assert curr.lower >= prev.lower


def test_reliability_top_bucket_includes_one_point_zero() -> None:
    """A prediction of exactly 1.0 maps to the last bucket."""
    buckets = reliability_diagram_data([1.0], [True], bin_count=4)
    last = buckets[-1]
    assert last.count == 1
    assert last.upper == 1.0


def test_reliability_zero_lands_in_first_bucket() -> None:
    """A prediction of exactly 0.0 maps to the first bucket."""
    buckets = reliability_diagram_data([0.0], [False], bin_count=4)
    assert buckets[0].count == 1


@pytest.mark.parametrize(
    ("probability", "expected_index"),
    [
        (math.nextafter(0.29, 0.0), 28),
        (0.29, 29),
        (math.nextafter(0.29, 1.0), 29),
        (0.0, 0),
        (1.0, 99),
    ],
)
def test_reliability_assigns_an_exact_interior_edge_to_its_bucket(probability: float, expected_index: int) -> None:
    buckets = reliability_diagram_data([probability], [True], bin_count=100)
    assert buckets[28].upper == buckets[29].lower == 0.29
    assert [i for i, bucket in enumerate(buckets) if bucket.count] == [expected_index]


def test_reliability_empty_buckets_preserve_axis_structure() -> None:
    """Empty buckets are still emitted so plot axes line up."""
    buckets = reliability_diagram_data([0.5], [True], bin_count=4)
    assert len(buckets) == 4
    assert sum(b.count for b in buckets) == 1


def test_reliability_invalid_bin_count() -> None:
    """bin_count < 1 raises ``ValueError``."""
    with pytest.raises(ValueError, match="bin_count"):
        reliability_diagram_data([0.5], [True], bin_count=0)


def test_reliability_length_mismatch() -> None:
    """Length mismatch is rejected by the reliability helper."""
    with pytest.raises(CalibrationLogError):
        reliability_diagram_data([0.5], [True, False])


def test_reliability_bucket_count_sums_to_input_size() -> None:
    """Total count across all buckets equals the input length."""
    preds = [0.05, 0.15, 0.35, 0.55, 0.75, 0.95]
    obs = [True, False, True, False, True, False]
    buckets = reliability_diagram_data(preds, obs, bin_count=10)
    assert sum(b.count for b in buckets) == len(preds)


def test_reliability_single_bucket_collapses_all_inputs() -> None:
    """With bin_count=1 every prediction lands in the only bucket."""
    preds = [0.1, 0.5, 0.9]
    obs = [True, False, True]
    buckets = reliability_diagram_data(preds, obs, bin_count=1)
    assert len(buckets) == 1
    assert buckets[0].count == 3


def test_reliability_predicted_mean_within_bucket_range() -> None:
    """Each non-empty bucket has predicted_mean within its [lower, upper] range."""
    preds = [0.05, 0.45, 0.85]
    obs = [True, False, True]
    for b in reliability_diagram_data(preds, obs, bin_count=10):
        if b.count == 0:
            continue
        assert b.lower <= b.predicted_mean <= b.upper + 1e-12


# ---------------------------------------------------------------------------
# CalibrationRecord validation
# ---------------------------------------------------------------------------


def test_record_rejects_nan_probability() -> None:
    """CalibrationRecord rejects NaN predicted_prob at construction."""
    with pytest.raises(CalibrationLogError, match="NaN"):
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="model_route",
            policy_path="p",
            predicted_prob=math.nan,
            observed_outcome=True,
        )


def test_record_rejects_inf_probability() -> None:
    """CalibrationRecord rejects infinite predicted_prob."""
    with pytest.raises(CalibrationLogError, match="finite"):
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=math.inf,
            observed_outcome=False,
        )


def test_record_rejects_probability_below_zero() -> None:
    """CalibrationRecord rejects probability below 0."""
    with pytest.raises(CalibrationLogError, match=r"outside \[0, 1\]"):
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=-0.0001,
            observed_outcome=True,
        )


def test_record_rejects_empty_decision_kind() -> None:
    """An empty decision_kind is rejected."""
    with pytest.raises(CalibrationLogError, match="decision_kind"):
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="",
            policy_path="p",
            predicted_prob=0.5,
            observed_outcome=True,
        )


def test_record_rejects_non_bool_outcome() -> None:
    """``observed_outcome`` must be a true ``bool``, not an ``int``."""
    with pytest.raises(CalibrationLogError, match="observed_outcome"):
        CalibrationRecord(  # type: ignore[arg-type]
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=0.5,
            observed_outcome=1,
        )


# ---------------------------------------------------------------------------
# Log I/O - round-trip and edge cases
# ---------------------------------------------------------------------------


def test_log_append_creates_parent_dirs(tmp_path: Path) -> None:
    """``log_decision`` creates the metrics directory if it is missing."""
    log = tmp_path / "deep" / "nested" / "calibration.jsonl"
    log_decision(
        decision_kind="model_route",
        policy_path="bandit/v1",
        predicted_prob=0.7,
        observed_outcome=True,
        log_path=log,
        timestamp=1_000.0,
    )
    assert log.exists()
    assert log.parent.exists()


def test_log_round_trip_preserves_values(tmp_path: Path) -> None:
    """A logged record reads back with identical values."""
    log = tmp_path / "calibration.jsonl"
    record = log_decision(
        decision_kind="judge",
        policy_path="judge/v2",
        predicted_prob=0.42,
        observed_outcome=False,
        decision_id="abc",
        metadata={"slice": "router"},
        log_path=log,
        timestamp=42.0,
    )
    rows = load_log(log)
    assert len(rows) == 1
    assert rows[0] == record


def test_log_missing_returns_empty(tmp_path: Path) -> None:
    """A missing log file returns an empty list, not an error."""
    assert load_log(tmp_path / "absent.jsonl") == []


def test_log_blank_lines_are_skipped(tmp_path: Path) -> None:
    """Blank lines inside the JSONL file are silently skipped."""
    log = tmp_path / "calibration.jsonl"
    log_decision(
        decision_kind="k",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=True,
        log_path=log,
        timestamp=1.0,
    )
    log.write_text(log.read_text() + "\n\n", encoding="utf-8")
    rows = load_log(log)
    assert len(rows) == 1


def test_log_rejects_invalid_json(tmp_path: Path) -> None:
    """Malformed JSON in the log raises ``CalibrationLogError``."""
    log = tmp_path / "calibration.jsonl"
    log.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(CalibrationLogError, match="invalid JSON"):
        load_log(log)


def test_log_rejects_non_object(tmp_path: Path) -> None:
    """Top-level non-object entries are rejected."""
    log = tmp_path / "calibration.jsonl"
    log.write_text(json.dumps([1, 2, 3]) + "\n", encoding="utf-8")
    with pytest.raises(CalibrationLogError, match="must be a JSON object"):
        load_log(log)


def test_log_rejects_missing_outcome(tmp_path: Path) -> None:
    """Records missing ``observed_outcome`` are flagged."""
    log = tmp_path / "calibration.jsonl"
    payload: dict[str, Any] = {
        "ts": 0.0,
        "decision_kind": "k",
        "policy_path": "p",
        "predicted_prob": 0.5,
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(CalibrationLogError, match="observed_outcome"):
        load_log(log)


def test_log_rejects_non_bool_outcome_in_json(tmp_path: Path) -> None:
    """Outcomes stored as ints are rejected - bool only."""
    log = tmp_path / "calibration.jsonl"
    payload = {
        "ts": 0.0,
        "decision_kind": "k",
        "policy_path": "p",
        "predicted_prob": 0.5,
        "observed_outcome": 1,
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(CalibrationLogError, match="observed_outcome"):
        load_log(log)


def test_log_filter_by_decision_kind(tmp_path: Path) -> None:
    """``decision_kind`` filter restricts to matching records."""
    log = tmp_path / "calibration.jsonl"
    log_decision(
        decision_kind="model_route",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=True,
        log_path=log,
        timestamp=1.0,
    )
    log_decision(
        decision_kind="judge",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=False,
        log_path=log,
        timestamp=2.0,
    )
    rows = load_log(log, decision_kind="judge")
    assert len(rows) == 1
    assert rows[0].decision_kind == "judge"


def test_log_filter_by_since_drops_old_records(tmp_path: Path) -> None:
    """``since_seconds`` filter excludes older records."""
    log = tmp_path / "calibration.jsonl"
    log_decision(
        decision_kind="k",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=True,
        log_path=log,
        timestamp=100.0,
    )
    log_decision(
        decision_kind="k",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=False,
        log_path=log,
        timestamp=200.0,
    )
    rows = load_log(log, since_seconds=50.0, now=210.0)
    assert len(rows) == 1
    assert rows[0].timestamp == 200.0


def test_log_default_path_constant_is_under_sdd_metrics() -> None:
    """The default log path lives under .sdd/metrics - operator contract."""
    assert Path(".sdd/metrics/calibration.jsonl") == DEFAULT_LOG_PATH


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def test_compute_report_empty_returns_nulls() -> None:
    """Empty records produce a report with null Brier and ECE - no crash."""
    report = compute_report([])
    assert report.decisions == 0
    assert report.brier is None
    assert report.ece is None
    assert report.buckets == ()


def test_compute_report_carries_filter_metadata() -> None:
    """``decision_kind`` and ``since`` are echoed on the report."""
    report = compute_report([], decision_kind="judge", since="7d")
    assert report.decision_kind == "judge"
    assert report.since == "7d"


def test_compute_report_with_records_sets_decisions() -> None:
    """A non-empty input yields the matching decisions count."""
    records = [
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=0.6,
            observed_outcome=True,
        ),
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=0.4,
            observed_outcome=False,
        ),
    ]
    report = compute_report(records)
    assert report.decisions == 2
    assert report.brier is not None and 0.0 <= report.brier <= 1.0
    assert report.ece is not None and 0.0 <= report.ece <= 1.0


def test_report_to_dict_is_json_serializable() -> None:
    """The report dict can be serialised to JSON and back losslessly."""
    records = [
        CalibrationRecord(
            timestamp=0.0,
            decision_kind="k",
            policy_path="p",
            predicted_prob=0.5,
            observed_outcome=True,
        ),
    ]
    report = compute_report(records, decision_kind="k", since="1h")
    blob = json.dumps(report.to_dict(), sort_keys=True)
    parsed = json.loads(blob)
    assert parsed["decisions"] == 1
    assert parsed["since"] == "1h"


def test_report_dataclass_is_frozen() -> None:
    """``CalibrationReport`` is immutable."""
    report = CalibrationReport(decisions=0, brier=None, ece=None, buckets=())
    with pytest.raises((AttributeError, TypeError)):
        report.decisions = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1s", 1.0),
        ("30s", 30.0),
        ("5m", 300.0),
        ("2h", 7_200.0),
        ("1d", 86_400.0),
        ("7d", 604_800.0),
        ("2w", 1_209_600.0),
        ("24H", 86_400.0),
        (" 3d ", 259_200.0),
    ],
)
def test_parse_duration_supported_forms(spec: str, expected: float) -> None:
    """All documented duration suffixes round-trip to seconds."""
    assert parse_duration(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "5y", "-1d", "1.5d", "d", "5"])
def test_parse_duration_rejects_invalid_forms(spec: str) -> None:
    """Empty, units-only, missing-unit, and unsupported suffixes raise."""
    with pytest.raises(ValueError):
        parse_duration(spec)


# ---------------------------------------------------------------------------
# Fixture-driven reference parity (issue acceptance criterion #1 & #2)
# ---------------------------------------------------------------------------


def test_hundred_sample_fixture_brier_matches_reference() -> None:
    """50-win / 50-loss synthetic fixture parity check."""
    # Construct a deterministic fixture: confidences increase linearly.
    preds = [(i + 0.5) / 100 for i in range(100)]
    # Outcomes: the second half always wins, the first half always loses.
    obs = [i >= 50 for i in range(100)]
    # Hand-rolled reference, matching the standard mean-squared-error formula.
    reference = sum((p - (1.0 if o else 0.0)) ** 2 for p, o in zip(preds, obs, strict=True)) / 100
    assert compute_brier(preds, obs).score == pytest.approx(reference, abs=1e-9)


def test_hundred_sample_fixture_ece_matches_reference() -> None:
    """ECE on the same fixture matches a hand-rolled 10-bin reference."""
    preds = [(i + 0.5) / 100 for i in range(100)]
    obs = [i >= 50 for i in range(100)]
    # Reference: each of 10 buckets holds 10 predictions; compute by hand.
    pred_sum = [0.0] * 10
    obs_sum = [0.0] * 10
    cnt = [0] * 10
    for p, o in zip(preds, obs, strict=True):
        idx = min(int(p * 10), 9)
        pred_sum[idx] += p
        obs_sum[idx] += 1.0 if o else 0.0
        cnt[idx] += 1
    total = sum(cnt)
    expected = sum((cnt[i] / total) * abs(pred_sum[i] / cnt[i] - obs_sum[i] / cnt[i]) for i in range(10) if cnt[i] > 0)
    assert expected_calibration_error(preds, obs) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Defensive parity / typing
# ---------------------------------------------------------------------------


def test_record_is_hashable_and_frozen() -> None:
    """CalibrationRecord is a frozen dataclass - hashable when metadata empty."""
    record = CalibrationRecord(
        timestamp=0.0,
        decision_kind="k",
        policy_path="p",
        predicted_prob=0.5,
        observed_outcome=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        record.predicted_prob = 0.0  # type: ignore[misc]


def test_brier_dataclass_is_frozen() -> None:
    """BrierScore is a frozen dataclass."""
    b = BrierScore(score=0.0, sample_count=0)
    with pytest.raises((AttributeError, TypeError)):
        b.score = 1.0  # type: ignore[misc]


def test_reliability_bucket_dataclass_is_frozen() -> None:
    """ReliabilityBucket is a frozen dataclass."""
    bucket = ReliabilityBucket(lower=0.0, upper=0.1, count=0, predicted_mean=0.0, observed_mean=0.0)
    with pytest.raises((AttributeError, TypeError)):
        bucket.count = 5  # type: ignore[misc]
