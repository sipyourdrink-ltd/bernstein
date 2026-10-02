"""Test SubmissionBundle stores lambda_value and does not invent the three rates.

Acceptance criterion: test_bundle_records_lambda_and_the_three_rates
Verify that SubmissionBundle stores lambda_value, ranks with it, and reports the
rates it has no data for as unavailable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult


def _make_task_result(task_id: str, passed: bool, score: float = 1.0) -> TaskResult:
    """Helper to create a TaskResult with minimal boilerplate."""
    return TaskResult(
        task_id=task_id,
        task_hash=f"hash-{task_id}",
        receipt={"journal_head": "a" * 64, "spine_head": "b" * 64},
        passed=passed,
        score=score,
    )


class TestBundleLambdaValue:
    """SubmissionBundle stores lambda_value correctly."""

    def test_bundle_has_default_lambda_value(self) -> None:
        """Default lambda_value should be 0.5."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result("t1", True)],
            scheduler_config={},
        )
        assert bundle.lambda_value == 0.5

    def test_bundle_accepts_custom_lambda_value(self) -> None:
        """Custom lambda_value should be stored."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result("t1", True)],
            scheduler_config={},
            lambda_value=0.75,
        )
        assert bundle.lambda_value == 0.75

    def test_lambda_value_persists_through_save_load(self) -> None:
        """lambda_value should survive serialization round-trip."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result("t1", True)],
            scheduler_config={},
            lambda_value=0.33,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            loaded = SubmissionBundle.load(path)
        assert loaded.lambda_value == 0.33

    def test_lambda_value_defaults_on_load_for_old_bundles(self) -> None:
        """Old bundles without lambda_value should default to 0.5."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result("t1", True)],
            scheduler_config={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            # Manually remove lambda_value from the JSON to simulate old bundle
            import json

            raw = json.loads(path.read_text())
            del raw["lambda_value"]
            path.write_text(json.dumps(raw))
            loaded = SubmissionBundle.load(path)
        assert loaded.lambda_value == 0.5


class TestRatesThatNeedAbstentionDataAreUnavailable:
    """A task result records no abstention, so resolve / abstain / confident-error are not numbers.

    They used to be derived from pass/fail alone -- abstain_rate a constant 0.0, confident_error_rate
    every failure, resolve_rate pass_rate -- and written to the bundle as measurements.
    """

    def _bundle(self, *passed: bool) -> SubmissionBundle:
        return SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result(f"t{i}", p) for i, p in enumerate(passed)],
            scheduler_config={},
        )

    @pytest.mark.parametrize("results", [(), (True, True), (True, False), (False,)])
    @pytest.mark.parametrize("rate", ["resolve_rate", "abstain_rate", "confident_error_rate"])
    def test_rate_is_none_whatever_the_results(self, rate: str, results: tuple[bool, ...]) -> None:
        assert getattr(self._bundle(*results), rate) is None

    def test_pass_rate_is_still_measured(self) -> None:
        assert self._bundle(True, False).pass_rate == 0.5


class TestExpectedValue:
    """expected_value = (passed - lambda * wrong) / total, with the bundle's own lambda_value."""

    def _bundle(self, lam: float, *passed: bool) -> SubmissionBundle:
        return SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[_make_task_result(f"t{i}", p) for i, p in enumerate(passed)],
            scheduler_config={},
            lambda_value=lam,
        )

    def test_default_lambda(self) -> None:
        assert self._bundle(0.5, True, False).expected_value() == 0.25

    def test_lambda_changes_the_value(self) -> None:
        assert self._bundle(2.0, True, False).expected_value() == -0.5

    def test_empty_bundle_is_zero(self) -> None:
        assert self._bundle(0.5).expected_value() == 0.0


class TestBundleRatesInSerialization:
    """Unavailable rates are serialised as null, not as numbers."""

    def test_to_dict_reports_unavailable_rates_as_null(self) -> None:
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
            lambda_value=0.6,
        )
        d = bundle.to_dict()
        assert d["lambda_value"] == 0.6
        assert d["pass_rate"] == 0.5
        assert d["resolve_rate"] is None
        assert d["abstain_rate"] is None
        assert d["confident_error_rate"] is None
