"""Detect and quarantine flaky tests from repeated pytest runs."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence

    import pytest

logger = logging.getLogger(__name__)

_PYTEST_RESULT_RE = re.compile(
    r"^(?P<test_id>[^\s:]+(?:::[^\s:\[]+)+(?:\[[^\r\n]*?\])?)[ \t]+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
    r"(?:[ \t]+\([^\r\n]*)?"
    r"(?:[ \t]+(?:\[[ \t]*\d+(?:%|[ \t]*/[ \t]*\d+[ \t]*)\]|"
    r"\d+(?:\.\d+)?(?:us|ms|s|m|h)(?:[ \t]+\d+[sm])?))?[ \t]*\r?$",
    re.MULTILINE,
)
_WRITE_LOCK = threading.Lock()


def _coerce_int(raw: object, default: int = 0) -> int:
    """Convert a JSON-loaded primitive to ``int`` safely."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float, str)):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


@dataclass(frozen=True)
class TestRun:
    """Record of a single test execution."""

    test_id: str
    passed: bool
    duration_ms: int
    timestamp: str
    run_id: str


@dataclass(frozen=True)
class FlakyTestReport:
    """Analysis of a single test's flakiness."""

    test_id: str
    total_runs: int
    pass_count: int
    fail_count: int
    flaky_score: float
    is_flaky: bool
    first_seen: str
    last_seen: str
    quarantined: bool


@dataclass(frozen=True)
class FlakyDetectorResult:
    """Result of analyzing all recorded test runs."""

    flaky_tests: list[FlakyTestReport]
    quarantined_count: int
    newly_detected: list[str]
    resolved: list[str]


type PytestOutcome = Literal["PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS"]
type PytestPhase = Literal["collect", "setup", "call", "teardown"]
type PytestPhaseOutcome = Literal["passed", "assertion_failed", "error", "skipped", "xfailed", "xpassed"]


@dataclass(frozen=True)
class PytestPhaseObservation:
    """One structured pytest report, without exception text or test output."""

    node_id: str
    phase: PytestPhase
    outcome: PytestPhaseOutcome


@dataclass(frozen=True)
class PytestInvocationObservation:
    """Observed exit, elapsed duration, final collection, and reports for one invocation.

    Duration is diagnostic metadata and does not affect the proof verdict.
    """

    exit_code: int
    selected_node_ids: tuple[str, ...]
    phases: tuple[PytestPhaseObservation, ...]
    duration_ms: int = 0


@dataclass(frozen=True)
class FixProofObservations:
    """Frozen selections and repeated observations for the proof and regression arms."""

    proof_node_ids: tuple[str, ...]
    regression_node_ids: tuple[str, ...]
    repeats: int
    tests_on_base: tuple[PytestInvocationObservation, ...]
    full_change: tuple[PytestInvocationObservation, ...]
    source_reverted: tuple[PytestInvocationObservation, ...]
    regression_base: tuple[PytestInvocationObservation, ...]
    regression_full: tuple[PytestInvocationObservation, ...]
    quarantined_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixProofVerdict:
    """Deterministic interpretation of observations, without an audit receipt."""

    verdict: Literal["pass", "fail", "inconclusive"]
    reason: str
    proof_failure_ids: tuple[str, ...] = ()
    regression_ids: tuple[str, ...] = ()


