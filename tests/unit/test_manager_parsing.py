"""Unit tests for manager JSON parsing and dependency resolution."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest

from bernstein.core.manager_parsing import (
    _resolve_depends_on,
    parse_queue_review_response,
    parse_review_response,
    parse_tasks_response,
    raw_dicts_to_tasks,
)
from bernstein.core.models import TaskType


def test_parse_queue_review_response_filters_unknown_actions() -> None:
    raw = json.dumps(
        {
            "reasoning": "Need one reassignment and one new task.",
            "corrections": [
                {"action": "reassign", "task_id": "task-1", "new_role": "frontend", "reason": "UI bug"},
                {"action": "add_task", "title": "Backfill tests", "role": "qa", "priority": 1},
                {"action": "something_else", "task_id": "task-9"},
            ],
        }
    )

    result = parse_queue_review_response(raw)

    assert result.reasoning == "Need one reassignment and one new task."
    assert [correction.action for correction in result.corrections] == ["reassign", "add_task"]
    assert result.corrections[1].new_task == {
        "title": "Backfill tests",
        "role": "qa",
        "description": "Backfill tests",
        "priority": 1,
    }


def test_raw_dicts_to_tasks_parses_upgrade_details_and_signals() -> None:
    tasks = raw_dicts_to_tasks(
        [
            {
                "title": "Improve planner",
                "description": "Upgrade planning flow",
                "role": "backend",
                "task_type": "upgrade_proposal",
                "completion_signals": [
                    {"type": "path_exists", "value": "src/bernstein/core/planner.py"},
                    {"type": "invalid", "value": "ignored"},
                ],
                "upgrade_details": {
                    "current_state": "Old planner",
                    "proposed_change": "New planner",
                    "benefits": ["Better latency"],
                    "risk_assessment": {"level": "high", "breaking_changes": True},
                    "rollback_plan": {"steps": ["revert"], "estimated_rollback_minutes": 15},
                    "cost_estimate_usd": 3.5,
                },
            }
        ]
    )

    task = tasks[0]
    assert task.task_type is TaskType.UPGRADE_PROPOSAL
    assert [(signal.type, signal.value) for signal in task.completion_signals] == [
        ("path_exists", "src/bernstein/core/planner.py")
    ]
    assert task.upgrade_details is not None
    assert task.upgrade_details.risk_assessment.level == "high"
    assert task.upgrade_details.rollback_plan.estimated_rollback_minutes == 15


def test_resolve_depends_on_maps_titles_to_generated_ids() -> None:
    tasks = raw_dicts_to_tasks(
        [
            {"title": "Collect evidence", "role": "research"},
            {"title": "Draft answer", "role": "backend", "depends_on": ["Collect evidence", "missing"]},
        ],
        id_prefix="task",
    )

    _resolve_depends_on(tasks)

    assert tasks[1].depends_on == ["task-001"]


def test_parse_review_response_requires_valid_verdict() -> None:
    with pytest.raises(ValueError, match="Invalid verdict"):
        parse_review_response(json.dumps({"verdict": "ship_it"}))


def test_parse_tasks_response_accepts_fenced_json_array() -> None:
    raw = "```json\n[{\"title\":\"Task A\", \"role\":\"backend\"}]\n```"

    tasks = parse_tasks_response(raw)

    assert tasks == [{"title": "Task A", "role": "backend"}]


def test_parse_tasks_response_rejects_non_array_payload() -> None:
    with pytest.raises(ValueError, match="Expected a JSON array"):
        parse_tasks_response(json.dumps({"title": "not-an-array"}))
