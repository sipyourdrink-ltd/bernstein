"""Tests for MCP routine tools."""

from __future__ import annotations

from pathlib import Path

from bernstein.mcp.routine_tools import get_scenario_detail, list_scenarios

# Use the actual templates/scenarios directory
_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "scenarios"


class TestListScenarios:
    def test_returns_list(self) -> None:
        result = list_scenarios(_SCENARIOS_DIR)
        assert isinstance(result, list)

    def test_scenario_has_required_fields(self) -> None:
        result = list_scenarios(_SCENARIOS_DIR)
        if result:
            s = result[0]
            assert "id" in s
            assert "name" in s
            assert "description" in s
            assert "tags" in s
            assert "task_count" in s
            assert "roles" in s

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = list_scenarios(tmp_path)
        assert result == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        result = list_scenarios(tmp_path / "nonexistent")
        assert result == []


class TestGetScenarioDetail:
    def test_returns_none_for_unknown(self) -> None:
        result = get_scenario_detail("nonexistent-scenario", _SCENARIOS_DIR)
        assert result is None

    def test_returns_detail_with_tasks(self) -> None:
        scenarios = list_scenarios(_SCENARIOS_DIR)
        if not scenarios:
            return
        detail = get_scenario_detail(scenarios[0]["id"], _SCENARIOS_DIR)
        assert detail is not None
        assert "tasks" in detail
        assert len(detail["tasks"]) > 0
        task = detail["tasks"][0]
        assert "title" in task
        assert "role" in task
