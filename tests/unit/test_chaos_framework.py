"""Tests for the chaos testing framework — scenarios, results, grading, formatting."""

from __future__ import annotations

import pytest

from bernstein.testing.chaos_framework import (
    BUILTIN_SCENARIOS,
    ChaosResult,
    ChaosScenario,
    FailureType,
    ReliabilityReport,
    evaluate_chaos_results,
    format_reliability_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scenario(
    *,
    name: str = "test-scenario",
    description: str = "A test scenario",
    failure_type: FailureType = FailureType.NETWORK_DROP,
    duration_s: float = 10.0,
    intensity: float = 0.5,
    target_service: str = "task-server",
) -> ChaosScenario:
    """Build a ``ChaosScenario`` with sensible defaults."""
    return ChaosScenario(
        name=name,
        description=description,
        failure_type=failure_type,
        duration_s=duration_s,
        intensity=intensity,
        target_service=target_service,
    )


def _make_result(
    *,
    scenario: ChaosScenario | None = None,
    data_loss: bool = False,
    task_duplication: bool = False,
    incorrect_results: bool = False,
    recovery_time_s: float = 2.0,
    observations: list[str] | None = None,
) -> ChaosResult:
    """Build a ``ChaosResult`` with sensible defaults."""
    return ChaosResult(
        scenario=scenario or _make_scenario(),
        data_loss=data_loss,
        task_duplication=task_duplication,
        incorrect_results=incorrect_results,
        recovery_time_s=recovery_time_s,
        observations=observations or [],
    )


# ---------------------------------------------------------------------------
# TestFailureType
# ---------------------------------------------------------------------------


class TestFailureType:
    def test_all_members_present(self) -> None:
        expected = {
            "NETWORK_DROP",
            "DISK_FULL",
            "PROCESS_KILL",
            "CLOCK_SKEW",
            "LATENCY_SPIKE",
            "API_ERROR",
        }
        assert {m.name for m in FailureType} == expected

    def test_values_are_snake_case(self) -> None:
        for member in FailureType:
            assert member.value == member.name.lower()


# ---------------------------------------------------------------------------
# TestChaosScenario
# ---------------------------------------------------------------------------


class TestChaosScenario:
    def test_fields(self) -> None:
        s = _make_scenario(name="net-drop", intensity=0.8)
        assert s.name == "net-drop"
        assert s.intensity == 0.8
        assert s.failure_type == FailureType.NETWORK_DROP

    def test_frozen(self) -> None:
        s = _make_scenario()
        with pytest.raises(AttributeError):
            s.name = "changed"  # type: ignore[misc]

    def test_intensity_lower_bound(self) -> None:
        s = _make_scenario(intensity=0.0)
        assert s.intensity == 0.0

    def test_intensity_upper_bound(self) -> None:
        s = _make_scenario(intensity=1.0)
        assert s.intensity == 1.0

    def test_intensity_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="intensity must be between 0 and 1"):
            _make_scenario(intensity=-0.1)

    def test_intensity_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="intensity must be between 0 and 1"):
            _make_scenario(intensity=1.1)


# ---------------------------------------------------------------------------
# TestChaosResult
# ---------------------------------------------------------------------------


class TestChaosResult:
    def test_passed_when_all_clean(self) -> None:
        r = _make_result()
        assert r.passed is True

    def test_failed_on_data_loss(self) -> None:
        r = _make_result(data_loss=True)
        assert r.passed is False

    def test_failed_on_task_duplication(self) -> None:
        r = _make_result(task_duplication=True)
        assert r.passed is False

    def test_failed_on_incorrect_results(self) -> None:
        r = _make_result(incorrect_results=True)
        assert r.passed is False

    def test_failed_on_multiple_issues(self) -> None:
        r = _make_result(data_loss=True, task_duplication=True, incorrect_results=True)
        assert r.passed is False

    def test_frozen(self) -> None:
        r = _make_result()
        with pytest.raises(AttributeError):
            r.data_loss = True  # type: ignore[misc]

    def test_observations_stored(self) -> None:
        r = _make_result(observations=["saw timeout", "retried successfully"])
        assert len(r.observations) == 2
        assert "saw timeout" in r.observations


# ---------------------------------------------------------------------------
# TestReliabilityReport
# ---------------------------------------------------------------------------


