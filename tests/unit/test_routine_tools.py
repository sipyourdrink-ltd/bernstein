"""Tests for MCP routine tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.mcp.routine_tools import get_scenario_detail, invoke_scenario_via_server, list_scenarios

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

    def test_returns_task_ids_and_depends_on_for_sequenced_scenario(self) -> None:
        detail = get_scenario_detail("disaster-recovery-failover-drill", _SCENARIOS_DIR)
        assert detail is not None
        assert [t["id"] for t in detail["tasks"]] == ["inventory-backups", "run-dr-workflow", "audit-dr-receipt"]
        assert detail["tasks"][1]["depends_on"] == ["inventory-backups"]
        assert detail["tasks"][2]["depends_on"] == ["run-dr-workflow"]


@pytest.mark.asyncio
async def test_invoke_scenario_via_server_resolves_depends_on_and_skips_on_parent_failure() -> None:
    posted: list[dict[str, Any]] = []

    async def fake_post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> MagicMock:
        posted.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"id": f"mcp-task-{len(posted)}"}
        return resp

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=fake_post)

    with patch("bernstein.mcp.routine_tools.httpx.AsyncClient", return_value=client):
        result = await invoke_scenario_via_server(
            "disaster-recovery-failover-drill",
            server_url="http://127.0.0.1:8052",
            scenarios_dir=_SCENARIOS_DIR,
        )

    assert result["task_ids"] == ["mcp-task-1", "mcp-task-2", "mcp-task-3"]
    assert "depends_on" not in posted[0]
    assert posted[1]["depends_on"] == ["mcp-task-1"]
    assert posted[2]["depends_on"] == ["mcp-task-2"]

    # If the first POST fails, dependent tasks must be skipped rather than posted unconstrained.
    posted_fail: list[dict[str, Any]] = []

    async def failing_first_post(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> MagicMock:
        posted_fail.append(json)
        raise RuntimeError("boom")

    client.post = AsyncMock(side_effect=failing_first_post)
    with patch("bernstein.mcp.routine_tools.httpx.AsyncClient", return_value=client):
        failed_result = await invoke_scenario_via_server(
            "disaster-recovery-failover-drill",
            server_url="http://127.0.0.1:8052",
            scenarios_dir=_SCENARIOS_DIR,
        )

    assert failed_result["task_ids"] == []
    assert len(posted_fail) == 1
