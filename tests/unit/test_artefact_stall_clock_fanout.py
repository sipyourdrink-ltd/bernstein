"""Unit tests for artefact-progress stall clock, repeated command detector, and fan-out ceiling (#5439)."""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core import defaults
from bernstein.core.orchestration.run_stall import (
    ArtefactProgressClock,
    FanOutController,
    RepeatedCommandDetector,
)
from bernstein.core.orchestration.supervisor_receipt import RecommendedAction, StallReason, recommend_action


class TestArtefactProgressClock:
    def test_log_without_artefacts_stalls_in_t(self) -> None:
        """Fixture agent that logs without artefacts -> stall in T."""
        clock = ArtefactProgressClock()
        task_id = "task-1"
        start_ts = 1000.0
        threshold_s = 600.0  # T = 600s

        clock.register_task(task_id, start_ts)

        # Agent produces multiple log output events over time
        for step in range(1, 10):
            current_time = start_ts + step * 50.0  # up to 450s
            advanced = clock.record_event(task_id, "log_output", current_time)
            assert not advanced
            advanced_stdout = clock.record_event(task_id, "stdout", current_time)
            assert not advanced_stdout

        # At T - 100s (500s elapsed): not stalled
        stalled, reason = clock.check_stall(task_id, now=start_ts + 500.0, threshold_s=threshold_s)
        assert not stalled
        assert reason is None

        # At T (600s elapsed) without artefact progress: stalled!
        stalled, reason = clock.check_stall(task_id, now=start_ts + 600.0, threshold_s=threshold_s)
        assert stalled
        assert reason is not None
        assert "no artefact progress" in reason
        assert "task-1" in reason

    def test_agent_producing_artefact_every_half_t_does_not_stall(self) -> None:
        """Agent producing an artefact every T/2 -> no stall."""
        clock = ArtefactProgressClock()
        task_id = "task-2"
        start_ts = 1000.0
        threshold_s = 600.0  # T = 600s

        clock.register_task(task_id, start_ts)

        # At T/2 (300s), produce file_hash_change
        t1 = start_ts + 300.0
        advanced = clock.record_event(task_id, "file_hash_change", t1)
        assert advanced
        stalled, _ = clock.check_stall(task_id, now=t1, threshold_s=threshold_s)
        assert not stalled

        # At T (600s, which is 300s since last artefact), produce test_result
        t2 = start_ts + 600.0
        advanced = clock.record_event(task_id, "test_result", t2)
        assert advanced
        stalled, _ = clock.check_stall(task_id, now=t2, threshold_s=threshold_s)
        assert not stalled

        # At 1.5 * T (900s, 300s since last artefact), produce artifact_posted
        t3 = start_ts + 900.0
        advanced = clock.record_event(task_id, "artifact_posted", t3)
        assert advanced
        stalled, _ = clock.check_stall(task_id, now=t3, threshold_s=threshold_s)
        assert not stalled

        # Total time elapsed is 900s (> T), but interval between artefacts is 300s (T/2 <= T) -> never stalled
        assert not stalled


class TestRepeatedCommandDetector:
    def test_different_commands_or_exit_codes_do_not_stall(self) -> None:
        detector = RepeatedCommandDetector()
        task_id = "task-cmd-1"

        # Different commands
        stalled, _ = detector.record_command(task_id, "pytest tests/test_a.py", 1, threshold=3)
        assert not stalled
        stalled, _ = detector.record_command(task_id, "pytest tests/test_b.py", 1, threshold=3)
        assert not stalled
        # Same command but different exit code
        stalled, _ = detector.record_command(task_id, "pytest tests/test_a.py", 0, threshold=3)
        assert not stalled

    def test_repeated_failing_command_stalls_and_names_command_and_count(self) -> None:
        """Repeated failing command -> stall reason names the command and count."""
        detector = RepeatedCommandDetector()
        task_id = "task-rebase"
        failing_cmd = "git rebase main"
        exit_code = 1
        threshold = 3

        # 1st attempt
        stalled, reason = detector.record_command(task_id, failing_cmd, exit_code, threshold=threshold)
        assert not stalled
        assert reason is None

        # 2nd attempt
        stalled, reason = detector.record_command(task_id, failing_cmd, exit_code, threshold=threshold)
        assert not stalled
        assert reason is None

        # 3rd attempt reaches threshold -> stall!
        stalled, reason = detector.record_command(task_id, failing_cmd, exit_code, threshold=threshold)
        assert stalled
        assert reason is not None
        assert failing_cmd in reason
        assert "exit code 1" in reason
        assert "repeated 3 times" in reason
        assert "(threshold 3)" in reason


class TestFanOutController:
    def test_fan_out_ceiling_respected_and_halved_on_no_progress(self) -> None:
        """Fan-out primitive: ceiling respected; halving offered to the callback."""
        controller = FanOutController(ceiling=8, degrade_threshold=2)
        audit_events: list[dict[str, Any]] = []

        def audit_callback(event: dict[str, Any]) -> None:
            audit_events.append(event)

        # With 0 no-progress tasks: admission up to ceiling of 8
        admitted, reason = controller.can_admit_task(active_task_count=4, no_progress_task_count=0)
        assert admitted
        assert "Admitted" in reason

        # Active == 8 reaches ceiling
        admitted, reason = controller.can_admit_task(active_task_count=8, no_progress_task_count=0)
        assert not admitted
        assert "Fan-out ceiling reached" in reason

        # Now 2 tasks enter no-progress state (>= degrade_threshold=2) -> ceiling halves 8 -> 4
        admitted, reason = controller.can_admit_task(
            active_task_count=3,
            no_progress_task_count=2,
            audit_callback=audit_callback,
        )
        assert controller.halved
        assert controller.ceiling == 4
        assert len(audit_events) == 1
        assert audit_events[0]["event"] == "fan_out_ceiling_halved"
        assert audit_events[0]["old_ceiling"] == 8
        assert audit_events[0]["new_ceiling"] == 4
        assert audit_events[0]["no_progress_tasks"] == 2

        # Under the new degraded ceiling of 4:
        # active_task_count=3 < 4 -> admitted
        assert admitted

        # active_task_count=4 >= new ceiling 4 -> rejected!
        admitted_new, reason_new = controller.can_admit_task(
            active_task_count=4,
            no_progress_task_count=2,
            audit_callback=audit_callback,
        )
        assert not admitted_new
        assert "ceiling 4" in reason_new


