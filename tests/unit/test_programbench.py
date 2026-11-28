"""Unit tests for ProgramBench evaluation harness (TREND-1404)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.benchmark.programbench import (
    AdapterBreakdown,
    ProgramBenchHarness,
    ProgramBenchTask,
    TaskResult,
    classify_score,
    compute_report,
    compute_score,
    report_to_dict,
    save_results,
)


@pytest.fixture(autouse=True)
def isolated_benchmark_fixture(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Execute only the literal fixture programs in a test process."""
    import subprocess
    import sys

    from bernstein.benchmark import programbench

    def run_fixture(_image, argv, *, input_data, timeout):
        return subprocess.run(
            [sys.executable, *argv[1:]], input=input_data, capture_output=True, timeout=timeout, check=False
        )

    if "runtime_unavailable_fails_without_local_fallback" not in request.node.name:
        monkeypatch.setattr(programbench, "run_isolated", run_fixture)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "pb-001",
    *,
    setup: str = "x = 1",
    asserts: list[str] | None = None,
    hidden: list[str] | None = None,
    target: str = "def solve(): pass",
    subset: str = "lite",
) -> ProgramBenchTask:
    return ProgramBenchTask(
        task_id=task_id,
        subset=subset,
        description="demo",
        setup_code=setup,
        target_signature=target,
        asserts=asserts if asserts is not None else ["x == 1"],
        hidden_asserts=hidden if hidden is not None else [],
        tags=["unit"],
    )


def _result(
    task_id: str = "pb-001",
    *,
    score: float = 1.0,
    passed: int = 2,
    total: int = 2,
    adapter: str = "mock",
    cost: float = 0.01,
    duration: float = 1.0,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        score=score,
        asserts_passed=passed,
        asserts_total=total,
        fully_solved=score >= 1.0,
        cost_usd=cost,
        duration_seconds=duration,
        adapter=adapter,
    )


# ---------------------------------------------------------------------------
# Scoring (unit) - 6 tests
# ---------------------------------------------------------------------------


class TestComputeScore:
    def test_zero_total_returns_zero(self) -> None:
        assert compute_score(0, 0) == 0.0

    def test_negative_total_returns_zero(self) -> None:
        assert compute_score(2, -1) == 0.0

    def test_all_passing_returns_one(self) -> None:
        assert compute_score(5, 5) == 1.0

    def test_partial_credit(self) -> None:
        assert compute_score(3, 4) == 0.75

    def test_passed_exceeds_total_clamps_to_one(self) -> None:
        assert compute_score(7, 5) == 1.0

    def test_zero_passed_returns_zero(self) -> None:
        assert compute_score(0, 5) == 0.0


# ---------------------------------------------------------------------------
# Classification - 4 tests
# ---------------------------------------------------------------------------


class TestClassifyScore:
    def test_fully_solved_at_one(self) -> None:
        assert classify_score(1.0) == "fully_solved"

    def test_near_solved_at_threshold(self) -> None:
        assert classify_score(0.5) == "near_solved"

    def test_near_solved_below_one(self) -> None:
        assert classify_score(0.99) == "near_solved"

    def test_failed_below_threshold(self) -> None:
        assert classify_score(0.49) == "failed"


# ---------------------------------------------------------------------------
# Task parsing - 7 tests
# ---------------------------------------------------------------------------


