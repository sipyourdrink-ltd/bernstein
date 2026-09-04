"""
TDD tests for bench cost per verdict in bundles, bundle comparison, and budget gating (Issue #5464).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.compare import compare_bundles
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask


class CostMockReplayAdapter(MockReplayAdapter):
    """Hermetic adapter that returns custom cost and token metrics in receipts."""

    def __init__(
        self,
        cost_per_task: dict[str, float] | None = None,
        tokens_per_task: dict[str, int] | None = None,
        pass_map: dict[str, bool] | None = None,
    ):
        self.cost_per_task = cost_per_task or {}
        self.tokens_per_task = tokens_per_task or {}
        self.pass_map = pass_map or {}

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        receipt = super().run_task(task, scheduler_config)
        receipt["cost_usd"] = self.cost_per_task.get(task.id, 0.01)
        receipt["token_count"] = self.tokens_per_task.get(task.id, 500)
        receipt["duration_seconds"] = 1.5
        return receipt

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        passed = self.pass_map.get(task.id, True)
        return passed, 1.0 if passed else 0.0, {"note": "cost mock"}


@pytest.fixture()
def three_task_suite() -> BenchSuite:
    return BenchSuite(
        version="cost-test-v1",
        tasks=[
            BenchTask(id="task_1", description="Task 1", steps=("s1",), assertions=()),
            BenchTask(id="task_2", description="Task 2", steps=("s2",), assertions=()),
            BenchTask(id="task_3", description="Task 3", steps=("s3",), assertions=()),
        ],
    )


class TestBundleCostMetrics:
    def test_task_result_cost_fields(self) -> None:
        tr = TaskResult(
            task_id="t1",
            task_hash="hash1",
            receipt={"journal_head": "j", "spine_head": "s"},
            passed=True,
            score=1.0,
            duration_seconds=2.5,
            token_count=1200,
            cost_usd=0.035,
        )
        d = tr.to_dict()
        assert d["duration_seconds"] == 2.5
        assert d["token_count"] == 1200
        assert d["cost_usd"] == 0.035

    def test_submission_bundle_totals_and_verdict_breakdown(self) -> None:
        tr1 = TaskResult(
            task_id="t1",
            task_hash="h1",
            receipt={"r": 1},
            passed=True,
            score=1.0,
            duration_seconds=2.0,
            token_count=1000,
            cost_usd=0.02,
        )
        tr2 = TaskResult(
            task_id="t2",
            task_hash="h2",
            receipt={"r": 2},
            passed=False,
            score=0.0,
            duration_seconds=3.0,
            token_count=1500,
            cost_usd=0.03,
        )
        bundle = SubmissionBundle(
            suite_hash="shash",
            suite_version="sv",
            task_results=[tr1, tr2],
            scheduler_config={},
        )
        assert bundle.schema_version == 2
        assert bundle.total_cost_usd == pytest.approx(0.05)
        assert bundle.total_tokens == 2500
        assert bundle.total_duration_seconds == pytest.approx(5.0)

        cv = bundle.cost_per_verdict
        assert cv["passed"] == pytest.approx(0.02)
        assert cv["failed"] == pytest.approx(0.03)

        tv = bundle.tokens_per_verdict
        assert tv["passed"] == 1000
        assert tv["failed"] == 1500

    def test_bundle_serialization_round_trip(self) -> None:
        tr1 = TaskResult(
            task_id="t1",
            task_hash="h1",
            receipt={"r": 1},
            passed=True,
            score=1.0,
            duration_seconds=1.5,
            token_count=500,
            cost_usd=0.015,
        )
        bundle = SubmissionBundle(
            suite_hash="shash",
            suite_version="sv",
            task_results=[tr1],
            scheduler_config={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            loaded = SubmissionBundle.load(path)

        assert loaded.schema_version == 2
        assert loaded.total_cost_usd == pytest.approx(0.015)
        assert loaded.total_tokens == 500
        assert loaded.total_duration_seconds == pytest.approx(1.5)
        assert loaded.task_results[0].cost_usd == pytest.approx(0.015)
        assert loaded.bundle_hash() == bundle.bundle_hash()

    def test_backwards_compatibility_v1_bundle(self) -> None:
        raw_v1 = {
            "suite_hash": "shash",
            "suite_version": "sv1",
            "submitted_at": 100000.0,
            "scheduler_config": {},
            "task_results": [
                {
                    "task_id": "t1",
                    "task_hash": "h1",
                    "receipt": {"j": 1},
                    "receipt_hash": TaskResult._compute_receipt_hash({"j": 1}),
                    "passed": True,
                    "score": 1.0,
                    "harness_output": {},
                }
            ],
            "signature": "",
            "signer_fingerprint": "",
        }
        import hashlib

        payload = json.dumps(
            {
                "suite_hash": raw_v1["suite_hash"],
                "suite_version": raw_v1["suite_version"],
                "submitted_at": raw_v1["submitted_at"],
                "scheduler_config": raw_v1["scheduler_config"],
                "task_results": [
                    {
                        "task_id": "t1",
                        "task_hash": "h1",
                        "receipt": {"j": 1},
                        "receipt_hash": TaskResult._compute_receipt_hash({"j": 1}),
                        "passed": True,
                        "score": 1.0,
                        "harness_output": {},
                        "duration_seconds": 0.0,
                        "token_count": 0,
                        "cost_usd": 0.0,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        raw_v1["bundle_hash"] = hashlib.sha256(payload).hexdigest()

        loaded = SubmissionBundle.from_dict(raw_v1)
        assert loaded.total_cost_usd == 0.0
        assert loaded.total_tokens == 0
        assert loaded.total_duration_seconds == 0.0
        assert loaded.task_results[0].cost_usd == 0.0


class TestBudgetGate:
    def test_runner_populates_cost_and_tokens(self, three_task_suite: BenchSuite) -> None:
        adapter = CostMockReplayAdapter(
            cost_per_task={"task_1": 0.01, "task_2": 0.02, "task_3": 0.03},
            tokens_per_task={"task_1": 100, "task_2": 200, "task_3": 300},
        )
        runner = BenchRunner(suite=three_task_suite, adapter=adapter, scheduler_config={})
        bundle = runner.run()

        assert len(bundle.task_results) == 3
        assert bundle.total_cost_usd == pytest.approx(0.06)
        assert bundle.total_tokens == 600
        assert not bundle.budget_exceeded

    def test_budget_exceeded_halts_runner(self, three_task_suite: BenchSuite) -> None:
        adapter = CostMockReplayAdapter(
            cost_per_task={"task_1": 0.02, "task_2": 0.02, "task_3": 0.02},
            tokens_per_task={"task_1": 200, "task_2": 200, "task_3": 200},
        )
        # Set budget to 0.03 (task 1 costs 0.02, after task 2 total is 0.04 >= 0.03 -> stops before task 3)
        runner = BenchRunner(suite=three_task_suite, adapter=adapter, scheduler_config={}, budget_usd=0.03)
        bundle = runner.run()

        assert len(bundle.task_results) == 2
        assert bundle.total_cost_usd == pytest.approx(0.04)
        assert bundle.budget_usd == 0.03
        assert bundle.budget_exceeded is True


class TestBundleComparison:
    def test_compare_bundles(self) -> None:
        tr_a1 = TaskResult("t1", "h1", {"r": 1}, True, 1.0, duration_seconds=2.0, token_count=1000, cost_usd=0.02)
        tr_a2 = TaskResult("t2", "h2", {"r": 2}, False, 0.0, duration_seconds=3.0, token_count=1500, cost_usd=0.03)
        bundle_a = SubmissionBundle("shash", "sv1", [tr_a1, tr_a2], {})

        tr_b1 = TaskResult("t1", "h1", {"r": 1}, True, 1.0, duration_seconds=1.5, token_count=800, cost_usd=0.015)
        tr_b2 = TaskResult("t2", "h2", {"r": 2}, True, 1.0, duration_seconds=2.0, token_count=1000, cost_usd=0.02)
        bundle_b = SubmissionBundle("shash", "sv1", [tr_b1, tr_b2], {})

        cmp = compare_bundles(bundle_a, bundle_b)
        assert cmp.score_delta == pytest.approx(0.5)
        assert cmp.pass_rate_delta == pytest.approx(0.5)
        assert cmp.cost_delta == pytest.approx(-0.015)
        assert cmp.token_delta == -700
        assert cmp.duration_delta == pytest.approx(-1.5)

        report = cmp.report()
        assert "Overall Score" in report
        assert "Total Cost" in report
        assert "Total Tokens" in report
        assert "PASS -> PASS" in report
        assert "FAIL -> PASS" in report


class TestCLIWithBudgetAndCompare:
    def test_cli_run_with_budget(self, tmp_path: Path) -> None:
        out = tmp_path / "budget_bundle.json"
        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(out), "--stub-signer", "--budget", "100.0"],
        )
        assert result.exit_code == 0, result.output
        assert "Cost" in result.output
        assert out.exists()

    def test_cli_compare(self, tmp_path: Path, three_task_suite: BenchSuite) -> None:
        adapter = CostMockReplayAdapter(
            cost_per_task={"task_1": 0.01, "task_2": 0.02, "task_3": 0.03},
            tokens_per_task={"task_1": 100, "task_2": 200, "task_3": 300},
        )
        runner_a = BenchRunner(suite=three_task_suite, adapter=adapter, scheduler_config={})
        bundle_a = StubSigner().sign(runner_a.run())
        path_a = tmp_path / "bundle_a.json"
        bundle_a.save(path_a)

        adapter_b = CostMockReplayAdapter(
            cost_per_task={"task_1": 0.008, "task_2": 0.015, "task_3": 0.02},
            tokens_per_task={"task_1": 80, "task_2": 150, "task_3": 200},
        )
        runner_b = BenchRunner(suite=three_task_suite, adapter=adapter_b, scheduler_config={})
        bundle_b = StubSigner().sign(runner_b.run())
        path_b = tmp_path / "bundle_b.json"
        bundle_b.save(path_b)

        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["compare", str(path_a), str(path_b)],
        )
        assert result.exit_code == 0, result.output
        assert "Overall Score" in result.output
        assert "Total Cost" in result.output
        assert "Total Tokens" in result.output
