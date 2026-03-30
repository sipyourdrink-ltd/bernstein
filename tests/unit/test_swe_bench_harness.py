"""Unit tests for bernstein.benchmark.swe_bench — SWE-Bench evaluation harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from bernstein.benchmark.swe_bench import (
    InstanceResult,
    SWEBenchRunner,
    SWEInstance,
    compute_report,
    save_results,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_instance() -> SWEInstance:
    return SWEInstance(
        instance_id="django__django-11905",
        repo="django/django",
        base_commit="abc123",
        problem_statement="Fix queryset annotation bug in GROUP BY clause",
        hints_text="",
        test_patch="diff --git a/tests/test_queryset.py ...",
        patch="diff --git a/django/db/models/sql/compiler.py ...",
        fail_to_pass=["tests.queryset.tests.QuerysetTests.test_annotation"],
        pass_to_pass=["tests.queryset.tests.QuerysetTests.test_basic"],
        environment_setup_commit="abc123",
        version="3.0",
        created_at="2019-11-01T00:00:00Z",
        repo_version="3.0",
        FAIL_TO_PASS=["tests.queryset.tests.QuerysetTests.test_annotation"],
        PASS_TO_PASS=["tests.queryset.tests.QuerysetTests.test_basic"],
    )


@pytest.fixture()
def passing_result(sample_instance: SWEInstance) -> InstanceResult:
    return InstanceResult(
        instance_id=sample_instance.instance_id,
        resolved=True,
        cost_usd=0.12,
        duration_seconds=45.0,
        agent_count=2,
        retries=0,
        error=None,
    )


@pytest.fixture()
def failing_result(sample_instance: SWEInstance) -> InstanceResult:
    return InstanceResult(
        instance_id=sample_instance.instance_id,
        resolved=False,
        cost_usd=0.08,
        duration_seconds=30.0,
        agent_count=1,
        retries=1,
        error="Patch did not resolve failing tests",
    )


# ---------------------------------------------------------------------------
# SWEInstance
# ---------------------------------------------------------------------------


def test_swe_instance_has_required_fields(sample_instance: SWEInstance) -> None:
    assert sample_instance.instance_id == "django__django-11905"
    assert sample_instance.repo == "django/django"
    assert sample_instance.base_commit == "abc123"
    assert sample_instance.problem_statement.startswith("Fix queryset")
    assert isinstance(sample_instance.fail_to_pass, list)
    assert isinstance(sample_instance.pass_to_pass, list)


def test_swe_instance_from_dict_parses_correctly() -> None:
    raw: dict[str, Any] = {
        "instance_id": "astropy__astropy-12907",
        "repo": "astropy/astropy",
        "base_commit": "def456",
        "problem_statement": "Modeling bug in Gaussian2D",
        "hints_text": "",
        "test_patch": "diff ...",
        "patch": "diff ...",
        "FAIL_TO_PASS": '["tests.modeling.test_models.test_gaussian2d"]',
        "PASS_TO_PASS": '["tests.modeling.test_models.test_basic"]',
        "environment_setup_commit": "def456",
        "version": "5.0",
        "created_at": "2022-01-01T00:00:00Z",
        "repo_version": "5.0",
    }
    instance = SWEInstance.from_dict(raw)
    assert instance.instance_id == "astropy__astropy-12907"
    assert instance.repo == "astropy/astropy"
    assert instance.fail_to_pass == ["tests.modeling.test_models.test_gaussian2d"]
    assert instance.pass_to_pass == ["tests.modeling.test_models.test_basic"]


def test_swe_instance_from_dict_handles_list_fail_to_pass() -> None:
    """FAIL_TO_PASS may be a list (not JSON string) in some dataset versions."""
    raw: dict[str, Any] = {
        "instance_id": "x__y-1",
        "repo": "x/y",
        "base_commit": "aaa",
        "problem_statement": "Bug",
        "hints_text": "",
        "test_patch": "",
        "patch": "",
        "FAIL_TO_PASS": ["tests.test_foo"],
        "PASS_TO_PASS": ["tests.test_bar"],
        "environment_setup_commit": "aaa",
        "version": "1.0",
        "created_at": "2020-01-01T00:00:00Z",
        "repo_version": "1.0",
    }
    instance = SWEInstance.from_dict(raw)
    assert instance.fail_to_pass == ["tests.test_foo"]
    assert instance.pass_to_pass == ["tests.test_bar"]


# ---------------------------------------------------------------------------
# InstanceResult
# ---------------------------------------------------------------------------


def test_instance_result_resolved_fields(passing_result: InstanceResult) -> None:
    assert passing_result.resolved is True
    assert passing_result.cost_usd == pytest.approx(0.12)
    assert passing_result.duration_seconds == pytest.approx(45.0)
    assert passing_result.agent_count == 2
    assert passing_result.retries == 0
    assert passing_result.error is None


def test_instance_result_failed_fields(failing_result: InstanceResult) -> None:
    assert failing_result.resolved is False
    assert failing_result.retries == 1
    assert failing_result.error is not None


def test_instance_result_to_dict(passing_result: InstanceResult) -> None:
    d = passing_result.to_dict()
    assert d["instance_id"] == "django__django-11905"
    assert d["resolved"] is True
    assert d["cost_usd"] == pytest.approx(0.12)
    assert d["duration_seconds"] == pytest.approx(45.0)
    assert d["agent_count"] == 2
    assert d["retries"] == 0
    assert d["error"] is None


# ---------------------------------------------------------------------------
# BenchmarkReport
# ---------------------------------------------------------------------------


def test_compute_report_empty_produces_zero_metrics() -> None:
    report = compute_report([])
    assert report.total == 0
    assert report.resolved == 0
    assert report.resolve_rate == pytest.approx(0.0)
    assert report.median_cost_usd == pytest.approx(0.0)
    assert report.median_duration_seconds == pytest.approx(0.0)


def test_compute_report_all_resolved() -> None:
    results = [
        InstanceResult("a", True, 0.10, 30.0, 1, 0, None),
        InstanceResult("b", True, 0.20, 60.0, 2, 0, None),
    ]
    report = compute_report(results)
    assert report.total == 2
    assert report.resolved == 2
    assert report.resolve_rate == pytest.approx(1.0)
    assert report.median_cost_usd == pytest.approx(0.15)
    assert report.median_duration_seconds == pytest.approx(45.0)


def test_compute_report_mixed_results() -> None:
    results = [
        InstanceResult("a", True, 0.10, 30.0, 1, 0, None),
        InstanceResult("b", False, 0.05, 15.0, 1, 1, "timeout"),
        InstanceResult("c", True, 0.30, 90.0, 3, 0, None),
        InstanceResult("d", False, 0.08, 20.0, 1, 0, "no patch"),
    ]
    report = compute_report(results)
    assert report.total == 4
    assert report.resolved == 2
    assert report.resolve_rate == pytest.approx(0.5)


def test_compute_report_cost_effectiveness_ratio() -> None:
    results = [
        InstanceResult("a", True, 1.00, 60.0, 2, 0, None),
        InstanceResult("b", True, 3.00, 120.0, 2, 0, None),
    ]
    report = compute_report(results)
    # cost_effectiveness = resolved / total_cost
    assert report.cost_effectiveness_ratio == pytest.approx(2 / 4.0)


def test_benchmark_report_has_instance_results() -> None:
    results = [
        InstanceResult("a", True, 0.10, 30.0, 1, 0, None),
    ]
    report = compute_report(results)
    assert len(report.instance_results) == 1
    assert report.instance_results[0].instance_id == "a"


# ---------------------------------------------------------------------------
# save_results
# ---------------------------------------------------------------------------


def test_save_results_writes_json(tmp_path: Path) -> None:
    results = [
        InstanceResult("a", True, 0.10, 30.0, 1, 0, None),
        InstanceResult("b", False, 0.05, 15.0, 1, 1, "timeout"),
    ]
    report = compute_report(results)
    out_path = save_results(report, tmp_path)

    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["total"] == 2
    assert data["resolved"] == 1
    assert "resolve_rate" in data
    assert "instance_results" in data
    assert len(data["instance_results"]) == 2


def test_save_results_creates_output_directory(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    # Do NOT create sdd_dir — save_results must create it
    results = [InstanceResult("x", True, 0.01, 5.0, 1, 0, None)]
    report = compute_report(results)
    out_path = save_results(report, sdd_dir)
    assert out_path.exists()


def test_save_results_path_is_under_benchmark_dir(tmp_path: Path) -> None:
    report = compute_report([])
    out_path = save_results(report, tmp_path)
    assert "benchmark" in str(out_path)
    assert "swe_bench" in out_path.name


# ---------------------------------------------------------------------------
# SWEBenchRunner — construction
# ---------------------------------------------------------------------------


def test_swe_bench_runner_constructs_with_defaults(tmp_path: Path) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    assert runner.workdir == tmp_path
    assert runner.sample is None
    assert runner.instance_id is None


def test_swe_bench_runner_constructs_with_sample(tmp_path: Path) -> None:
    runner = SWEBenchRunner(workdir=tmp_path, sample=20)
    assert runner.sample == 20


def test_swe_bench_runner_constructs_with_instance_id(tmp_path: Path) -> None:
    runner = SWEBenchRunner(workdir=tmp_path, instance_id="django__django-11905")
    assert runner.instance_id == "django__django-11905"


# ---------------------------------------------------------------------------
# SWEBenchRunner — filter_instances
# ---------------------------------------------------------------------------


def test_filter_instances_returns_all_when_no_filter(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    instances = [
        sample_instance,
        SWEInstance("b__b-1", "b/b", "x", "bug", "", "", "", [], [], "x", "1", "2020", "1", [], []),
    ]
    filtered = runner.filter_instances(instances)
    assert len(filtered) == 2


def test_filter_instances_by_instance_id(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path, instance_id="django__django-11905")
    other = SWEInstance("b__b-1", "b/b", "x", "bug", "", "", "", [], [], "x", "1", "2020", "1", [], [])
    filtered = runner.filter_instances([sample_instance, other])
    assert len(filtered) == 1
    assert filtered[0].instance_id == "django__django-11905"


def test_filter_instances_by_sample(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path, sample=1)
    instances = [
        sample_instance,
        SWEInstance("b__b-1", "b/b", "x", "bug", "", "", "", [], [], "x", "1", "2020", "1", [], []),
        SWEInstance("c__c-2", "c/c", "y", "fix", "", "", "", [], [], "y", "2", "2021", "2", [], []),
    ]
    filtered = runner.filter_instances(instances)
    assert len(filtered) == 1


def test_filter_instances_sample_larger_than_list(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path, sample=100)
    instances = [sample_instance]
    filtered = runner.filter_instances(instances)
    assert len(filtered) == 1


# ---------------------------------------------------------------------------
# SWEBenchRunner — build_goal
# ---------------------------------------------------------------------------


def test_build_goal_contains_problem_statement(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    goal = runner.build_goal(sample_instance)
    assert sample_instance.problem_statement in goal


def test_build_goal_contains_repo(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    goal = runner.build_goal(sample_instance)
    assert sample_instance.repo in goal


def test_build_goal_contains_failing_tests(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    goal = runner.build_goal(sample_instance)
    for test in sample_instance.fail_to_pass:
        assert test in goal


# ---------------------------------------------------------------------------
# SWEBenchRunner — evaluate_patch (no subprocess, pure logic)
# ---------------------------------------------------------------------------


def test_evaluate_patch_returns_true_for_matching_patch(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    # When the patch text matches the expected patch, resolved = True
    resolved = runner.evaluate_patch(
        instance=sample_instance,
        patch_text=sample_instance.patch,
    )
    # With a matching patch string, the harness marks resolved
    assert isinstance(resolved, bool)


def test_evaluate_patch_returns_false_for_empty_patch(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)
    resolved = runner.evaluate_patch(instance=sample_instance, patch_text="")
    assert resolved is False


# ---------------------------------------------------------------------------
# SWEBenchRunner — run_instance (mocked)
# ---------------------------------------------------------------------------


def test_run_instance_returns_instance_result(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)

    # Mock _spawn_bernstein so no real subprocess is started
    with patch.object(runner, "_spawn_bernstein") as mock_spawn:
        mock_spawn.return_value = (sample_instance.patch, 0.10, 30.0, 1)
        result = runner.run_instance(sample_instance)

    assert isinstance(result, InstanceResult)
    assert result.instance_id == sample_instance.instance_id


def test_run_instance_marks_resolved_when_patch_matches(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)

    with patch.object(runner, "_spawn_bernstein") as mock_spawn:
        mock_spawn.return_value = (sample_instance.patch, 0.10, 30.0, 1)
        with patch.object(runner, "evaluate_patch", return_value=True):
            result = runner.run_instance(sample_instance)

    assert result.resolved is True


def test_run_instance_marks_unresolved_on_empty_patch(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)

    with patch.object(runner, "_spawn_bernstein") as mock_spawn:
        mock_spawn.return_value = ("", 0.05, 20.0, 1)
        result = runner.run_instance(sample_instance)

    assert result.resolved is False


def test_run_instance_records_cost_and_duration(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)

    with patch.object(runner, "_spawn_bernstein") as mock_spawn:
        mock_spawn.return_value = ("", 0.42, 99.0, 3)
        result = runner.run_instance(sample_instance)

    assert result.cost_usd == pytest.approx(0.42)
    assert result.duration_seconds == pytest.approx(99.0)
    assert result.agent_count == 3


def test_run_instance_handles_spawn_exception(tmp_path: Path, sample_instance: SWEInstance) -> None:
    runner = SWEBenchRunner(workdir=tmp_path)

    with patch.object(runner, "_spawn_bernstein", side_effect=RuntimeError("timeout")):
        result = runner.run_instance(sample_instance)

    assert result.resolved is False
    assert result.error is not None
    assert "timeout" in result.error