class TestTaskParsing:
    def test_from_dict_minimal(self) -> None:
        task = ProgramBenchTask.from_dict({"task_id": "pb-1", "asserts": ["1 == 1"]})
        assert task.task_id == "pb-1"
        assert task.asserts == ["1 == 1"]
        assert task.hidden_asserts == []
        assert task.subset == "lite"

    def test_from_dict_full(self) -> None:
        task = ProgramBenchTask.from_dict(
            {
                "task_id": "pb-2",
                "subset": "hard",
                "description": "Build feature X",
                "setup_code": "y = 2",
                "target_signature": "def f(x): ...",
                "asserts": ["f(2) == 4"],
                "hidden_asserts": ["f(0) == 0"],
                "tags": ["math"],
            }
        )
        assert task.task_id == "pb-2"
        assert task.subset == "hard"
        assert task.target_signature == "def f(x): ..."
        assert task.hidden_asserts == ["f(0) == 0"]
        assert task.tags == ["math"]

    def test_from_dict_accepts_id_alias(self) -> None:
        task = ProgramBenchTask.from_dict({"id": "pb-3", "asserts": []})
        assert task.task_id == "pb-3"

    def test_from_dict_missing_task_id_raises(self) -> None:
        with pytest.raises(KeyError):
            ProgramBenchTask.from_dict({"asserts": []})

    def test_from_dict_empty_task_id_raises(self) -> None:
        with pytest.raises(KeyError):
            ProgramBenchTask.from_dict({"task_id": "", "asserts": []})

    def test_from_dict_json_encoded_asserts(self) -> None:
        task = ProgramBenchTask.from_dict({"task_id": "pb-4", "asserts": json.dumps(["a == 1", "b == 2"])})
        assert task.asserts == ["a == 1", "b == 2"]

    def test_from_dict_string_assert_falls_back_to_single_item(self) -> None:
        task = ProgramBenchTask.from_dict({"task_id": "pb-5", "asserts": "x == 1"})
        assert task.asserts == ["x == 1"]


# ---------------------------------------------------------------------------
# Report aggregation - 7 tests
# ---------------------------------------------------------------------------


class TestReportAggregation:
    def test_empty_results(self) -> None:
        report = compute_report([])
        assert report.total_tasks == 0
        assert report.mean_partial_credit == 0.0
        assert report.per_adapter_breakdown == []
        assert report.per_task == []

    def test_aggregates_partial_credit(self) -> None:
        results = [
            _result("a", score=1.0, passed=2, total=2),
            _result("b", score=0.5, passed=1, total=2),
            _result("c", score=0.0, passed=0, total=2),
        ]
        report = compute_report(results)
        assert report.total_tasks == 3
        assert report.fully_solved == 1
        assert report.near_solved == 1
        assert report.failed == 1
        assert math.isclose(report.mean_partial_credit, 0.5, abs_tol=1e-9)

    def test_per_adapter_breakdown_sorted(self) -> None:
        results = [
            _result("a", adapter="zeta"),
            _result("b", adapter="alpha"),
        ]
        report = compute_report(results)
        names = [b.adapter for b in report.per_adapter_breakdown]
        assert names == ["alpha", "zeta"]

    def test_per_adapter_counts_buckets(self) -> None:
        results = [
            _result("a", score=1.0, adapter="x"),
            _result("b", score=0.8, adapter="x"),
            _result("c", score=0.0, adapter="x"),
        ]
        report = compute_report(results)
        assert len(report.per_adapter_breakdown) == 1
        breakdown = report.per_adapter_breakdown[0]
        assert breakdown.fully_solved == 1
        assert breakdown.near_solved == 1
        assert breakdown.failed == 1

    def test_median_cost_and_duration(self) -> None:
        results = [
            _result("a", cost=0.10, duration=10.0),
            _result("b", cost=0.20, duration=20.0),
            _result("c", cost=0.40, duration=40.0),
        ]
        report = compute_report(results)
        assert report.median_cost_usd == 0.20
        assert report.median_duration_seconds == 20.0

    def test_total_cost_sums(self) -> None:
        results = [
            _result("a", cost=0.10),
            _result("b", cost=0.30),
        ]
        report = compute_report(results)
        assert math.isclose(report.total_cost_usd, 0.40, abs_tol=1e-9)

    def test_unknown_adapter_fallback(self) -> None:
        results = [_result("a", adapter="")]
        report = compute_report(results)
        assert report.per_adapter_breakdown[0].adapter == "unknown"


