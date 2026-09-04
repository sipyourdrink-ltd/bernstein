"""
Unit tests for benchmark bundle cost accounting, comparison, and budget gate (#5464).

Acceptance Criteria:
1. Per-task tokens, cost and wall time in TaskResult and SubmissionBundle with schema version.
2. bernstein bench compare reports cost deltas next to pass-rate deltas.
3. --budget stops the run when exceeded and writes a refusal receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.compare import compare_bundles
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.suite import BenchSuite, BenchTask


def _make_sample_bundle(
    *,
    version: str = "golden-v1",
    task_specs: list[dict],
    scheduler_cfg: dict | None = None,
) -> SubmissionBundle:
    task_results = [
        TaskResult(
            task_id=ts["id"],
            task_hash=f"hash_{ts['id']}",
            receipt={"journal_head": "jhead", "spine_head": "shead"},
            passed=ts.get("passed", True),
            score=ts.get("score", 1.0),
            tokens=ts.get("tokens", 100),
            cost_usd=ts.get("cost_usd", 0.01),
            duration_seconds=ts.get("duration_seconds", 1.5),
        )
        for ts in task_specs
    ]
    return SubmissionBundle(
        suite_hash="suite_hash_123",
        suite_version=version,
        task_results=task_results,
        scheduler_config=scheduler_cfg or {"scheduler": "default"},
    )


class TestBundleCostAccounting:
    """Test cost, token, and duration fields in bundle schema."""

    def test_task_result_fields(self) -> None:
        tr = TaskResult(
            task_id="t1",
            task_hash="thash1",
            receipt={"run_id": "r1"},
            passed=True,
            score=1.0,
            tokens=250,
            cost_usd=0.005,
            duration_seconds=2.1,
        )
        assert tr.tokens == 250
        assert tr.cost_usd == 0.005
        assert tr.duration_seconds == 2.1

        d = tr.to_dict()
        assert d["tokens"] == 250
        assert d["cost_usd"] == 0.005
        assert d["duration_seconds"] == 2.1

    def test_submission_bundle_aggregated_cost_metrics(self) -> None:
        bundle = _make_sample_bundle(
            task_specs=[
                {
                    "id": "task_1",
                    "passed": True,
                    "score": 1.0,
                    "tokens": 100,
                    "cost_usd": 0.02,
                    "duration_seconds": 1.0,
                },
                {
                    "id": "task_2",
                    "passed": False,
                    "score": 0.0,
                    "tokens": 200,
                    "cost_usd": 0.03,
                    "duration_seconds": 2.0,
                },
            ]
        )

        assert bundle.total_tokens == 300
        assert bundle.total_cost_usd == pytest.approx(0.05)
        assert bundle.total_duration_seconds == pytest.approx(3.0)
        assert bundle.bundle_schema_version >= 2

        d = bundle.to_dict()
        assert d["total_tokens"] == 300
        assert d["total_cost_usd"] == pytest.approx(0.05)
        assert d["total_duration_seconds"] == pytest.approx(3.0)
        assert d["bundle_schema_version"] >= 2

    def test_bundle_save_load_round_trip_preserves_cost_and_hash(self, tmp_path: Path) -> None:
        bundle = _make_sample_bundle(
            task_specs=[
                {
                    "id": "task_1",
                    "passed": True,
                    "score": 1.0,
                    "tokens": 150,
                    "cost_usd": 0.015,
                    "duration_seconds": 1.2,
                },
            ]
        )
        p = tmp_path / "bundle.json"
        bundle.save(p)

        loaded = SubmissionBundle.load(p)
        assert loaded.bundle_hash() == bundle.bundle_hash()
        assert loaded.total_tokens == 150
        assert loaded.total_cost_usd == pytest.approx(0.015)
        assert loaded.task_results[0].tokens == 150
        assert loaded.task_results[0].cost_usd == pytest.approx(0.015)


class TestBundleComparison:
    """Test comparing two bundles for score, pass-rate, and cost deltas."""

    def test_compare_bundles_metrics(self) -> None:
        bundle_a = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 100, "cost_usd": 0.04, "duration_seconds": 2.0},
                {"id": "t2", "passed": False, "score": 0.0, "tokens": 100, "cost_usd": 0.04, "duration_seconds": 2.0},
            ]
        )
        bundle_b = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.02, "duration_seconds": 1.0},
                {"id": "t2", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.02, "duration_seconds": 1.0},
            ]
        )

        cmp = compare_bundles(bundle_a, bundle_b)
        assert cmp.pass_rate_delta == pytest.approx(0.5)  # 50% -> 100%
        assert cmp.score_delta == pytest.approx(0.5)
        assert cmp.cost_delta_usd == pytest.approx(-0.04)  # $0.08 -> $0.04
        assert cmp.cost_delta_percent == pytest.approx(-50.0)
        assert cmp.tokens_delta == -40
        assert cmp.duration_delta_seconds == pytest.approx(-2.0)

        md = cmp.to_markdown()
        assert "Cost" in md
        assert "$0.08" in md
        assert "$0.04" in md

    def test_cli_compare_command(self, tmp_path: Path) -> None:
        b1 = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 100, "cost_usd": 0.05, "duration_seconds": 1.0}
            ]
        )
        b2 = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.03, "duration_seconds": 0.8}
            ]
        )

        p1 = tmp_path / "b1.json"
        p2 = tmp_path / "b2.json"
        b1.save(p1)
        b2.save(p2)

        runner = CliRunner()
        result = runner.invoke(bench_group, ["compare", str(p1), str(p2)])
        assert result.exit_code == 0, result.output
        assert "Cost" in result.output


class TestBudgetGate:
    """Test budget limit enforcement and refusal receipt emission."""

    def test_runner_budget_gate_stops_early(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[
                BenchTask(id="t1", description="Task 1", steps=(), assertions=()),
                BenchTask(id="t2", description="Task 2", steps=(), assertions=()),
                BenchTask(id="t3", description="Task 3", steps=(), assertions=()),
            ],
        )

        class CostingAdapter(MockReplayAdapter):
            def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
                res = super().run_task(task, scheduler_config)
                res["cost_usd"] = 0.02
                res["tokens"] = 100
                res["duration_seconds"] = 0.5
                return res

        # Budget of $0.03 stops after task 2
        runner = BenchRunner(
            suite=suite,
            adapter=CostingAdapter(),
            scheduler_config={},
            budget_usd=0.03,
        )
        bundle = runner.run()

        assert len(bundle.task_results) == 3
        # Task 1 & 2 ran, Task 3 was refused / stopped due to budget
        assert bundle.task_results[0].passed is True
        assert bundle.task_results[1].passed is True
        assert bundle.task_results[2].passed is False
        assert "budget_exceeded" in bundle.task_results[2].receipt.get("refusal_reason", "")

    def test_cli_run_with_budget(self, tmp_path: Path) -> None:
        out = tmp_path / "budget_bundle.json"
        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(out), "--budget", "0.001", "--stub-signer"],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
