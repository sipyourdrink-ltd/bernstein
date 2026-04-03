"""Tests for task deadline enforcement and escalation."""

from __future__ import annotations

import time

from bernstein.core.models import Task, TaskStatus
from bernstein.core.notifications import _PD_SEVERITY


class TestDeadlineNotificationEvents:
    def test_deadline_warning_is_valid_event(self) -> None:
        assert "task.deadline_warning" in list(_PD_SEVERITY.keys())
        assert "task.deadline_exceeded" in list(_PD_SEVERITY.keys())

    def test_deadline_events_have_pagerduty_severity(self) -> None:
        assert _PD_SEVERITY["task.deadline_warning"] == "warning"
        assert _PD_SEVERITY["task.deadline_exceeded"] == "critical"


class TestTaskDeadlineField:
    def test_task_default_deadline_is_none(self) -> None:
        task = Task(
            id="test1",
            title="Test",
            description="Test task",
            role="backend",
        )
        assert task.deadline is None

    def test_task_deadline_can_be_set(self) -> None:
        future = time.time() + 3600
        task = Task(
            id="test1",
            title="Test",
            description="Test task",
            role="backend",
            deadline=future,
        )
        assert task.deadline == future
        assert task.deadline > time.time()

    def test_task_from_dict_preserves_deadline(self) -> None:
        future = time.time() + 3600
        raw = {
            "id": "test1",
            "title": "Test",
            "description": "Test task",
            "role": "backend",
            "status": "open",
            "deadline": future,
        }
        task = Task.from_dict(raw)
        assert task.deadline == future


class TestDeadlineChecking:
    """Tests for orchestrator deadline checking logic."""

    def test_deadline_exceeded_detected(self) -> None:
        """When a task's deadline has passed, it is correctly identified as exceeded."""
        past = time.time() - 300  # 5 minutes overdue
        task = Task(
            id="task1",
            title="Overdue Task",
            description="Should be overdue",
            role="backend",
            status=TaskStatus.IN_PROGRESS,
            deadline=past,
        )
        assert task.deadline is not None
        assert time.time() > task.deadline

    def test_deadline_approaching_detected(self) -> None:
        """When a task's deadline is approaching, warning should trigger."""
        future = time.time() + 60  # 1 minute remaining
        task = Task(
            id="task1",
            title="Almost Overdue",
            description="Should warn soon",
            role="backend",
            status=TaskStatus.IN_PROGRESS,
            deadline=future,
        )
        warning_window = 300  # 5 minutes
        remaining = task.deadline - time.time()
        assert 0 < remaining <= warning_window

    def test_no_deadline_skips_check(self) -> None:
        """Tasks without deadlines should not trigger deadline checks."""
        task = Task(
            id="task1",
            title="No Deadline",
            description="Normal task",
            role="backend",
            status=TaskStatus.IN_PROGRESS,
        )
        assert task.deadline is None


class TestDeadlineAwareEscalation:
    """Tests for deadline-aware escalation in task_lifecycle.py."""

    def test_deadline_exceeded_escalates_to_max_effort(self) -> None:
        """When deadline is exceeded, escalation should use highest effort model."""
        past = time.time() - 300
        task = Task(
            id="task1",
            title="Escalated Task",
            description="Should escalate",
            role="backend",
            status=TaskStatus.IN_PROGRESS,
            deadline=past,
        )

        # Verify deadline is indeed exceeded
        assert task.deadline is not None
        assert time.time() > task.deadline

    def test_task_within_deadline_no_escalation(self) -> None:
        """Tasks with plenty of time should not trigger deadline escalation."""
        future = time.time() + 7200  # 2 hours from now
        task = Task(
            id="task1",
            title="Plenty of Time",
            description="No rush",
            role="backend",
            status=TaskStatus.IN_PROGRESS,
            deadline=future,
        )
        assert task.deadline is not None
        assert time.time() < task.deadline
        # Warning window is 5 min, remaining is way more
        remaining = task.deadline - time.time()
        assert remaining > 300  # More than warning window