# ---------------------------------------------------------------------------
# Persistence - 4 tests
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_results_creates_metrics_and_snapshot(self, tmp_path: Path) -> None:
        report = compute_report([_result("a", score=1.0), _result("b", score=0.0)])
        snapshot = save_results(report, tmp_path)
        metrics = tmp_path / "metrics" / "programbench_results.jsonl"
        assert snapshot.exists()
        assert metrics.exists()
        record = json.loads(metrics.read_text(encoding="utf-8").splitlines()[0])
        assert record["total_tasks"] == 2
        assert record["fully_solved"] == 1

    def test_save_results_appends(self, tmp_path: Path) -> None:
        report = compute_report([_result("a")])
        save_results(report, tmp_path)
        save_results(report, tmp_path)
        metrics = tmp_path / "metrics" / "programbench_results.jsonl"
        assert len(metrics.read_text(encoding="utf-8").splitlines()) == 2

    def test_report_to_dict_round_trip(self) -> None:
        report = compute_report([_result("a", score=0.5, passed=1, total=2)])
        data = report_to_dict(report)
        assert data["total_tasks"] == 1
        assert data["per_task"][0]["task_id"] == "a"
        assert json.dumps(data, sort_keys=True)

    def test_snapshot_json_well_formed(self, tmp_path: Path) -> None:
        report = compute_report([_result("a")])
        snap = save_results(report, tmp_path)
        parsed = json.loads(snap.read_text(encoding="utf-8"))
        assert parsed["total_tasks"] == 1


# ---------------------------------------------------------------------------
# Harness state machine - 8 tests
# ---------------------------------------------------------------------------


class TestHarness:
    def test_build_goal_mentions_task(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task("pb-99", asserts=["a == 1"])
        goal = h.build_goal(task)
        assert "pb-99" in goal
        assert "a == 1" in goal

    def test_build_goal_handles_no_asserts(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(asserts=[])
        goal = h.build_goal(task)
        assert "(none)" in goal

    def test_filter_tasks_by_task_id(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path, task_id="pb-2")
        tasks = [_task("pb-1"), _task("pb-2"), _task("pb-3")]
        filtered = h.filter_tasks(tasks)
        assert [t.task_id for t in filtered] == ["pb-2"]

    def test_filter_tasks_sample(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path, sample=2, seed=42)
        tasks = [_task(f"pb-{i}") for i in range(10)]
        filtered = h.filter_tasks(tasks)
        assert len(filtered) == 2

    def test_filter_tasks_sample_deterministic(self, tmp_path: Path) -> None:
        tasks = [_task(f"pb-{i}") for i in range(10)]
        h1 = ProgramBenchHarness(workdir=tmp_path, sample=3, seed=42)
        h2 = ProgramBenchHarness(workdir=tmp_path, sample=3, seed=42)
        assert [t.task_id for t in h1.filter_tasks(tasks)] == [t.task_id for t in h2.filter_tasks(tasks)]

    def test_runtime_unavailable_fails_without_local_fallback(self, tmp_path: Path, monkeypatch) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _executable: None)
        harness = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="x = 5", asserts=["x == 5"])
        passed, total, error = harness.evaluate_asserts(task, "")
        assert (passed, total) == (0, 1)
        assert error is not None and "isolated runtime" in error

    def test_evaluate_asserts_all_pass(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="x = 5", asserts=["x == 5", "x > 0"])
        passed, total, err = h.evaluate_asserts(task, "")
        assert passed == 2
        assert total == 2
        assert err is None

    def test_evaluate_asserts_partial(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="x = 1", asserts=["x == 1", "x == 99"])
        passed, total, _ = h.evaluate_asserts(task, "")
        assert passed == 1
        assert total == 2

    def test_evaluate_asserts_candidate_overrides_state(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="x = 1", asserts=["x == 42"])
        passed, total, _ = h.evaluate_asserts(task, "x = 42")
        assert passed == 1
        assert total == 1


# ---------------------------------------------------------------------------
# Harness end-to-end with mocked adapter - 4 tests
# ---------------------------------------------------------------------------


