"""A bench bundle only verifies when it covers the suite and its scores replay.

``bench verify`` used to answer MATCH for a bundle with no tasks, with a cherry-picked subset, with
one passing task repeated, and with a stored score the replay did not produce -- the verifier
iterated whatever ``task_results`` held and compared only ``passed``. The CLI also scored every
built-in suite without a production adapter through ``MockReplayAdapter`` (always pass, 1.0) and
signed that with the install identity.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus


class _ReceiptVerdictAdapter:
    """Hermetic adapter whose verdict is a pure function of the receipt: ``ok`` decides it."""

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        return {"journal_head": "j", "spine_head": "s", "run_id": task.id, "ok": task.id != "task_fail"}

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        ok = bool(receipt.get("ok"))
        return ok, 1.0 if ok else 0.0, {}


def _suite() -> BenchSuite:
    return BenchSuite(
        version="attest-v1",
        tasks=[
            BenchTask(id=name, description=name, steps=("s",), assertions=({"kind": "syntax_valid"},), category="c")
            for name in ("task_pass_a", "task_pass_b", "task_fail")
        ],
    )


@pytest.fixture
def suite() -> BenchSuite:
    return _suite()


@pytest.fixture
def honest(suite: BenchSuite) -> SubmissionBundle:
    return BenchRunner(suite=suite, adapter=_ReceiptVerdictAdapter(), scheduler_config={"scheduler": "x"}).run()


def _verify(suite: BenchSuite, bundle: SubmissionBundle, adapter: Any = None):
    resigned = StubSigner().sign(dataclasses.replace(bundle, signature="", signer_fingerprint=""))
    verifier = BenchVerifier(suite=suite, adapter=adapter or _ReceiptVerdictAdapter(), allow_stub_signature=True)
    return verifier.verify(resigned)


def _with_results(bundle: SubmissionBundle, results: list[TaskResult]) -> SubmissionBundle:
    # A fresh instance, so the cached bundle hash is recomputed over the new task list.
    return SubmissionBundle(
        suite_hash=bundle.suite_hash,
        suite_version=bundle.suite_version,
        task_results=results,
        scheduler_config=bundle.scheduler_config,
        submitted_at=bundle.submitted_at,
    )


class TestCoverage:
    def test_the_honest_bundle_matches(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        assert _verify(suite, honest).status == VerificationStatus.MATCH

    def test_an_empty_bundle_is_rejected(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        result = _verify(suite, _with_results(honest, []))
        assert result.status == VerificationStatus.COVERAGE_MISMATCH
        assert not result.passed

    def test_a_subset_that_drops_the_failing_task_is_rejected(
        self, suite: BenchSuite, honest: SubmissionBundle
    ) -> None:
        cherry_picked = _with_results(honest, [r for r in honest.task_results if r.task_id != "task_fail"])
        assert cherry_picked.pass_rate == 1.0, "the subset reads as a perfect run"
        result = _verify(suite, cherry_picked)
        assert result.status == VerificationStatus.COVERAGE_MISMATCH
        assert "task_fail" in result.detail

    def test_a_duplicated_task_is_rejected(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        first = honest.task_results[0]
        result = _verify(suite, _with_results(honest, [first, first, first]))
        assert result.status == VerificationStatus.COVERAGE_MISMATCH
        assert first.task_id in result.detail

    def test_a_full_set_with_one_task_repeated_is_rejected(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        padded = _with_results(honest, [*honest.task_results, honest.task_results[0]])
        result = _verify(suite, padded)
        assert result.status == VerificationStatus.COVERAGE_MISMATCH
        assert "duplicate" in result.detail.lower()

    def test_a_task_the_suite_does_not_have_is_rejected(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        stranger = dataclasses.replace(honest.task_results[0], task_id="not_in_suite")
        result = _verify(suite, _with_results(honest, [*honest.task_results, stranger]))
        assert not result.passed
        assert "not_in_suite" in result.report()


class TestScoreReplay:
    def test_an_inflated_stored_score_is_fabricated(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        inflated = [dataclasses.replace(honest.task_results[0], score=5.0), *honest.task_results[1:]]
        result = _verify(suite, _with_results(honest, inflated))
        assert result.status == VerificationStatus.DIVERGED
        assert result.task_results[0].status == VerificationStatus.FABRICATED_SCORE
        assert "score" in result.task_results[0].detail

    def test_a_partial_credit_the_replay_did_not_give_is_fabricated(
        self, suite: BenchSuite, honest: SubmissionBundle
    ) -> None:
        failing = next(r for r in honest.task_results if r.task_id == "task_fail")
        bumped = [dataclasses.replace(r, score=0.4) if r is failing else r for r in honest.task_results]
        result = _verify(suite, _with_results(honest, bumped))
        assert not result.passed


class TestAttestedFields:
    def test_a_tampered_harness_fingerprint_is_rejected(self, suite: BenchSuite, honest: SubmissionBundle) -> None:
        forged = dataclasses.replace(honest, harness_fingerprint="0" * 64)
        assert _verify(suite, forged).status == VerificationStatus.HASH_MISMATCH

    def test_lambda_value_is_part_of_the_bundle_hash(self, honest: SubmissionBundle) -> None:
        other = dataclasses.replace(honest, lambda_value=9.9)
        other._bundle_hash = None
        assert other.bundle_hash() != honest.bundle_hash()

    def test_a_tampered_lambda_value_fails_to_load(self, honest: SubmissionBundle) -> None:
        raw = honest.to_dict()
        raw["lambda_value"] = 9.9
        with pytest.raises(ValueError, match="hash mismatch"):
            SubmissionBundle.from_dict(raw)

    def test_the_default_lambda_keeps_the_hash_of_a_bundle_written_before_the_field_was_hashed(
        self, honest: SubmissionBundle
    ) -> None:
        raw = honest.to_dict()
        raw.pop("lambda_value")  # an older writer that never emitted it
        assert SubmissionBundle.from_dict(raw).lambda_value == 0.5


class TestPlaceholderMetricsAreUnavailable:
    def test_rates_that_need_abstention_data_are_not_numbers(self, honest: SubmissionBundle) -> None:
        out = honest.to_dict()
        assert out["abstain_rate"] is None
        assert out["confident_error_rate"] is None
        assert out["resolve_rate"] is None
        assert out["pass_rate"] == pytest.approx(2 / 3)

    def test_expected_value_uses_the_bundles_own_lambda(self, honest: SubmissionBundle) -> None:
        # 2 passed, 1 wrong, lambda 0.5 -> (2 - 0.5) / 3
        assert honest.expected_value() == pytest.approx(0.5)


class TestMockAdapterReceipts:
    def test_a_garbage_receipt_does_not_replay_as_a_pass(self) -> None:
        suite = _suite()
        adapter = MockReplayAdapter()
        honest = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()
        first = honest.task_results[0]
        garbage = TaskResult(
            task_id=first.task_id,
            task_hash=first.task_hash,
            receipt={"garbage": 1},
            passed=True,
            score=1.0,
        )
        result = _verify(suite, _with_results(honest, [garbage, *honest.task_results[1:]]), adapter)
        assert not result.passed


class TestCli:
    def _run(self, *args: str):
        return CliRunner().invoke(bench_group, list(args))

    def test_the_install_identity_will_not_sign_mock_scores(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.json"
        result = self._run("run", "golden-v1", "--out", str(out))
        assert result.exit_code != 0
        assert "no production adapter" in result.output.lower()
        assert not out.exists()

    def test_the_install_identity_will_not_sign_a_mock_reliability_receipt(self, tmp_path: Path) -> None:
        out = tmp_path / "rel.json"
        result = self._run("run", "golden-v1", "--reliability", "2", "--out", str(out))
        assert result.exit_code != 0
        assert not out.exists()

    def test_a_stub_signed_mock_run_says_so_in_the_bundle(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.json"
        result = self._run("run", "golden-v1", "--out", str(out), "--stub-signer")
        assert result.exit_code == 0, result.output
        assert "MOCK" in result.output
        assert SubmissionBundle.load(out).scheduler_config["adapter"] == "mock"

    def test_verify_of_a_mock_scored_suite_says_the_replay_is_synthetic(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.json"
        self._run("run", "golden-v1", "--out", str(out), "--stub-signer")
        result = self._run("verify", str(out), "--stub-signer")
        assert "MOCK" in result.output

    def test_verify_reports_a_tampered_bundle_instead_of_crashing(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.json"
        self._run("run", "golden-v1", "--out", str(out), "--stub-signer")
        raw = json.loads(out.read_text())
        raw["task_results"][0]["score"] = 5.0
        out.write_text(json.dumps(raw))
        result = self._run("verify", str(out), "--stub-signer")
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "hash mismatch" in result.output.lower()

    def test_compare_ranks_each_bundle_by_its_own_lambda_value(self, tmp_path: Path) -> None:
        def bundle(name: str, passed: int, total: int, lam: float) -> Path:
            results = [
                TaskResult(
                    task_id=f"t{i}",
                    task_hash=f"h{i}",
                    receipt={"i": i},
                    passed=i < passed,
                    score=1.0 if i < passed else 0.0,
                )
                for i in range(total)
            ]
            b = SubmissionBundle(
                suite_hash="s",
                suite_version="v",
                task_results=results,
                scheduler_config={"scheduler": "x"},
                lambda_value=lam,
                submitted_at=1.0,
            )
            path = tmp_path / name
            b.save(path)
            return path

        # With a fixed lambda of 1.0 (the old behaviour) b would lead: 0.2 vs 0.0.
        a = bundle("a.json", passed=5, total=10, lam=0.0)  # EV 0.5
        b = bundle("b.json", passed=6, total=10, lam=2.0)  # EV 6*... = (6 - 2*4)/10 = -0.2
        result = self._run("compare", str(a), str(b))
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if ln[:2] in ("1.", "2.")]
        assert lines[0].startswith("1. a.json")
        assert "lambda" in result.output.lower()
        assert "not verified" in result.output.lower()
