"""Test SubmissionBundle stores lambda_value and computes three rates correctly.

Acceptance criterion: test_bundle_records_lambda_and_the_three_rates
Verify that SubmissionBundle stores lambda_value and computes resolve_rate,
abstain_rate, confident_error_rate correctly from task results.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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
            lambda_value=0.9,
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


class TestBundleResolveRate:
    """SubmissionBundle computes resolve_rate correctly from task results."""

    def test_resolve_rate_all_passed(self) -> None:
        """All tasks passed = 100% resolve rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", True),
                _make_task_result("t3", True),
            ],
            scheduler_config={},
        )
        assert bundle.resolve_rate == 1.0

    def test_resolve_rate_half_passed(self) -> None:
        """Half tasks passed = 50% resolve rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
        )
        assert bundle.resolve_rate == 0.5

    def test_resolve_rate_none_passed(self) -> None:
        """No tasks passed = 0% resolve rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", False),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
        )
        assert bundle.resolve_rate == 0.0

    def test_resolve_rate_empty_bundle(self) -> None:
        """Empty bundle has 0% resolve rate (no division by zero)."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[],
            scheduler_config={},
        )
        assert bundle.resolve_rate == 0.0


class TestBundleAbstainRate:
    """SubmissionBundle computes abstain_rate correctly from task results."""

    def test_abstain_rate_default_zero(self) -> None:
        """Without explicit abstention tracking, abstain_rate is 0."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
        )
        assert bundle.abstain_rate == 0.0

    def test_abstain_rate_empty_bundle(self) -> None:
        """Empty bundle has 0% abstain rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[],
            scheduler_config={},
        )
        assert bundle.abstain_rate == 0.0


class TestBundleConfidentErrorRate:
    """SubmissionBundle computes confident_error_rate correctly from task results."""

    def test_confident_error_rate_all_passed(self) -> None:
        """All tasks passed = 0% confident error rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", True),
            ],
            scheduler_config={},
        )
        assert bundle.confident_error_rate == 0.0

    def test_confident_error_rate_half_failed(self) -> None:
        """Half tasks failed = 50% confident error rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", True),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
        )
        assert bundle.confident_error_rate == 0.5

    def test_confident_error_rate_all_failed(self) -> None:
        """All tasks failed = 100% confident error rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[
                _make_task_result("t1", False),
                _make_task_result("t2", False),
            ],
            scheduler_config={},
        )
        assert bundle.confident_error_rate == 1.0

    def test_confident_error_rate_empty_bundle(self) -> None:
        """Empty bundle has 0% confident error rate."""
        bundle = SubmissionBundle(
            suite_hash="test-suite",
            suite_version="v1",
            task_results=[],
            scheduler_config={},
        )
        assert bundle.confident_error_rate == 0.0


class TestBundleRatesInSerialization:
    """Rates appear in to_dict output."""

    def test_to_dict_includes_all_rates(self) -> None:
        """to_dict must include resolve_rate, abstain_rate, confident_error_rate."""
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
        assert "lambda_value" in d
        assert "resolve_rate" in d
        assert "abstain_rate" in d
        assert "confident_error_rate" in d
        assert d["lambda_value"] == 0.6
        assert d["resolve_rate"] == 0.5
        assert d["abstain_rate"] == 0.0
        assert d["confident_error_rate"] == 0.5