def _stable_assertion_failures(
    observations: tuple[PytestInvocationObservation, ...], expected: tuple[str, ...], repeats: int
) -> tuple[frozenset[str] | None, str]:
    node_ids = frozenset(expected)
    if not node_ids or len(node_ids) != len(expected) or any(not node for node in node_ids):
        return None, "selection is empty or invalid"
    if type(repeats) is not int or repeats < 1 or len(observations) != repeats:
        return None, "configured observations are incomplete"

    expected_phases = {(node, phase) for node in node_ids for phase in ("setup", "call", "teardown")}
    prior: frozenset[str] | None = None
    for observation in observations:
        selected = observation.selected_node_ids
        if len(selected) != len(node_ids) or frozenset(selected) != node_ids:
            return None, "observed selection differs from the frozen node IDs"
        phases: set[tuple[str, str]] = set()
        failures: set[str] = set()
        for report in observation.phases:
            key = (report.node_id, report.phase)
            if key in phases or key not in expected_phases:
                return None, "phase reports are duplicated or outside the selection"
            phases.add(key)
            if report.phase == "call" and report.outcome == "assertion_failed":
                failures.add(report.node_id)
            elif report.outcome != "passed":
                return None, "a phase reported an error, skip or unsupported outcome"
        if phases != expected_phases:
            return None, "one or more selected phases were not observed"
        if observation.exit_code != int(bool(failures)):
            return None, "exit status does not agree with observed outcomes"
        current = frozenset(failures)
        if prior is not None and current != prior:
            return None, "assertion outcomes changed between repeats"
        prior = current
    return prior, ""


def evaluate_fix_proof(evidence: FixProofObservations) -> FixProofVerdict:
    """Evaluate fully observed, serial pytest arms without running tests.

    The caller binds the supplied observations to their frozen trees and
    runs the configured commands in isolation. Incomplete, unstable or
    error-only evidence cannot establish proof.
    """
    if set(evidence.proof_node_ids).intersection(evidence.quarantined_node_ids):
        return FixProofVerdict("inconclusive", "proof selection contains a quarantined test")

    groups = (
        ("tests on base", evidence.tests_on_base, evidence.proof_node_ids),
        ("full change", evidence.full_change, evidence.proof_node_ids),
        ("source reverted", evidence.source_reverted, evidence.proof_node_ids),
        ("regression base", evidence.regression_base, evidence.regression_node_ids),
        ("regression full", evidence.regression_full, evidence.regression_node_ids),
    )
    results: list[frozenset[str]] = []
    for label, observations, selection in groups:
        failures, reason = _stable_assertion_failures(observations, selection, evidence.repeats)
        if failures is None:
            return FixProofVerdict("inconclusive", f"{label}: {reason}")
        results.append(failures)

    red, green, reverted, baseline_regressions, full_regressions = results
    shared_full_ids = set(evidence.proof_node_ids).intersection(evidence.regression_node_ids)
    if (green ^ full_regressions).intersection(shared_full_ids):
        return FixProofVerdict("inconclusive", "full-tree outcomes disagree across proof and regression selections")
    regressions = tuple(sorted(full_regressions - baseline_regressions))
    if regressions:
        return FixProofVerdict("fail", "previously passing tests now fail", regression_ids=regressions)
    if len(baseline_regressions) == len(evidence.regression_node_ids):
        return FixProofVerdict("inconclusive", "regression base has no passing test")
    if not red:
        return FixProofVerdict("fail", "tests on base do not reproduce the bug")
    if green:
        return FixProofVerdict("fail", "proof tests fail with the full change")
    if not reverted:
        return FixProofVerdict("fail", "proof tests do not exercise the reverted source changes")
    witnessed = tuple(sorted(red & reverted))
    if not witnessed:
        return FixProofVerdict("inconclusive", "no proof test fails in both negative arms")
    return FixProofVerdict("pass", "stable assertion failures witness the fix", proof_failure_ids=witnessed)


