"""Tests for SWE-Bench eval integration and metrics persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.cli.eval_benchmark_cmd import eval_group
from click.testing import CliRunner

from bernstein.benchmark.swe_bench import (
    InstanceResult,
    SWEBenchRunner,
    SWEInstance,
    compute_report,
    save_results,
)


def _instance(instance_id: str) -> SWEInstance:
    return SWEInstance(
        instance_id=instance_id,
        repo="demo/repo",
        base_commit="abc123",
        problem_statement="Fix failing test",
        hints_text="",
        test_patch="diff --git a/tests.py b/tests.py",
        patch="diff --git a/app.py b/app.py",
        fail_to_pass=["tests.test_demo"],
        pass_to_pass=["tests.test_keep"],
        environment_setup_commit="abc123",
        version="1.0",
        created_at="2024-01-01T00:00:00Z",
        repo_version="1.0",
    )


class TestReportAggregation:
    def test_compute_report_includes_per_task_and_per_model_metrics(self) -> None:
        report = compute_report(
            [
                InstanceResult("a", True, 0.12, 10.0, 1, 0, None, model_name="sonnet"),
                InstanceResult("b", False, 0.18, 20.0, 1, 1, "timeout", model_name="sonnet"),
                InstanceResult("c", True, 0.30, 40.0, 2, 0, None, model_name="opus"),
            ]
        )

        assert abs(report.resolve_rate - (2 / 3)) < 1e-9
        assert abs(report.cost_per_task - 0.2) < 1e-9
        assert abs(report.time_per_task - (70.0 / 3.0)) < 1e-9
        assert [entry.model_name for entry in report.per_model_breakdown] == ["opus", "sonnet"]
        sonnet = next(entry for entry in report.per_model_breakdown if entry.model_name == "sonnet")
        assert sonnet.total == 2
        assert sonnet.resolved == 1
        assert abs(sonnet.cost_per_task - 0.15) < 1e-9


class TestPersistence:
    def test_save_results_appends_metrics_jsonl_and_keeps_legacy_snapshot(self, tmp_path: Path) -> None:
        report = compute_report(
            [
                InstanceResult("a", True, 0.10, 30.0, 1, 0, None, model_name="sonnet"),
                InstanceResult("b", False, 0.05, 20.0, 1, 0, "failed", model_name="opus"),
            ]
        )

        snapshot_path = save_results(report, tmp_path)

        metrics_path = tmp_path / "metrics" / "swe_bench_results.jsonl"
        assert snapshot_path == tmp_path / "benchmark" / "swe_bench_results.json"
        assert snapshot_path.exists()
        assert metrics_path.exists()

        records = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 1
        assert abs(float(records[0]["cost_per_task"]) - 0.075) < 1e-9
        assert records[0]["per_model_breakdown"][0]["model_name"] == "opus"


class TestDatasetLoading:
    def test_load_dataset_uses_selected_subset_for_lazy_download(self) -> None:
        captured: dict[str, str] = {}

        def _fake_load_dataset(name: str, split: str) -> list[dict[str, object]]:
            captured["name"] = name
            captured["split"] = split
            return [
                {
                    "instance_id": "demo__repo-1",
                    "repo": "demo/repo",
                    "base_commit": "abc123",
                    "problem_statement": "Fix it",
                    "FAIL_TO_PASS": ["tests.test_demo"],
                    "PASS_TO_PASS": ["tests.test_keep"],
                }
            ]

        fake_module = SimpleNamespace(load_dataset=_fake_load_dataset)
        runner = SWEBenchRunner(workdir=Path("."), subset="lite")

        with patch.dict("sys.modules", {"datasets": fake_module}):
            instances = runner.load_dataset()

        assert captured == {"name": "princeton-nlp/SWE-bench_Lite", "split": "test"}
        assert [instance.instance_id for instance in instances] == ["demo__repo-1"]


class TestEvalCli:
    def test_eval_swe_bench_command_writes_metrics_output(self, tmp_path: Path) -> None:
        instances = [_instance("demo__repo-1"), _instance("demo__repo-2"), _instance("demo__repo-3")]
        results = [
            InstanceResult("demo__repo-1", True, 0.10, 10.0, 1, 0, None, model_name="sonnet"),
            InstanceResult("demo__repo-2", False, 0.20, 20.0, 1, 1, "timeout", model_name="sonnet"),
            InstanceResult("demo__repo-3", True, 0.30, 30.0, 2, 0, None, model_name="opus"),
        ]

        class FakeRunner:
            def __init__(
                self,
                workdir: Path,
                sample: int | None = None,
                instance_id: str | None = None,
                subset: str = "lite",
                seed: int = 42,
            ) -> None:
                self.workdir = workdir
                self.sample = sample
                self.instance_id = instance_id
                self.subset = subset
                self.seed = seed

            def load_dataset(self, dataset_path: Path | None = None) -> list[SWEInstance]:
                assert dataset_path is None
                return instances

            def run_instance(self, instance: SWEInstance) -> InstanceResult:
                return next(result for result in results if result.instance_id == instance.instance_id)

        runner = CliRunner()
        old_cwd = Path.cwd()

        try:
            os.chdir(tmp_path)
            with patch("bernstein.benchmark.swe_bench.SWEBenchRunner", FakeRunner):
                result = runner.invoke(eval_group, ["swe-bench", "--subset", "lite"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0, result.output
        assert "Resolve rate:" in result.output
        assert "Per-Model Breakdown" in result.output
        metrics_path = tmp_path / ".sdd" / "metrics" / "swe_bench_results.jsonl"
        assert metrics_path.exists()
