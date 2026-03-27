"""Tests for bernstein_sdk.state_map."""

from __future__ import annotations

import pytest

from bernstein_sdk.models import TaskStatus
from bernstein_sdk.state_map import (
    BernsteinToJira,
    BernsteinToLinear,
    JiraToBernstein,
    LinearToBernstein,
)


class TestJiraToBernstein:
    @pytest.mark.parametrize(
        "jira_status, expected",
        [
            ("To Do", TaskStatus.OPEN),
            ("to do", TaskStatus.OPEN),
            ("Backlog", TaskStatus.OPEN),
            ("In Progress", TaskStatus.IN_PROGRESS),
            ("In Review", TaskStatus.IN_PROGRESS),
            ("Code Review", TaskStatus.IN_PROGRESS),
            ("Blocked", TaskStatus.BLOCKED),
            ("On Hold", TaskStatus.BLOCKED),
            ("Done", TaskStatus.DONE),
            ("Closed", TaskStatus.DONE),
            ("Resolved", TaskStatus.DONE),
            ("Won't Do", TaskStatus.CANCELLED),
            ("Cancelled", TaskStatus.CANCELLED),
            ("Duplicate", TaskStatus.CANCELLED),
        ],
    )
    def test_standard_statuses(self, jira_status: str, expected: TaskStatus) -> None:
        assert JiraToBernstein.map(jira_status) == expected

    def test_unknown_falls_back_to_open(self) -> None:
        assert JiraToBernstein.map("Some Custom Status") == TaskStatus.OPEN

    def test_custom_fallback(self) -> None:
        assert (
            JiraToBernstein.map("Unknown", fallback=TaskStatus.BLOCKED)
            == TaskStatus.BLOCKED
        )

    def test_register_override(self) -> None:
        JiraToBernstein.register("Awaiting Deploy", TaskStatus.BLOCKED)
        assert JiraToBernstein.map("Awaiting Deploy") == TaskStatus.BLOCKED
        assert JiraToBernstein.map("awaiting deploy") == TaskStatus.BLOCKED


class TestBernsteinToJira:
    @pytest.mark.parametrize(
        "status, expected",
        [
            (TaskStatus.OPEN, "To Do"),
            (TaskStatus.CLAIMED, "In Progress"),
            (TaskStatus.IN_PROGRESS, "In Progress"),
            (TaskStatus.DONE, "Done"),
            (TaskStatus.FAILED, "Done"),
            (TaskStatus.BLOCKED, "Blocked"),
            (TaskStatus.CANCELLED, "Won't Do"),
            (TaskStatus.ORPHANED, "To Do"),
        ],
    )
    def test_default_mappings(self, status: TaskStatus, expected: str) -> None:
        assert BernsteinToJira.map(status) == expected

    def test_register_override(self) -> None:
        BernsteinToJira.register(TaskStatus.FAILED, "Rejected")
        assert BernsteinToJira.map(TaskStatus.FAILED) == "Rejected"


class TestLinearToBernstein:
    @pytest.mark.parametrize(
        "linear_state, expected",
        [
            ("triage", TaskStatus.OPEN),
            ("backlog", TaskStatus.OPEN),
            ("unstarted", TaskStatus.OPEN),
            ("started", TaskStatus.IN_PROGRESS),
            ("In Progress", TaskStatus.IN_PROGRESS),
            ("completed", TaskStatus.DONE),
            ("Done", TaskStatus.DONE),
            ("cancelled", TaskStatus.CANCELLED),
            ("Canceled", TaskStatus.CANCELLED),
            ("blocked", TaskStatus.BLOCKED),
        ],
    )
    def test_standard_states(self, linear_state: str, expected: TaskStatus) -> None:
        assert LinearToBernstein.map(linear_state) == expected

    def test_unknown_falls_back(self) -> None:
        assert LinearToBernstein.map("Weird Custom") == TaskStatus.OPEN

    def test_register_override(self) -> None:
        LinearToBernstein.register("QA Review", TaskStatus.BLOCKED)
        assert LinearToBernstein.map("QA Review") == TaskStatus.BLOCKED


class TestBernsteinToLinear:
    @pytest.mark.parametrize(
        "status, expected",
        [
            (TaskStatus.OPEN, "Todo"),
            (TaskStatus.CLAIMED, "In Progress"),
            (TaskStatus.IN_PROGRESS, "In Progress"),
            (TaskStatus.DONE, "Done"),
            (TaskStatus.FAILED, "Cancelled"),
            (TaskStatus.BLOCKED, "Blocked"),
            (TaskStatus.CANCELLED, "Cancelled"),
            (TaskStatus.ORPHANED, "Todo"),
        ],
    )
    def test_default_mappings(self, status: TaskStatus, expected: str) -> None:
        assert BernsteinToLinear.map(status) == expected
