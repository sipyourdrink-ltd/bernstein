"""Unit tests for benchmark CI surface, SARIF output, and scorecard deltas (#5458).

Acceptance Criteria:
1. SARIF validates (offline schema in tests, one result per failed case).
2. Check run renders the table; failure on regression; neutral on a missing or unverifiable baseline.
3. Docs page updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.ci import evaluate_ci_scorecard, post_bench_check_run
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.sarif import bundle_to_sarif
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier
from bernstein.github_app.check_runs import CheckRunClient


def _make_test_bundle(*, suite_hash: str = "suite123", tasks: list[dict]) -> SubmissionBundle:
    results = [
        TaskResult(
            task_id=t["id"],
            task_hash=f"hash_{t['id']}",
            receipt={"journal_head": "jh", "spine_head": "sh", "run_id": f"r_{t['id']}"},
            passed=t.get("passed", True),
            score=t.get("score", 1.0),
            harness_output=t.get("harness_output", {}),
        )
        for t in tasks
    ]
    return SubmissionBundle(
        suite_hash=suite_hash,
        suite_version="golden-v1",
        task_results=results,
        scheduler_config={"scheduler": "default"},
    )


class TestSarifGeneration:
    """Test generating SARIF 2.1.0 reports for benchmark runs."""

    def test_sarif_structure_on_passing_bundle(self) -> None:
        bundle = _make_test_bundle(tasks=[{"id": "t1", "passed": True, "score": 1.0}])
        sarif = bundle_to_sarif(bundle)

        assert sarif["version"] == "2.1.0"
        assert "$schema" in sarif
        assert len(sarif["runs"]) == 1
        run = sarif["runs"][0]
        assert run["tool"]["driver"]["name"] == "bernstein-bench"
        assert len(run["results"]) == 0

    def test_sarif_contains_failed_task_details(self) -> None:
        bundle = _make_test_bundle(
            tasks=[
                {"id": "task_ok", "passed": True, "score": 1.0},
                {
                    "id": "task_bad",
                    "passed": False,
                    "score": 0.0,
                    "harness_output": {"error": "assertion_failed: file missing"},
                },
            ]
        )
        sarif = bundle_to_sarif(bundle)
        run = sarif["runs"][0]
        assert len(run["results"]) == 1
        res = run["results"][0]
        assert res["ruleId"] == "task_bad"
        assert res["level"] == "error"
        assert "assertion_failed" in res["message"]["text"] or "task_bad" in res["message"]["text"]
        assert len(res["locations"]) > 0


class TestScorecardEvaluation:
    """Test scorecard calculation, baseline comparison, and conclusions."""

    def test_scorecard_with_no_baseline_is_neutral(self) -> None:
        suite = build_golden_suite_v1()
        bundle = _make_test_bundle(suite_hash=suite.suite_hash, tasks=[{"id": suite.tasks[0].id, "passed": True}])
        scorecard = evaluate_ci_scorecard(bundle=bundle, suite=suite, baseline_bundle=None)

        assert scorecard.conclusion == "neutral"
        assert scorecard.baseline_pass_rate is None
        assert "baseline" in scorecard.summary.lower()
        md = scorecard.to_markdown()
        assert bundle.suite_version in md
        assert bundle.bundle_hash()[:12] in md

    def test_scorecard_with_unverifiable_baseline_is_neutral(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        curr_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()

        # Create tampered baseline bundle
        bad_task = TaskResult(
            task_id=suite.tasks[0].id,
            task_hash=suite.tasks[0].content_hash(),
            receipt={"journal_head": "tampered", "spine_head": "bad"},
            passed=True,
            score=1.0,
            stored_receipt_hash="fake_hash",
        )
        bad_baseline = SubmissionBundle(
            suite_hash=suite.suite_hash,
            suite_version=suite.version,
            task_results=[bad_task],
            scheduler_config={},
        )

        scorecard = evaluate_ci_scorecard(
            bundle=curr_bundle,
            suite=suite,
            baseline_bundle=bad_baseline,
            verifier=verifier,
        )

        assert scorecard.conclusion == "neutral"
        assert "unverifiable" in scorecard.summary.lower()

    def test_scorecard_with_verified_baseline_success(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        baseline_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()
        curr_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()

        scorecard = evaluate_ci_scorecard(
            bundle=curr_bundle,
            suite=suite,
            baseline_bundle=baseline_bundle,
            verifier=verifier,
            regression_threshold=0.0,
        )

        assert scorecard.conclusion == "success"
        assert scorecard.pass_rate_delta == pytest.approx(0.0)
        assert scorecard.baseline_pass_rate == pytest.approx(1.0)
        md = scorecard.to_markdown()
        assert "PASS" in md or "✓" in md

    def test_scorecard_fails_on_regression(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[
                BenchTask(id="t1", description="T1", steps=(), assertions=()),
                BenchTask(id="t2", description="T2", steps=(), assertions=()),
            ],
        )

        baseline = _make_test_bundle(
            suite_hash=suite.suite_hash,
            tasks=[{"id": "t1", "passed": True, "score": 1.0}, {"id": "t2", "passed": True, "score": 1.0}],
        )
        # Regression: t2 fails (50% pass rate vs 100% baseline)
        curr = _make_test_bundle(
            suite_hash=suite.suite_hash,
            tasks=[{"id": "t1", "passed": True, "score": 1.0}, {"id": "t2", "passed": False, "score": 0.0}],
        )

        scorecard = evaluate_ci_scorecard(
            bundle=curr,
            suite=suite,
            baseline_bundle=baseline,
            verifier=None,
            regression_threshold=0.05,
        )

        assert scorecard.conclusion == "failure"
        assert scorecard.pass_rate_delta == pytest.approx(-0.5)
        assert "regression" in scorecard.summary.lower()


class TestCheckRunPosting:
    """Test creating check runs with scorecard summaries."""

    def test_post_bench_check_run(self) -> None:
        suite = build_golden_suite_v1()
        bundle = _make_test_bundle(suite_hash=suite.suite_hash, tasks=[{"id": suite.tasks[0].id, "passed": True}])
        scorecard = evaluate_ci_scorecard(bundle=bundle, suite=suite, baseline_bundle=None)

        client = CheckRunClient(repo="owner/repo")
        with patch.object(client, "create_bench_check_run") as mock_create:
            mock_create.return_value = MagicMock(check_run_id=123)
            res = post_bench_check_run(scorecard=scorecard, client=client, head_sha="abc1234")

            assert res is not None
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            assert kwargs.get("conclusion") == "neutral" or (len(args) > 2 and args[2] == "neutral")


class TestCLI_CI_Integration:
    """Test CLI --ci and --sarif-out options."""

    def test_cli_run_ci_mode(self, tmp_path: Path) -> None:
        out_bundle = tmp_path / "bundle.json"
        sarif_out = tmp_path / "report.sarif"

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
                str(sarif_out),
            ],
        )

        assert result.exit_code == 0, result.output
        assert out_bundle.exists()
        assert sarif_out.exists()
        sarif_data = json.loads(sarif_out.read_text(encoding="utf-8"))
        assert sarif_data["version"] == "2.1.0"
