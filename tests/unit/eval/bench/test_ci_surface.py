"""
Tests for bernstein-bench CI surface (Issue #5458).

Acceptance criteria covered:
- AC-1: SARIF output validates against offline SARIF 2.1.0 schema:
        one result per failed case (fixture path, rule ID = control ID, expected vs observed message).
- AC-2: Check run renders scorecard table; failure on regression above threshold;
        neutral on missing or unverifiable baseline (never green).
- AC-3: Never compare against an unsigned or unverified baseline.
- AC-4: CLI `bernstein bench run --ci` integration with SARIF emission and baseline comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.ci import (
    BenchScorecard,
    generate_bench_sarif,
    validate_sarif_log,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.github_app.check_runs import CheckRunClient


@pytest.fixture()
def sample_suite() -> BenchSuite:
    return BenchSuite(
        version="sample-v1",
        tasks=[
            BenchTask(
                id="task_audit",
                description="Audit trail task",
                steps=("step 1",),
                assertions=({"kind": "audit_valid"},),
                category="audit",
            ),
            BenchTask(
                id="task_lineage",
                description="Data lineage task",
                steps=("step 1",),
                assertions=({"kind": "lineage_valid"},),
                category="lineage",
            ),
        ],
        controls=["CTRL-AUDIT-TRAIL", "CTRL-DATA-LINEAGE"],
    )


@pytest.fixture()
def adapter() -> MockReplayAdapter:
    return MockReplayAdapter()


def _create_bundle(
    suite: BenchSuite,
    adapter: MockReplayAdapter,
    task_passes: dict[str, bool] | None = None,
    signed: bool = True,
) -> SubmissionBundle:
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "default"})
    bundle = runner.run()
    if task_passes:
        modified_results: list[TaskResult] = []
        for tr in bundle.task_results:
            passed = task_passes.get(tr.task_id, tr.passed)
            modified_results.append(
                TaskResult(
                    task_id=tr.task_id,
                    task_hash=tr.task_hash,
                    receipt=tr.receipt if passed else None,  # empty receipt on failure
                    stored_receipt_hash=tr.stored_receipt_hash if passed else "",
                    passed=passed,
                    score=1.0 if passed else 0.0,
                    harness_output={"reason": "expected passed=True, observed passed=False"} if not passed else {},
                )
            )
        bundle = SubmissionBundle(
            suite_hash=bundle.suite_hash,
            suite_version=bundle.suite_version,
            task_results=modified_results,
            scheduler_config=bundle.scheduler_config,
            submitted_at=bundle.submitted_at,
        )
    if signed:
        bundle = StubSigner().sign(bundle)
    return bundle


# ===========================================================================
# AC-1: SARIF Schema Validation & Content
# ===========================================================================


class TestSarifGeneration:
    def test_sarif_validates_schema_on_failed_tasks(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-1: SARIF validates offline schema; 1 result per failed case with control ID & diff msg."""
        # task_lineage fails
        bundle = _create_bundle(sample_suite, adapter, task_passes={"task_lineage": False})
        sarif = generate_bench_sarif(sample_suite, bundle)

        # Validate schema structure
        assert validate_sarif_log(sarif) is True
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"]) == 1
        run = sarif["runs"][0]
        assert run["tool"]["driver"]["name"] == "bernstein-bench"

        # Exactly 1 result for the 1 failed task
        results = run["results"]
        assert len(results) == 1
        res = results[0]
        # Rule ID should map to a suite control or control registry ID
        assert res["ruleId"] in sample_suite.controls
        assert res["level"] == "error"
        assert "expected" in res["message"]["text"].lower() or "observed" in res["message"]["text"].lower()
        # Locations check
        loc = res["locations"][0]["physicalLocation"]
        assert "task_lineage" in loc["artifactLocation"]["uri"]

    def test_sarif_empty_results_on_all_passed(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-1: All passed tasks produce empty SARIF results with valid schema."""
        bundle = _create_bundle(sample_suite, adapter)
        sarif = generate_bench_sarif(sample_suite, bundle)
        assert validate_sarif_log(sarif) is True
        assert len(sarif["runs"][0]["results"]) == 0


# ===========================================================================
# AC-2 & AC-3: Scorecard Delta against Verified Baseline
# ===========================================================================


class TestScorecardDelta:
    def test_scorecard_delta_against_verified_baseline(
        self, sample_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """AC-2: Verified baseline yields exact delta and success conclusion."""
        baseline = _create_bundle(sample_suite, adapter)  # pass_rate = 1.0
        current = _create_bundle(sample_suite, adapter)  # pass_rate = 1.0

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=baseline,
            suite=sample_suite,
            adapter=adapter,
        )

        assert scorecard.baseline_verified is True
        assert scorecard.delta == 0.0
        assert scorecard.conclusion == "success"
        assert "+0.0%" in scorecard.delta_formatted

    def test_scorecard_fails_on_regression_above_threshold(
        self, sample_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """AC-2: Regression above threshold produces conclusion='failure'."""
        baseline = _create_bundle(sample_suite, adapter)  # pass_rate = 1.0 (2/2)
        current = _create_bundle(sample_suite, adapter, task_passes={"task_audit": False})  # pass_rate = 0.5 (1/2)

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=baseline,
            suite=sample_suite,
            adapter=adapter,
            threshold=0.0,
        )

        assert scorecard.baseline_verified is True
        assert scorecard.delta == -0.5
        assert scorecard.conclusion == "failure"
        assert "-50.0%" in scorecard.delta_formatted

    def test_scorecard_neutral_on_missing_baseline(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-2: Missing baseline yields neutral conclusion (never green)."""
        current = _create_bundle(sample_suite, adapter)

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=None,
            suite=sample_suite,
            adapter=adapter,
        )

        assert scorecard.baseline_verified is False
        assert scorecard.delta is None
        assert scorecard.conclusion == "neutral"
        assert "neutral" in scorecard.summary.lower()

    def test_scorecard_neutral_on_unsigned_baseline(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-3: Unsigned baseline is rejected -> neutral conclusion with note."""
        baseline = _create_bundle(sample_suite, adapter, signed=False)
        current = _create_bundle(sample_suite, adapter, signed=True)

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=baseline,
            suite=sample_suite,
            adapter=adapter,
        )

        assert scorecard.baseline_verified is False
        assert scorecard.conclusion == "neutral"
        assert "unverifiable" in scorecard.summary.lower() or "unsigned" in scorecard.summary.lower()

    def test_scorecard_neutral_on_tampered_baseline(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-3: Baseline with corrupted/divergent receipt fails verification -> neutral conclusion."""
        baseline = _create_bundle(sample_suite, adapter, signed=True)
        # Tamper with stored receipt hash
        tampered_results = list(baseline.task_results)
        tampered_results[0] = TaskResult(
            task_id=tampered_results[0].task_id,
            task_hash=tampered_results[0].task_hash,
            receipt=tampered_results[0].receipt,
            stored_receipt_hash="0000000000000000000000000000000000000000000000000000000000000000",
            passed=tampered_results[0].passed,
            score=tampered_results[0].score,
        )
        tampered_baseline = SubmissionBundle(
            suite_hash=baseline.suite_hash,
            suite_version=baseline.suite_version,
            task_results=tampered_results,
            scheduler_config=baseline.scheduler_config,
            submitted_at=baseline.submitted_at,
            signature=baseline.signature,
            signer_fingerprint=baseline.signer_fingerprint,
        )
        current = _create_bundle(sample_suite, adapter, signed=True)

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=tampered_baseline,
            suite=sample_suite,
            adapter=adapter,
        )

        assert scorecard.baseline_verified is False
        assert scorecard.conclusion == "neutral"

    def test_scorecard_renders_markdown_table(self, sample_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-2: Scorecard renders markdown table containing suite, pass rate, delta, bundle hash."""
        baseline = _create_bundle(sample_suite, adapter)
        current = _create_bundle(sample_suite, adapter)

        scorecard = BenchScorecard.compute(
            current_bundle=current,
            baseline_bundle=baseline,
            suite=sample_suite,
            adapter=adapter,
        )

        md = scorecard.to_markdown()
        assert sample_suite.version in md
        assert "Pass Rate" in md
        assert "Delta" in md
        assert current.bundle_hash()[:16] in md


# ===========================================================================
# CheckRunClient Integration
# ===========================================================================


class TestCheckRunScorecardIntegration:
    def test_create_bench_check_run(self) -> None:
        client = CheckRunClient(repo="sipyourdrink-ltd/bernstein")
        response_data = {"id": 888, "html_url": "https://github.com/checks/888"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = client.create_bench_check_run(
                head_sha="deadbeef1234",
                summary="Pass rate: 100.0%",
                scorecard_md="| Suite | Pass Rate |\n|---|---|\n| golden-v1 | 100% |",
                conclusion="success",
            )

        assert result is not None
        assert result.check_run_id == 888
        args = list(mock_run.call_args[0][0])
        assert "POST" in args


# ===========================================================================
# CLI Integration
# ===========================================================================


class TestCliCiOption:
    def test_bench_run_ci_produces_sarif_and_bundle(self, tmp_path: Path) -> None:
        out_bundle = tmp_path / "bundle.json"
        sarif_path = tmp_path / "bench.sarif"

        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(out_bundle),
                "--stub-signer",
                "--ci",
                "--sarif-out",
                str(sarif_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert out_bundle.exists()
        assert sarif_path.exists()
        sarif_data = json.loads(sarif_path.read_text(encoding="utf-8"))
        assert sarif_data["version"] == "2.1.0"
        assert "Scorecard" in result.output

    def test_bench_run_ci_with_baseline_comparison(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "baseline.json"
        out_bundle = tmp_path / "bundle.json"
        sarif_path = tmp_path / "bench.sarif"

        # Create baseline bundle
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        baseline_bundle = _create_bundle(suite, adapter, signed=True)
        baseline_bundle.save(baseline_path)

        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(out_bundle),
                "--stub-signer",
                "--ci",
                "--sarif-out",
                str(sarif_path),
                "--baseline",
                str(baseline_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Delta" in result.output or "+0.0%" in result.output