def capture_pytest_observation(args: Sequence[str]) -> PytestInvocationObservation:
    """Capture structured reports inside a dedicated pytest process.

    The caller owns process isolation and environment setup. Capture is
    process-local and assumes serial pytest, without distributed workers.
    An exit code or assertion alone does not prove a fix.
    """
    import pytest

    selected: list[str] = []
    phases: list[PytestPhaseObservation] = []

    class Observer:
        @pytest.hookimpl(trylast=True)
        def pytest_collection_finish(self, session: pytest.Session) -> None:
            selected.extend(item.nodeid for item in session.items)

        def pytest_collectreport(self, report: pytest.CollectReport) -> None:
            if report.failed:
                phases.append(PytestPhaseObservation(report.nodeid, "collect", "error"))
            elif report.skipped:
                phases.append(PytestPhaseObservation(report.nodeid, "collect", "skipped"))

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(
            self, item: pytest.Item, call: pytest.CallInfo[None]
        ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
            report = yield
            outcome: PytestPhaseOutcome
            if hasattr(report, "wasxfail"):
                outcome = "xfailed" if report.skipped else "xpassed"
            elif (
                report.when == "call"
                and report.failed
                and call.excinfo is None
                and isinstance(report.longrepr, str)
                and report.longrepr.startswith("[XPASS(strict)] ")
            ):
                # Pytest marks strict XPASS failed without setting wasxfail.
                outcome = "xpassed"
            elif report.passed:
                outcome = "passed"
            elif report.skipped:
                outcome = "skipped"
            elif report.when == "call" and call.excinfo is not None and call.excinfo.errisinstance(AssertionError):
                outcome = "assertion_failed"
            else:
                outcome = "error"
            phases.append(PytestPhaseObservation(item.nodeid, report.when, outcome))
            return report

    started = perf_counter_ns()
    code = pytest.main(list(args), plugins=[Observer()])
    duration_ms = (perf_counter_ns() - started) // 1_000_000
    return PytestInvocationObservation(int(code), tuple(selected), tuple(phases), duration_ms=duration_ms)


def iter_pytest_outcomes(output: str) -> Iterator[tuple[str, PytestOutcome]]:
    """Yield verbose pytest node outcomes, preserving errors and skipped cases."""
    for match in _PYTEST_RESULT_RE.finditer(output):
        yield match.group("test_id"), cast("PytestOutcome", match.group("status"))


def parse_pytest_output(output: str, *, run_id: str, timestamp: str | None = None) -> list[TestRun]:
    """Parse pytest terminal output into per-test run records."""
    ts = timestamp or datetime.now(UTC).isoformat()
    results: list[TestRun] = []
    for test_id, status in iter_pytest_outcomes(output):
        if status not in {"PASSED", "FAILED", "ERROR"}:
            continue
        results.append(
            TestRun(
                test_id=test_id,
                passed=status == "PASSED",
                duration_ms=0,
                timestamp=ts,
                run_id=run_id,
            )
        )
    return results


class FlakyDetector:
    """Detect and manage flaky tests using run history."""

    HISTORY_FILE = Path(".sdd/metrics/test_runs.jsonl")
    QUARANTINE_FILE = Path(".sdd/runtime/flaky_quarantine.json")
    MIN_RUNS = 5
    FLAKY_THRESHOLD = 0.15
    STABLE_THRESHOLD = 0.02

    def __init__(
        self,
        workdir: Path,
        *,
        min_runs: int | None = None,
        flaky_threshold: float | None = None,
        stable_threshold: float | None = None,
    ) -> None:
        self._workdir = workdir
        self._history_path = workdir / self.HISTORY_FILE
        self._quarantine_path = workdir / self.QUARANTINE_FILE
        self._min_runs = min_runs if min_runs is not None else self.MIN_RUNS
        self._flaky_threshold = flaky_threshold if flaky_threshold is not None else self.FLAKY_THRESHOLD
        self._stable_threshold = stable_threshold if stable_threshold is not None else self.STABLE_THRESHOLD

    def record_run(self, results: list[TestRun]) -> None:
        """Append test run results to the JSONL history file."""
        if not results:
            return
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK, self._history_path.open("a", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(asdict(result), sort_keys=True) + "\n")

    def analyze(self) -> FlakyDetectorResult:
        """Analyze the recorded history and update the quarantine set."""
        history = self._load_history()
        grouped: dict[str, list[TestRun]] = defaultdict(list)
        for run in history:
            grouped[run.test_id].append(run)

        current_quarantine = set(self.get_quarantined())
        next_quarantine = current_quarantine.copy()
        reports: list[FlakyTestReport] = []
        newly_detected: list[str] = []
        resolved: list[str] = []

        for test_id, runs in sorted(grouped.items()):
            sorted_runs = sorted(runs, key=lambda item: item.timestamp)
            total_runs = len(sorted_runs)
            pass_count = sum(1 for run in sorted_runs if run.passed)
            fail_count = total_runs - pass_count
            flaky_score = self._compute_flaky_score(sorted_runs)
            mixed_outcomes = pass_count > 0 and fail_count > 0
            is_flaky = total_runs >= self._min_runs and mixed_outcomes and flaky_score >= self._flaky_threshold

            recent_runs = sorted_runs[-20:]
            stable_score = self._compute_flaky_score(recent_runs) if recent_runs else 0.0
            if is_flaky:
                if test_id not in current_quarantine:
                    newly_detected.append(test_id)
                next_quarantine.add(test_id)
            elif test_id in current_quarantine and recent_runs and stable_score <= self._stable_threshold:
                next_quarantine.discard(test_id)
                resolved.append(test_id)

            reports.append(
                FlakyTestReport(
                    test_id=test_id,
                    total_runs=total_runs,
                    pass_count=pass_count,
                    fail_count=fail_count,
                    flaky_score=round(flaky_score, 4),
                    is_flaky=is_flaky,
                    first_seen=sorted_runs[0].timestamp,
                    last_seen=sorted_runs[-1].timestamp,
                    quarantined=test_id in next_quarantine,
                )
            )

        self._write_quarantine(sorted(next_quarantine))
        return FlakyDetectorResult(
            flaky_tests=reports,
            quarantined_count=len(next_quarantine),
            newly_detected=sorted(newly_detected),
            resolved=sorted(resolved),
        )

    def get_quarantined(self) -> list[str]:
        """Return the currently quarantined test ids."""
        if not self._quarantine_path.exists():
            return []
        try:
            raw: object = json.loads(self._quarantine_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        items = cast("list[object]", raw)
        return [item for item in items if isinstance(item, str)]

    def pytest_deselect_args(self) -> str:
        """Return pytest ``--deselect`` arguments for quarantined tests."""
        quarantined = self.get_quarantined()
        return " ".join(f"--deselect {test_id}" for test_id in quarantined)

    def _compute_flaky_score(self, runs: list[TestRun]) -> float:
        """Compute a recency-weighted failure ratio for a test."""
        if not runs:
            return 0.0
        now = datetime.now(UTC)
        weighted_failures = 0.0
        weighted_total = 0.0
        for run in runs:
            try:
                run_dt = datetime.fromisoformat(run.timestamp)
            except ValueError:
                run_dt = now
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=UTC)
            weight = 2.0 if now - run_dt <= timedelta(days=7) else 1.0
            weighted_total += weight
            if not run.passed:
                weighted_failures += weight
        return weighted_failures / weighted_total if weighted_total else 0.0

    def _load_history(self) -> list[TestRun]:
        """Load recorded test runs from the history file."""
        if not self._history_path.exists():
            return []
        runs: list[TestRun] = []
        for line in self._history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw: object = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed flaky-test history line")
                continue
            if not isinstance(raw, dict):
                continue
            data = cast("dict[str, object]", raw)
            try:
                duration_raw = data.get("duration_ms", 0)
                runs.append(
                    TestRun(
                        test_id=str(data["test_id"]),
                        passed=bool(data["passed"]),
                        duration_ms=_coerce_int(duration_raw, 0),
                        timestamp=str(data["timestamp"]),
                        run_id=str(data.get("run_id", "")),
                    )
                )
            except KeyError:
                continue
        return runs

    def _write_quarantine(self, quarantined: list[str]) -> None:
        """Persist the current quarantine set."""
        self._quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        self._quarantine_path.write_text(json.dumps(quarantined, indent=2), encoding="utf-8")