class TestStallReasonEnum:
    def test_new_stall_reasons_exist(self) -> None:
        assert StallReason.ARTEFACT_NO_PROGRESS == "artefact_no_progress"
        assert StallReason.REPEATED_COMMAND == "repeated_command"


# Primitive contracts required before lifecycle wiring can rely on them.
def test_repeated_successful_commands_do_not_trigger_a_failure_stall() -> None:
    detector = RepeatedCommandDetector()
    for _ in range(5):
        assert detector.record_command("healthy", "pytest", 0) == (False, None)


def test_failure_counts_keep_exit_codes_separate_and_ignore_success() -> None:
    detector = RepeatedCommandDetector()
    assert not detector.record_command("task", "pytest", 1)[0]
    assert not detector.record_command("task", "pytest", 1)[0]
    assert not detector.record_command("task", "pytest", 2)[0]
    assert not detector.record_command("task", "pytest", 0)[0]
    stalled, reason = detector.record_command("task", "pytest", 1)
    assert stalled and reason and "exit code 1" in reason and "repeated 3 times" in reason


def test_unregistered_clock_never_reports_a_clean_check() -> None:
    with pytest.raises(KeyError, match="unknown"):
        ArtefactProgressClock().check_stall("unknown", now=1000, threshold_s=600)


def test_late_and_duplicate_artifact_events_cannot_regress_progress() -> None:
    clock = ArtefactProgressClock()
    clock.register_task("task", 100)
    assert clock.record_event("task", "file_hash_change", 200)
    assert not clock.record_event("task", "test_result", 50)
    assert not clock.record_event("task", "test_result", 200)
    assert clock.last_progress_ts["task"] == 200
    assert clock.check_stall("task", 201, 100) == (False, None)
    assert clock.check_stall("task", 300, 100)[0]


def test_failed_degradation_callback_does_not_commit_unrecorded_state() -> None:
    controller = FanOutController()

    def unavailable_sink(_decision: dict[str, Any]) -> None:
        raise RuntimeError("SyntheticAuditSinkFailure")

    with pytest.raises(RuntimeError, match="SyntheticAuditSinkFailure"):
        controller.check_and_degrade(2, audit_callback=unavailable_sink)
    assert controller.ceiling == 8
    assert not controller.halved
    assert controller.audit_records == []
    events: list[dict[str, Any]] = []
    assert controller.check_and_degrade(2, audit_callback=events.append)
    assert controller.ceiling == 4 and controller.halved
    assert not controller.check_and_degrade(3, audit_callback=events.append)
    assert len(events) == len(controller.audit_records) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artefact_stall_threshold_s", 0),
        ("artefact_stall_threshold_s", -1),
        ("repeated_command_threshold", 0),
        ("repeated_command_threshold", 1),
        ("fan_out_ceiling", 0),
        ("fan_out_ceiling", -1),
        ("fan_out_degrade_threshold", 0),
        ("fan_out_degrade_threshold", -1),
    ],
)
def test_invalid_stall_tuning_cannot_replace_current_defaults(field: str, value: int) -> None:
    original = defaults.ORCHESTRATOR
    try:
        with pytest.raises(ValueError, match=field):
            defaults.override("orchestrator", {field: value})
        assert defaults.ORCHESTRATOR is original
    finally:
        defaults.ORCHESTRATOR = original


@pytest.mark.parametrize("threshold", [0.0, -1.0])
def test_clock_requires_a_positive_stall_window(threshold: float) -> None:
    clock = ArtefactProgressClock()
    clock.register_task("task", 0)
    with pytest.raises(ValueError, match="threshold"):
        clock.check_stall("task", 0, threshold)


@pytest.mark.parametrize("threshold", [0, 1])
def test_repeated_failure_requires_more_than_one_observation(threshold: int) -> None:
    with pytest.raises(ValueError, match="threshold"):
        RepeatedCommandDetector().record_command("task", "pytest", 1, threshold=threshold)


@pytest.mark.parametrize(("ceiling", "degrade_threshold"), [(0, 2), (8, 0)])
def test_fanout_requires_positive_limits(ceiling: int, degrade_threshold: int) -> None:
    with pytest.raises(ValueError):
        FanOutController(ceiling=ceiling, degrade_threshold=degrade_threshold)


@pytest.mark.parametrize("reason", [StallReason.ARTEFACT_NO_PROGRESS, StallReason.REPEATED_COMMAND])
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("budget", [0, 1])
def test_new_stall_reasons_preserve_supervisor_budget_action(reason: StallReason, raw: bool, budget: int) -> None:
    expected = RecommendedAction.RESPAWN if budget else RecommendedAction.ESCALATE
    assert recommend_action(str(reason) if raw else reason, [], respawn_budget_remaining=budget) == expected