class TestReliabilityReport:
    def test_frozen(self) -> None:
        report = ReliabilityReport(
            scenarios=[],
            overall_grade="A",
            total_scenarios=0,
            passed=0,
            failed=0,
            generated_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(AttributeError):
            report.overall_grade = "F"  # type: ignore[misc]

    def test_fields(self) -> None:
        report = ReliabilityReport(
            scenarios=[],
            overall_grade="B",
            total_scenarios=5,
            passed=4,
            failed=1,
            generated_at="2026-04-10T12:00:00+00:00",
        )
        assert report.overall_grade == "B"
        assert report.total_scenarios == 5
        assert report.passed == 4
        assert report.failed == 1


# ---------------------------------------------------------------------------
# TestBuiltinScenarios
# ---------------------------------------------------------------------------


class TestBuiltinScenarios:
    def test_count(self) -> None:
        assert len(BUILTIN_SCENARIOS) == 5

    def test_names_unique(self) -> None:
        names = [s.name for s in BUILTIN_SCENARIOS]
        assert len(names) == len(set(names))

    def test_expected_names(self) -> None:
        names = {s.name for s in BUILTIN_SCENARIOS}
        assert names == {"network-flap", "disk-pressure", "agent-crash", "slow-api", "clock-drift"}

    def test_all_intensities_valid(self) -> None:
        for s in BUILTIN_SCENARIOS:
            assert 0.0 <= s.intensity <= 1.0

    def test_all_are_frozen(self) -> None:
        for s in BUILTIN_SCENARIOS:
            with pytest.raises(AttributeError):
                s.name = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestEvaluateChaosResults
# ---------------------------------------------------------------------------


class TestEvaluateChaosResults:
    def test_all_pass_grade_a(self) -> None:
        results = [_make_result() for _ in range(5)]
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "A"
        assert report.passed == 5
        assert report.failed == 0

    def test_one_fail_grade_b(self) -> None:
        results = [_make_result() for _ in range(4)]
        results.append(_make_result(data_loss=True))
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "B"
        assert report.passed == 4
        assert report.failed == 1

    def test_two_fail_grade_c(self) -> None:
        results = [
            _make_result(),
            _make_result(data_loss=True),
            _make_result(task_duplication=True),
        ]
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "C"
        assert report.failed == 2

    def test_three_fail_grade_d(self) -> None:
        results = [
            _make_result(data_loss=True),
            _make_result(task_duplication=True),
            _make_result(incorrect_results=True),
        ]
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "D"
        assert report.failed == 3

    def test_four_plus_fail_grade_f(self) -> None:
        results = [_make_result(data_loss=True) for _ in range(4)]
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "F"
        assert report.failed == 4

    def test_many_failures_still_f(self) -> None:
        results = [_make_result(data_loss=True) for _ in range(10)]
        report = evaluate_chaos_results(results)
        assert report.overall_grade == "F"

    def test_empty_results_grade_a(self) -> None:
        report = evaluate_chaos_results([])
        assert report.overall_grade == "A"
        assert report.total_scenarios == 0
        assert report.passed == 0
        assert report.failed == 0

    def test_total_scenarios_count(self) -> None:
        results = [_make_result() for _ in range(7)]
        report = evaluate_chaos_results(results)
        assert report.total_scenarios == 7

    def test_generated_at_is_iso_timestamp(self) -> None:
        report = evaluate_chaos_results([_make_result()])
        # Should be a valid ISO-8601 string containing a 'T' separator.
        assert "T" in report.generated_at

    def test_scenarios_list_preserved(self) -> None:
        r1 = _make_result(recovery_time_s=1.0)
        r2 = _make_result(recovery_time_s=2.0)
        report = evaluate_chaos_results([r1, r2])
        assert report.scenarios[0].recovery_time_s == 1.0
        assert report.scenarios[1].recovery_time_s == 2.0


# ---------------------------------------------------------------------------
# TestFormatReliabilityReport
# ---------------------------------------------------------------------------


class TestFormatReliabilityReport:
    def test_returns_string(self) -> None:
        report = evaluate_chaos_results([])
        text = format_reliability_report(report)
        assert isinstance(text, str)
        assert len(text) > 0

    def test_contains_grade(self) -> None:
        report = evaluate_chaos_results([_make_result()])
        text = format_reliability_report(report)
        assert "Grade: A" in text

    def test_contains_pass_verdict(self) -> None:
        report = evaluate_chaos_results([_make_result()])
        text = format_reliability_report(report)
        assert "[PASS]" in text

    def test_contains_fail_verdict(self) -> None:
        report = evaluate_chaos_results([_make_result(data_loss=True)])
        text = format_reliability_report(report)
        assert "[FAIL]" in text

    def test_contains_scenario_name(self) -> None:
        s = _make_scenario(name="my-chaos-test")
        r = _make_result(scenario=s)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "my-chaos-test" in text

    def test_contains_failure_type(self) -> None:
        s = _make_scenario(failure_type=FailureType.DISK_FULL)
        r = _make_result(scenario=s)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "disk_full" in text

    def test_contains_recovery_time(self) -> None:
        r = _make_result(recovery_time_s=3.5)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "3.5" in text

    def test_data_loss_flagged(self) -> None:
        r = _make_result(data_loss=True)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "data loss" in text

    def test_task_duplication_flagged(self) -> None:
        r = _make_result(task_duplication=True)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "task duplication" in text

    def test_incorrect_results_flagged(self) -> None:
        r = _make_result(incorrect_results=True)
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "incorrect results" in text

    def test_observations_included(self) -> None:
        r = _make_result(observations=["network recovered in 2s"])
        report = evaluate_chaos_results([r])
        text = format_reliability_report(report)
        assert "network recovered in 2s" in text

    def test_overall_grade_in_footer(self) -> None:
        report = evaluate_chaos_results([_make_result(data_loss=True)])
        text = format_reliability_report(report)
        assert "Overall grade: B" in text

    def test_generated_at_in_header(self) -> None:
        report = evaluate_chaos_results([_make_result()])
        text = format_reliability_report(report)
        assert report.generated_at in text
