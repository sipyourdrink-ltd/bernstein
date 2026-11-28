"""Tests for control-to-suite benchmark coverage and latest measurement resolution (#5455, #7098)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.compliance.control_coverage import (
    RecordedMeasurement,
    build_control_coverage,
    select_latest_measurements,
)
from bernstein.compliance.controls import Control, ControlRegistry, get_default_registry
from bernstein.eval.bench.bench_cli import _get_suite, builtin_suite_control_map
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.suite import BenchSuite, BenchTask

if TYPE_CHECKING:
    from pathlib import Path


def _make_bundle(
    *,
    suite_version: str,
    submitted_at: float,
    passed_flags: list[bool],
    suite_hash: str | None = None,
    suite: BenchSuite | None = None,
    adapter: str = "fixture",
    mismatched_task: bool = False,
    refused: bool = False,
    override_score: float | None = None,
) -> SubmissionBundle:
    """Construct a valid, hash-checked SubmissionBundle fixture."""
    results: list[TaskResult] = []
    suite = suite or _get_suite(suite_version)
    for idx, task in enumerate(suite.tasks):
        passed = idx < len(passed_flags) and passed_flags[idx]
        receipt = {"task": f"{suite_version}-{idx}", "passed": passed, "submitted_at": submitted_at}
        if refused:
            receipt["status"] = "refused"
        results.append(
            TaskResult(
                task_id=task.id,
                task_hash="bad-task-hash" if mismatched_task else task.content_hash(),
                receipt=receipt,
                passed=passed,
                score=override_score if override_score is not None else (1.0 if passed else -0.5),
            )
        )
    resolved_suite_hash = suite_hash or suite.suite_hash
    return SubmissionBundle(
        suite_hash=resolved_suite_hash,
        suite_version=suite_version,
        task_results=results,
        scheduler_config={"adapter": adapter, "suite": suite_version},
        submitted_at=submitted_at,
    )


class TestBuiltinSuitesDeclareControls:
    def test_every_builtin_suite_declares_at_least_one_valid_control(self) -> None:
        registry = get_default_registry()
        suite_map = builtin_suite_control_map()
        assert len(suite_map) >= 5
        for suite_name, expected_controls in suite_map.items():
            assert len(expected_controls) >= 1, f"{suite_name} must declare >= 1 control"
            suite = _get_suite(suite_name)
            assert suite.measurement_control_ids == expected_controls
            unknown = registry.validate_control_ids(expected_controls)
            assert unknown == [], f"{suite_name} references unknown controls: {unknown}"


class TestControlCoverageSelection:
    def test_two_fixtures_select_later_measurement_regardless_of_input_order(self) -> None:
        older = _make_bundle(suite_version="golden-v1", submitted_at=100.0, passed_flags=[True, False, False, False])
        newer = _make_bundle(suite_version="golden-v1", submitted_at=200.0, passed_flags=[True, True, True, False])

        selected_forward = select_latest_measurements([older, newer])
        selected_reverse = select_latest_measurements([newer, older])

        assert selected_forward == selected_reverse
        assert selected_forward["golden-v1"].submitted_at == 200.0
        assert selected_forward["golden-v1"].pass_rate == pytest.approx(0.6)
        assert selected_forward["golden-v1"].display == "60.0% (3/5)"

    def test_tie_broken_deterministically_across_input_order(self) -> None:
        a = _make_bundle(suite_version="golden-v1", submitted_at=150.0, passed_flags=[True, False])
        b = _make_bundle(suite_version="golden-v1", submitted_at=150.0, passed_flags=[True, True])

        first = select_latest_measurements([a, b])["golden-v1"]
        second = select_latest_measurements([b, a])["golden-v1"]
        assert first == second

    def test_unmeasured_controls_report_unavailable_and_ordering_stays_stable(self) -> None:
        c1 = Control(
            control_id="CTL-Z-01",
            title="Z control",
            description="first in registry",
            references={"eu_ai_act": "Art. 1"},
            evidence_kinds=["test"],
        )
        c2 = Control(
            control_id="CTL-A-02",
            title="A control",
            description="second in registry",
            references={"eu_ai_act": "Art. 2"},
            evidence_kinds=["test"],
        )
        registry = ControlRegistry(controls=[c1, c2])

        meas_b = RecordedMeasurement(
            suite_version="suite-b",
            suite_hash="b" * 64,
            submitted_at=200.0,
            pass_rate=0.5,
            overall_score=0.25,
            passed_tasks=1,
            total_tasks=2,
            bundle_hash="1" * 64,
        )
        cov_one = build_control_coverage(
            registry,
            {"suite-b": ("CTL-Z-01",), "suite-a": ("CTL-Z-01",)},
            [meas_b],
        )
        cov_two = build_control_coverage(
            registry,
            {"suite-a": ("CTL-Z-01",), "suite-b": ("CTL-Z-01",)},
            [meas_b],
        )

        assert [e.to_dict() for e in cov_one] == [e.to_dict() for e in cov_two]
        assert [e.control.control_id for e in cov_one] == ["CTL-Z-01", "CTL-A-02"]
        assert cov_one[0].suites == ("suite-a", "suite-b")
        assert cov_one[0].measurement_display == "suite-a: unavailable, suite-b: 50.0% (1/2)"
        assert cov_one[1].suites == ()
        assert cov_one[1].latest_measurement is None
        assert cov_one[1].measurement_display == "unavailable"


class TestComplianceControlsCoverageCLI:
    def test_controls_cli_displays_unavailable_without_bundles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        text_res = runner.invoke(compliance_group, ["controls"])
        assert text_res.exit_code == 0, text_res.output
        assert "golden-v1" in text_res.stdout
        assert "unavailable" in text_res.stdout

        md_res = runner.invoke(compliance_group, ["controls", "--format", "markdown"])
        assert md_res.exit_code == 0, md_res.output
        assert "| Suites | Latest Measurement |" in md_res.stdout
        assert "unavailable" in md_res.stdout

        json_res = runner.invoke(compliance_group, ["controls", "--format", "json"])
        assert json_res.exit_code == 0, json_res.output
        rows = json.loads(json_res.stdout)
        by_id = {row["control_id"]: row for row in rows}
        assert by_id["CTL-ROB-01"]["suites"] == ["golden-v1", "self-conformance-bench-v1"]
        assert by_id["CTL-ROB-01"]["latest_measurement"] is None
        assert by_id["CTL-ROB-01"]["latest_measurement_display"] == "unavailable"
        assert by_id["CTL-ROB-01"]["suite_measurements"] == {
            "golden-v1": None,
            "self-conformance-bench-v1": None,
        }

    def test_controls_cli_prefers_latest_bundle_across_argument_order(self, tmp_path: Path) -> None:
        older_path = tmp_path / "older.json"
        newer_path = tmp_path / "newer.json"
        _make_bundle(suite_version="golden-v1", submitted_at=100.0, passed_flags=[True, False]).save(older_path)
        _make_bundle(suite_version="golden-v1", submitted_at=300.0, passed_flags=[True, True, True, False]).save(
            newer_path
        )

        runner = CliRunner()
        res_a = runner.invoke(
            compliance_group,
            ["controls", "--format", "json", "--bundle", str(older_path), "--bundle", str(newer_path)],
        )
        res_b = runner.invoke(
            compliance_group,
            ["controls", "--format", "json", "--bundle", str(newer_path), "--bundle", str(older_path)],
        )
        assert res_a.exit_code == 0, res_a.output
        assert res_b.exit_code == 0, res_b.output

        rows_a = json.loads(res_a.stdout)
        rows_b = json.loads(res_b.stdout)
        assert rows_a == rows_b

        by_id = {row["control_id"]: row for row in rows_a}
        rob_01 = by_id["CTL-ROB-01"]
        assert rob_01["suites"] == ["golden-v1", "self-conformance-bench-v1"]
        assert rob_01["latest_measurement"]["submitted_at"] == 300.0
        assert rob_01["latest_measurement"]["pass_rate"] == pytest.approx(0.6)
        assert rob_01["latest_measurement_display"] == (
            "golden-v1: 60.0% (3/5), self-conformance-bench-v1: unavailable"
        )

        # Controls whose declaring suite has no recorded bundle stay unavailable.
        sec_04 = by_id["CTL-SEC-04"]
        assert sec_04["suites"] == ["gate-evasion-v1"]
        assert sec_04["latest_measurement"] is None
        assert sec_04["latest_measurement_display"] == "unavailable"

    def test_controls_cli_discovers_sdd_bench_and_custom_suites_in_bundle_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bench_dir = tmp_path / ".sdd" / "bench"
        bench_dir.mkdir(parents=True)
        (bench_dir / "leaderboard.json").write_text('{"entries": []}', encoding="utf-8")

        custom_suite = BenchSuite(
            version="custom-gov-v1",
            tasks=[
                BenchTask(
                    id="gov-t1",
                    description="Check governance control",
                    steps=("run check",),
                    assertions=("passes",),
                )
            ],
            measurement_control_ids=("CTL-GOV-01",),
        )
        custom_suite.save(bench_dir / "custom-gov-v1.suite.json")
        _make_bundle(
            suite_version="custom-gov-v1",
            suite=custom_suite,
            submitted_at=500.0,
            passed_flags=[True, True],
        ).save(bench_dir / "custom-gov.bundle.json")

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        auto_res = runner.invoke(compliance_group, ["controls", "--format", "text"])
        assert auto_res.exit_code == 0, auto_res.output
        assert "custom-gov-v1" in auto_res.stdout
        assert "100.0% (1/1)" in auto_res.stdout

        dir_res = runner.invoke(
            compliance_group,
            ["controls", "--format", "json", "--bundle-dir", str(bench_dir)],
        )
        assert dir_res.exit_code == 0, dir_res.output
        by_id = {row["control_id"]: row for row in json.loads(dir_res.stdout)}
        assert by_id["CTL-GOV-01"]["suites"] == ["custom-gov-v1"]
        assert by_id["CTL-GOV-01"]["latest_measurement_display"] == "100.0% (1/1)"
        assert by_id["CTL-ROB-01"]["latest_measurement_display"] == "unavailable"

    def test_controls_cli_rejects_corrupted_explicit_bundle(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.json"
        bad.write_text('{"not": "a bundle"}', encoding="utf-8")
        res = CliRunner().invoke(compliance_group, ["controls", "--bundle", str(bad)])
        assert res.exit_code != 0
        assert "Failed to load benchmark bundle" in res.output


def test_unrelated_bundle_with_same_suite_name_cannot_replace_measurement(tmp_path: Path) -> None:
    genuine = tmp_path / "genuine.json"
    _make_bundle(suite_version="golden-v1", submitted_at=10, passed_flags=[False]).save(genuine)
    other = tmp_path / "other.json"
    _make_bundle(suite_version="golden-v1", suite_hash="a" * 64, submitted_at=20, passed_flags=[True]).save(other)
    result = CliRunner().invoke(
        compliance_group, ["controls", "--format", "json", "--bundle", str(genuine), "--bundle", str(other)]
    )
    assert result.exit_code == 0, result.output
    row = next(row for row in json.loads(result.stdout) if row["control_id"] == "CTL-ROB-01")
    assert row["latest_measurement"]["submitted_at"] == 10


@pytest.mark.parametrize(("adapter", "wrong_task"), [("mock", False), ("fixture", True)])
def test_smoke_and_mismatched_task_results_do_not_supersede_measurements(
    tmp_path: Path, adapter: str, wrong_task: bool
) -> None:
    suite = _get_suite("goal-drift-v1")
    for time, source, mismatch in [(10, "fixture", False), (20, adapter, wrong_task)]:
        bundle = _make_bundle(
            suite_version=suite.version,
            suite=suite,
            submitted_at=time,
            passed_flags=[True],
            adapter=source,
            mismatched_task=mismatch,
        )
        bundle.save(tmp_path / f"{time}.json")
    result = CliRunner().invoke(compliance_group, ["controls", "--format", "json", "--bundle-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    row = next(row for row in json.loads(result.stdout) if row["control_id"] == "CTL-ROB-02")
    assert row["latest_measurement"]["submitted_at"] == 10


def test_nonfinite_submission_time_is_refused_before_ranking(tmp_path: Path) -> None:
    bundle = _make_bundle(suite_version="goal-drift-v1", submitted_at=float("nan"), passed_flags=[True])
    path = tmp_path / "invalid-time.json"
    bundle.save(path)
    result = CliRunner().invoke(compliance_group, ["controls", "--bundle", str(path)])
    assert result.exit_code != 0
    assert "finite" in result.output


def test_refused_suite_does_not_replace_recorded_measurement(tmp_path: Path) -> None:
    for time, refused in [(10, False), (20, True)]:
        bundle = _make_bundle(suite_version="goal-drift-v1", submitted_at=time, passed_flags=[False], refused=refused)
        bundle.save(tmp_path / f"{time}.json")
    result = CliRunner().invoke(compliance_group, ["controls", "--format", "json", "--bundle-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    row = next(row for row in json.loads(result.stdout) if row["control_id"] == "CTL-ROB-02")
    assert row["latest_measurement"]["submitted_at"] == 10


def test_nonfinite_score_cannot_produce_invalid_json(tmp_path: Path) -> None:
    bundle = _make_bundle(
        suite_version="goal-drift-v1", submitted_at=10, passed_flags=[False], override_score=float("nan")
    )
    path = tmp_path / "bad-score.json"
    bundle.save(path)
    result = CliRunner().invoke(compliance_group, ["controls", "--format", "json", "--bundle", str(path)])
    assert result.exit_code != 0
    assert not result.stdout
    assert "finite" in result.stderr
