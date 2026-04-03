"""Tests for coordinator mode dashboard widget."""

from __future__ import annotations

from bernstein.tui.widgets import (
    CoordinatorRow,
    build_coordinator_summary,
    classify_role,
)


class TestClassifyRole:
    def test_coordinator_roles(self) -> None:
        for role in ["coordinator", "manager", "lead", "Manager", "LEAD"]:
            assert classify_role(role) == "coordinator", f"Expected coordinator for {role}"

    def test_worker_roles(self) -> None:
        for role in [
            "backend",
            "frontend",
            "qa",
            "security",
            "devops",
            "worker",
            "backend-engineer",
            "frontend-engineer",
        ]:
            assert classify_role(role) == "worker", f"Expected worker for {role}"

    def test_other_roles(self) -> None:
        assert classify_role("docs") == "other"
        assert classify_role("architect") == "other"
        assert classify_role("") == "other"


class TestCoordinatorSummary:
    def test_empty_list(self) -> None:
        assert build_coordinator_summary([]) == "No coordinator-mode tasks detected"

    def test_only_other_roles(self) -> None:
        rows = [CoordinatorRow(role="docs", task_id="t1", title="Write docs", status="done", elapsed="1m")]
        assert build_coordinator_summary(rows) == "No coordinator-mode tasks detected"

    def test_single_coordinator(self) -> None:
        rows = [CoordinatorRow(role="manager", task_id="t1", title="Plan feature", status="in_progress", elapsed="2m")]
        summary = build_coordinator_summary(rows)
        assert "1 coord" in summary
        assert "1 running" in summary

    def test_coordinator_with_workers(self) -> None:
        rows = [
            CoordinatorRow(role="coordinator", task_id="c1", title="Orchestrate", status="in_progress", elapsed="5m"),
            CoordinatorRow(role="backend", task_id="w1", title="API", status="done", elapsed="3m"),
            CoordinatorRow(role="frontend", task_id="w2", title="UI", status="in_progress", elapsed="2m"),
            CoordinatorRow(role="qa", task_id="w3", title="Tests", status="failed", elapsed="1m"),
        ]
        summary = build_coordinator_summary(rows)
        assert "1 coord" in summary
        assert "1 running" in summary
        assert "3 workers" in summary
        assert "1 active" in summary
        assert "1 done" in summary
        assert "1 failed" in summary

    def test_multiple_coordinators(self) -> None:
        rows = [
            CoordinatorRow(role="manager", task_id="c1", title="Plan", status="in_progress", elapsed="5m"),
            CoordinatorRow(role="lead", task_id="c2", title="Review", status="done", elapsed="3m"),
        ]
        summary = build_coordinator_summary(rows)
        assert "2 coords" in summary