class TestHarnessRunTask:
    def test_run_task_uses_invoke_adapter(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="x = 0", asserts=["x == 5"])

        def fake_invoke(adapter: str, t: ProgramBenchTask) -> tuple[str, float, float]:
            return "x = 5", 0.42, 1.5

        with patch.object(h, "_invoke_adapter", side_effect=fake_invoke):
            result = h.run_task("mock", task)

        assert result.score == 1.0
        assert result.fully_solved
        assert result.cost_usd == 0.42
        assert result.adapter == "mock"

    def test_run_task_records_partial(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task(setup="", asserts=["a == 1", "b == 2"])

        with patch.object(h, "_invoke_adapter", return_value=("a = 1", 0.1, 0.5)):
            result = h.run_task("mock", task)

        assert result.score == 0.5
        assert result.asserts_passed == 1
        assert result.asserts_total == 2
        assert not result.fully_solved

    def test_run_task_handles_adapter_error(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        task = _task()

        with patch.object(h, "_invoke_adapter", side_effect=RuntimeError("boom")):
            result = h.run_task("mock", task)

        assert result.score == 0.0
        assert result.error == "boom"
        assert result.duration_seconds == 0.0

    def test_run_aggregates_across_tasks(self, tmp_path: Path) -> None:
        h = ProgramBenchHarness(workdir=tmp_path)
        tasks = [
            _task("t1", setup="", asserts=["1 == 1"]),
            _task("t2", setup="", asserts=["1 == 2"]),
        ]

        with patch.object(h, "_invoke_adapter", return_value=("", 0.01, 0.5)):
            report = h.run("mock", tasks=tasks)

        assert report.total_tasks == 2
        assert report.fully_solved == 1
        assert report.failed == 1


# ---------------------------------------------------------------------------
# Dataset loader - 4 tests
# ---------------------------------------------------------------------------


class TestDatasetLoading:
    def test_load_dataset_from_file(self, tmp_path: Path) -> None:
        dataset = tmp_path / "data.jsonl"
        dataset.write_text(
            json.dumps({"task_id": "pb-1", "asserts": ["1 == 1"]}) + "\n",
            encoding="utf-8",
        )
        h = ProgramBenchHarness(workdir=tmp_path)
        tasks = h.load_dataset(dataset)
        assert [t.task_id for t in tasks] == ["pb-1"]

    def test_load_dataset_skips_invalid_lines(self, tmp_path: Path) -> None:
        dataset = tmp_path / "data.jsonl"
        dataset.write_text(
            "not-json\n"
            + json.dumps({"task_id": "pb-1", "asserts": []})
            + "\n"
            + json.dumps({"no_id_here": True})
            + "\n",
            encoding="utf-8",
        )
        h = ProgramBenchHarness(workdir=tmp_path)
        tasks = h.load_dataset(dataset)
        assert [t.task_id for t in tasks] == ["pb-1"]

    def test_load_dataset_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dataset = tmp_path / "env.jsonl"
        dataset.write_text(json.dumps({"task_id": "pb-env", "asserts": []}) + "\n", encoding="utf-8")
        monkeypatch.setenv("BERNSTEIN_PROGRAMBENCH_DATASET", str(dataset))
        h = ProgramBenchHarness(workdir=tmp_path)
        tasks = h.load_dataset()
        assert [t.task_id for t in tasks] == ["pb-env"]

    def test_load_dataset_huggingface_fallback(self, tmp_path: Path) -> None:
        captured: dict[str, str] = {}

        def fake_load(name: str, split: str) -> list[dict[str, Any]]:
            captured["name"] = name
            captured["split"] = split
            return [{"task_id": "pb-hf", "asserts": ["1 == 1"]}]

        fake_module = SimpleNamespace(load_dataset=fake_load)
        h = ProgramBenchHarness(workdir=tmp_path, subset="lite")
        with patch.dict("sys.modules", {"datasets": fake_module}):
            tasks = h.load_dataset()
        assert captured["name"] == "programbench/programbench-lite"
        assert captured["split"] == "test"
        assert [t.task_id for t in tasks] == ["pb-hf"]


# ---------------------------------------------------------------------------
# AdapterBreakdown serialisation - 1 test
# ---------------------------------------------------------------------------


class TestAdapterBreakdownSerialisation:
    def test_to_dict(self) -> None:
        b = AdapterBreakdown(
            adapter="x",
            total=2,
            fully_solved=1,
            near_solved=1,
            failed=0,
            mean_partial_credit=0.75,
            cost_per_task=0.1,
            time_per_task=1.0,
        )
        d = b.to_dict()
        assert d["adapter"] == "x"
        assert d["total"] == 2


# ---------------------------------------------------------------------------
# Property tests - 10
# ---------------------------------------------------------------------------


_PROP_SETTINGS = settings(max_examples=50, deadline=None)


class TestProperties:
    @_PROP_SETTINGS
    @given(passed=st.integers(min_value=0, max_value=1000), total=st.integers(min_value=1, max_value=1000))
    def test_score_in_unit_interval(self, passed: int, total: int) -> None:
        s = compute_score(passed, total)
        assert 0.0 <= s <= 1.0

    @_PROP_SETTINGS
    @given(total=st.integers(min_value=1, max_value=100))
    def test_score_passed_zero_is_zero(self, total: int) -> None:
        assert compute_score(0, total) == 0.0

    @_PROP_SETTINGS
    @given(n=st.integers(min_value=1, max_value=100))
    def test_score_all_pass_is_one(self, n: int) -> None:
        assert compute_score(n, n) == 1.0

    @_PROP_SETTINGS
    @given(passed=st.integers(min_value=1, max_value=100), total=st.integers(min_value=1, max_value=100))
    def test_score_monotonic_in_passed(self, passed: int, total: int) -> None:
        assume_passed = min(passed, total)
        less = compute_score(max(assume_passed - 1, 0), total)
        more = compute_score(assume_passed, total)
        assert less <= more

    @_PROP_SETTINGS
    @given(score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
    def test_classify_in_expected_bucket(self, score: float) -> None:
        bucket = classify_score(score)
        if score >= 1.0:
            assert bucket == "fully_solved"
        elif score >= 0.5:
            assert bucket == "near_solved"
        else:
            assert bucket == "failed"

    @_PROP_SETTINGS
    @given(
        scores=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=50,
        )
    )
    def test_report_totals_match_inputs(self, scores: list[float]) -> None:
        results = [_result(f"t{i}", score=s, passed=int(s * 2), total=2) for i, s in enumerate(scores)]
        report = compute_report(results)
        assert report.total_tasks == len(scores)
        assert report.fully_solved + report.near_solved + report.failed == len(scores)

    @_PROP_SETTINGS
    @given(
        scores=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=30,
        )
    )
    def test_mean_partial_credit_within_bounds(self, scores: list[float]) -> None:
        results = [_result(f"t{i}", score=s) for i, s in enumerate(scores)]
        report = compute_report(results)
        assert 0.0 <= report.mean_partial_credit <= 1.0

    @_PROP_SETTINGS
    @given(
        costs=st.lists(
            st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=20,
        )
    )
    def test_total_cost_matches_sum(self, costs: list[float]) -> None:
        results = [_result(f"t{i}", cost=c) for i, c in enumerate(costs)]
        report = compute_report(results)
        assert math.isclose(report.total_cost_usd, sum(costs), rel_tol=1e-9, abs_tol=1e-9)

    @_PROP_SETTINGS
    @given(
        task_ids=st.lists(
            st.text(
                alphabet=st.characters(min_codepoint=97, max_codepoint=122),
                min_size=1,
                max_size=10,
            ),
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    def test_per_task_preserves_ids(self, task_ids: list[str]) -> None:
        results = [_result(tid) for tid in task_ids]
        report = compute_report(results)
        assert [r.task_id for r in report.per_task] == task_ids

    @_PROP_SETTINGS
    @given(
        passed=st.integers(min_value=0, max_value=20),
        total=st.integers(min_value=1, max_value=20),
    )
    def test_score_equals_ratio_when_in_range(self, passed: int, total: int) -> None:
        score = compute_score(passed, total)
        if passed <= total and passed >= 0:
            assert math.isclose(score, passed / total, abs_tol=1e-9)
        else:
            assert score in (0.0, 1.0)
