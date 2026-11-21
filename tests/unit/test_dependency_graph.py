"""Tests for the TUI dependency graph renderer."""

from __future__ import annotations

import pytest

from bernstein.tui.dependency_graph import render_dependency_graph


def _task(
    tid: str,
    title: str,
    status: str = "open",
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": tid,
        "title": title,
        "status": status,
        "depends_on": depends_on or [],
    }


class TestEmptyAndStandalone:
    """Edge cases: no tasks, tasks with no dependencies."""

    def test_empty_task_list(self) -> None:
        result = render_dependency_graph([])
        assert result == "(no tasks)"

    def test_single_task_no_deps(self) -> None:
        tasks = [_task("1", "Setup project", "open")]
        result = render_dependency_graph(tasks)
        assert "[OPEN]" in result
        assert "Setup project" in result

    def test_multiple_standalone_tasks(self) -> None:
        tasks = [
            _task("1", "Task A", "done"),
            _task("2", "Task B", "open"),
            _task("3", "Task C", "failed"),
        ]
        result = render_dependency_graph(tasks)
        assert "[DONE]" in result
        assert "[OPEN]" in result
        assert "[FAILED]" in result
        assert "Task A" in result
        assert "Task B" in result
        assert "Task C" in result


@pytest.mark.parametrize("reverse_input", [False, True], ids=["forward", "reversed"])
def test_text_graph_shows_each_chain_edge(reverse_input: bool) -> None:
    tasks = [
        _task("a", "Design API", "done"),
        _task("b", "Implement API", "in_progress", depends_on=["a"]),
        _task("c", "Write tests", "open", depends_on=["b"]),
    ]
    if reverse_input:
        tasks.reverse()
    result = render_dependency_graph(tasks)
    assert "[DONE] Design API --> [IN_PROGRESS] Implement API" in result
    assert "[IN_PROGRESS] Implement API --> [OPEN] Write tests" in result


class TestLinearDependencies:
    """A -> B -> C chain."""

    def test_linear_order_preserved(self) -> None:
        """Tasks appear in dependency order, not input order."""
        tasks = [
            _task("c", "Write tests", "open", depends_on=["b"]),
            _task("a", "Design API", "done"),
            _task("b", "Implement API", "in_progress", depends_on=["a"]),
        ]
        result = render_dependency_graph(tasks)
        lines = result.strip().split("\n")
        # "Design API" should appear before "Write tests"
        design_idx = next(i for i, l in enumerate(lines) if "Design API" in l)
        tests_idx = next(i for i, l in enumerate(lines) if "Write tests" in l)
        assert design_idx < tests_idx


class TestParallelDependencies:
    """Multiple tasks converging: A -> C, B -> C."""

    def test_fan_in(self) -> None:
        tasks = [
            _task("a", "Design API", "done"),
            _task("b", "Setup DB", "done"),
            _task("c", "Implement endpoints", "in_progress", depends_on=["a", "b"]),
        ]
        result = render_dependency_graph(tasks)
        assert "Design API" in result
        assert "Setup DB" in result
        assert "Implement endpoints" in result
        assert "[DONE]" in result
        assert "[IN_PROGRESS]" in result
        # Should have connector lines for multiple parents
        assert "+" in result

    def test_fan_out(self) -> None:
        """One task feeds multiple children: A -> B, A -> C."""
        tasks = [
            _task("a", "Core module", "done"),
            _task("b", "Feature X", "open", depends_on=["a"]),
            _task("c", "Feature Y", "open", depends_on=["a"]),
        ]
        result = render_dependency_graph(tasks)
        assert "Core module" in result
        assert "Feature X" in result
        assert "Feature Y" in result


class TestStatusTags:
    """Verify all status values render correctly."""

    def test_all_statuses(self) -> None:
        statuses = ["done", "in_progress", "failed", "open", "claimed", "blocked"]
        tasks = [_task(str(i), f"Task {s}", s) for i, s in enumerate(statuses)]
        result = render_dependency_graph(tasks)
        assert "[DONE]" in result
        assert "[IN_PROGRESS]" in result
        assert "[FAILED]" in result
        assert "[OPEN]" in result
        assert "[CLAIMED]" in result
        assert "[BLOCKED]" in result

    def test_unknown_status_uses_uppercase(self) -> None:
        tasks = [_task("1", "Mystery", "pending_review")]
        result = render_dependency_graph(tasks)
        assert "[PENDING_REVIEW]" in result


class TestMissingDependencies:
    """Tasks referencing deps not in the task list."""

    def test_missing_dep_ignored(self) -> None:
        tasks = [
            _task("b", "Implement", "open", depends_on=["nonexistent"]),
        ]
        result = render_dependency_graph(tasks)
        assert "Implement" in result
        # Should not crash
