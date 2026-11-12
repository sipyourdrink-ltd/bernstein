"""Unit tests for manager JSON parsing and dependency resolution."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import pytest
from bernstein.core.manager_parsing import (
    _resolve_depends_on,
    dispatch_distribution,
    parse_queue_review_response,
    parse_review_response,
    parse_tasks_response,
    raw_dicts_to_tasks,
)
from bernstein.core.models import TaskType

from bernstein.core.tasks.models import DispatchTier


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, DispatchTier.EDGE),
        ({"dispatch_tier": "local"}, DispatchTier.LOCAL),
        ({"dispatch_tier": "search"}, DispatchTier.SEARCH),
        ({"dispatch_hints": {"estimated_changed_lines": 10, "requires_shell": False}}, DispatchTier.LOCAL),
        ({"dispatch_hints": {"estimated_changed_lines": True, "requires_shell": False}}, DispatchTier.LOCAL),
        ({"dispatch_hints": {"estimated_changed_lines": 1, "requires_shell": "false"}}, DispatchTier.LOCAL),
        ({"dispatch_hints": {"estimated_changed_lines": None, "requires_shell": False}}, DispatchTier.LOCAL),
        ({"dispatch_hints": {"estimated_changed_lines": 0, "requires_shell": False}}, DispatchTier.EDGE),
        ({"dispatch_hints": []}, DispatchTier.LOCAL),
        ({"owned_files": ["src/*.py"]}, DispatchTier.LOCAL),
        ({"owned_files": ["a.py", "b.py"]}, DispatchTier.LOCAL),
        ({"owned_files": ["src/../a.py"]}, DispatchTier.LOCAL),
        ({"title": "Fetch documentation for the API"}, DispatchTier.SEARCH),
        ({"title": "Look up the API version online"}, DispatchTier.SEARCH),
        ({"title": "Look up the parser version in pyproject.toml", "dispatch_hints": None}, DispatchTier.LOCAL),
        ({"title": "Fetch docs from the repository", "dispatch_hints": None}, DispatchTier.LOCAL),
        ({"title": "Research options and implement the parser", "dispatch_hints": None}, DispatchTier.LOCAL),
        ({"title": "Survey API alternatives"}, DispatchTier.RESEARCH),
        ({"title": "Fetch documentation", "task_type": "research"}, DispatchTier.RESEARCH),
        (
            {"dispatch_hints": None, "description": "Analyze existing behavior and implement the repair"},
            DispatchTier.LOCAL,
        ),
        ({"title": "Fetch docs", "dispatch_hints": {"requires_shell": True}}, DispatchTier.SANDBOX),
        ({"completion_signals": [{"type": "test_passes", "value": "pytest"}]}, DispatchTier.SANDBOX),
    ],
)
def test_generated_dispatch_precedence(override: dict[str, object], expected: DispatchTier) -> None:
    raw = {
        "title": "Fix one typo",
        "owned_files": ["src/demo.py"],
        "dispatch_hints": {"estimated_changed_lines": 9, "requires_shell": False},
        **override,
    }
    [task] = raw_dicts_to_tasks([raw], classify_dispatch=True)
    assert task.dispatch_tier is expected


@pytest.mark.parametrize("invalid", [None, 0, [], "not-a-tier"])
def test_generated_invalid_explicit_dispatch_tier_rejects_row(
    invalid: object, caplog: pytest.LogCaptureFixture
) -> None:
    rows = [{"title": "Fetch docs", "dispatch_tier": invalid}, {"title": "Keep this row"}]
    [task] = raw_dicts_to_tasks(rows, classify_dispatch=True)
    assert task.title == "Keep this row"
    assert "Invalid dispatch_tier" in caplog.text


def test_generated_dispatch_keeps_only_valid_hints_and_counts_local(caplog: pytest.LogCaptureFixture) -> None:
    tasks = raw_dicts_to_tasks(
        [
            {"title": "Rename a field", "dispatch_hints": {"estimated_changed_lines": -1, "requires_shell": False}},
            {"title": "Fetch docs", "dispatch_hints": None},
        ],
        classify_dispatch=True,
    )
    assert tasks[0].metadata["dispatch_hints"] == {"requires_shell": False}
    assert "invalid dispatch_hints.estimated_changed_lines" in caplog.text
    assert not tasks[1].metadata
    assert dispatch_distribution(tasks) == "2 tasks: 1 local, 0 edge, 1 search, 0 research, 0 sandbox"
    [legacy] = raw_dicts_to_tasks([{"title": "Fetch docs", "dispatch_tier": "search"}])
    assert legacy.dispatch_tier is DispatchTier.LOCAL


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
    raw = '```json\n[{"title":"Task A", "role":"backend"}]\n```'

    tasks = parse_tasks_response(raw)

    assert tasks == [{"title": "Task A", "role": "backend"}]


def test_parse_tasks_response_rejects_non_array_payload() -> None:
    with pytest.raises(ValueError, match="Expected a JSON array"):
        parse_tasks_response(json.dumps({"title": "not-an-array"}))
