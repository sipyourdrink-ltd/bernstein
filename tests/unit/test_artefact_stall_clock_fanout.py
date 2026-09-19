"""Unit tests for artefact-progress stall clock, repeated command detector, and fan-out ceiling (#5439)."""

from __future__ import annotations

from typing import Any

from bernstein.core.orchestration.run_stall import (
    ArtefactProgressClock,
    FanOutController,
    RepeatedCommandDetector,
)
from bernstein.core.orchestration.supervisor_receipt import StallReason


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
        """Fan-out test: ceiling respected; halving recorded in the audit log."""
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
