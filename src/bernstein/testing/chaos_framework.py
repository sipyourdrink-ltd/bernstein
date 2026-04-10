"""Structured chaos testing framework for Bernstein reliability validation.

Defines failure scenarios, result types, and reporting utilities to
systematically verify that the orchestrator degrades gracefully under
adverse conditions such as network drops, disk pressure, and agent crashes.

Usage::

    from bernstein.testing.chaos_framework import (
        BUILTIN_SCENARIOS,
        ChaosResult,
        ChaosScenario,
        FailureType,
        ReliabilityReport,
        evaluate_chaos_results,
        format_reliability_report,
    )

    results = [run_scenario(s) for s in BUILTIN_SCENARIOS]
    report = evaluate_chaos_results(results)
    print(format_reliability_report(report))
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

# ---------------------------------------------------------------------------
# Failure types
# ---------------------------------------------------------------------------


class FailureType(Enum):
    """Closed set of failure modes that a chaos scenario can inject."""

    NETWORK_DROP = "network_drop"
    DISK_FULL = "disk_full"
    PROCESS_KILL = "process_kill"
    CLOCK_SKEW = "clock_skew"
    LATENCY_SPIKE = "latency_spike"
    API_ERROR = "api_error"


# ---------------------------------------------------------------------------
# Scenario / Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChaosScenario:
    """Immutable description of a single chaos test scenario.

    Args:
        name: Short machine-readable identifier (e.g. ``"network-flap"``).
        description: Human-readable explanation of what the scenario tests.
        failure_type: Which failure mode to inject.
        duration_s: How long the fault should persist, in seconds.
        intensity: Severity of the fault, from 0.0 (none) to 1.0 (maximum).
        target_service: Logical name of the service to target (e.g. ``"task-server"``).
    """

    name: str
    description: str
    failure_type: FailureType
    duration_s: float
    intensity: float
    target_service: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.intensity <= 1.0:
            msg = f"intensity must be between 0 and 1, got {self.intensity}"
            raise ValueError(msg)


@dataclass(frozen=True)
class ChaosResult:
    """Outcome of executing a single chaos scenario.

    Args:
        scenario: The scenario that was executed.
        data_loss: Whether any data was lost during the fault.
        task_duplication: Whether any tasks were duplicated.
        incorrect_results: Whether any tasks produced incorrect results.
        recovery_time_s: Seconds until the system returned to a healthy state.
        observations: Free-form notes from the test run.
    """

    scenario: ChaosScenario
    data_loss: bool
    task_duplication: bool
    incorrect_results: bool
    recovery_time_s: float
    observations: list[str]

    @property
    def passed(self) -> bool:
        """A result passes when there is no data loss, no duplication, and no incorrect results."""
        return not self.data_loss and not self.task_duplication and not self.incorrect_results


@dataclass(frozen=True)
class ReliabilityReport:
    """Aggregated reliability report from a suite of chaos scenarios.

    Args:
        scenarios: Ordered list of individual results.
        overall_grade: Letter grade from A (best) to F (worst).
        total_scenarios: Total number of scenarios executed.
        passed: Number of scenarios that passed.
        failed: Number of scenarios that failed.
        generated_at: ISO-8601 timestamp when the report was generated.
    """

    scenarios: list[ChaosResult]
    overall_grade: str
    total_scenarios: int
    passed: int
    failed: int
    generated_at: str


# ---------------------------------------------------------------------------
# Built-in scenarios
# ---------------------------------------------------------------------------

BUILTIN_SCENARIOS: list[ChaosScenario] = [
    ChaosScenario(
        name="network-flap",
        description="Repeatedly drop and restore the network link between agents and the task server",
        failure_type=FailureType.NETWORK_DROP,
        duration_s=30.0,
        intensity=0.8,
        target_service="task-server",
    ),
    ChaosScenario(
        name="disk-pressure",
        description="Simulate a nearly-full disk so state writes may fail",
        failure_type=FailureType.DISK_FULL,
        duration_s=60.0,
        intensity=0.9,
        target_service="state-store",
    ),
    ChaosScenario(
        name="agent-crash",
        description="Kill an active agent process mid-task to verify work-loss recovery",
        failure_type=FailureType.PROCESS_KILL,
        duration_s=0.0,
        intensity=1.0,
        target_service="agent",
    ),
    ChaosScenario(
        name="slow-api",
        description="Inject high latency into task-server API responses",
        failure_type=FailureType.LATENCY_SPIKE,
        duration_s=45.0,
        intensity=0.7,
        target_service="task-server",
    ),
    ChaosScenario(
        name="clock-drift",
        description="Skew the system clock forward to test timestamp-dependent logic",
        failure_type=FailureType.CLOCK_SKEW,
        duration_s=120.0,
        intensity=0.5,
        target_service="orchestrator",
    ),
]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

_GRADE_THRESHOLDS: list[str] = ["A", "B", "C", "D", "F"]


def _compute_grade(total: int, failed: int) -> str:
    """Map the number of failures to a letter grade.

    * A — all scenarios passed
    * B — 1 failure
    * C — 2 failures
    * D — 3 failures
    * F — 4 or more failures

    When *total* is 0, returns ``"A"`` (vacuous truth).
    """
    if total == 0 or failed == 0:
        return "A"
    idx = min(failed, len(_GRADE_THRESHOLDS) - 1)
    return _GRADE_THRESHOLDS[idx]


def evaluate_chaos_results(results: list[ChaosResult]) -> ReliabilityReport:
    """Build a ``ReliabilityReport`` from a list of scenario results.

    The grade is determined by the number of failed scenarios:
    A = all pass, B = 1 fail, C = 2, D = 3, F = 4+.
    """
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    grade = _compute_grade(len(results), failed)

    return ReliabilityReport(
        scenarios=results,
        overall_grade=grade,
        total_scenarios=len(results),
        passed=passed,
        failed=failed,
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_reliability_report(report: ReliabilityReport) -> str:
    """Format a ``ReliabilityReport`` as a human-readable string.

    Includes per-scenario verdicts and an overall grade summary.
    """
    lines: list[str] = [
        "",
        f"  Chaos Reliability Report  [Grade: {report.overall_grade}]",
        "=" * 58,
        f"  Generated at : {report.generated_at}",
        f"  Scenarios    : {report.total_scenarios}",
        f"  Passed       : {report.passed}",
        f"  Failed       : {report.failed}",
        "-" * 58,
    ]

    for result in report.scenarios:
        verdict = "PASS" if result.passed else "FAIL"
        lines.append(f"  [{verdict}] {result.scenario.name}")
        lines.append(f"         type       : {result.scenario.failure_type.value}")
        lines.append(f"         target     : {result.scenario.target_service}")
        lines.append(f"         recovery   : {result.recovery_time_s:.1f} s")
        if result.data_loss:
            lines.append("         !! data loss detected")
        if result.task_duplication:
            lines.append("         !! task duplication detected")
        if result.incorrect_results:
            lines.append("         !! incorrect results detected")
        if result.observations:
            for obs in result.observations:
                lines.append(f"         - {obs}")

    lines.append("=" * 58)
    lines.append(f"  Overall grade: {report.overall_grade}")
    lines.append("")

    return "\n".join(lines)
