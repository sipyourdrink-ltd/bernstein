"""Unit tests for manager model dataclasses."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from bernstein.core.manager_models import QueueCorrection, QueueReviewResult, ReviewResult


def test_review_result_creation(make_task: Any) -> None:
    follow_up = [make_task(id="T-200", title="Follow-up")]
    result = ReviewResult(
        verdict="request_changes",
        reasoning="Need stronger tests",
        feedback="Add integration coverage",
        follow_up_tasks=follow_up,
    )

    assert result.verdict == "request_changes"
    assert result.follow_up_tasks[0].id == "T-200"


def test_queue_correction_add_task_payload() -> None:
    correction = QueueCorrection(
        action="add_task",
        task_id=None,
        new_role=None,
        new_priority=None,
        reason="Missing migration",
        new_task={"title": "Write migration", "role": "backend"},
    )

    assert correction.action == "add_task"
    assert correction.new_task is not None
    assert correction.new_task["title"] == "Write migration"


def test_queue_review_result_defaults_and_serialization() -> None:
    result = QueueReviewResult(corrections=[], reasoning="Queue is healthy")
    payload = asdict(result)

    assert result.skipped is False
    assert payload["reasoning"] == "Queue is healthy"
    assert payload["corrections"] == []
