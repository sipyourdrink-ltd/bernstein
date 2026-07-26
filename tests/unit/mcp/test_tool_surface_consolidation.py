"""Consolidated MCP tool surface and deprecated aliases (#3087).

The surface an operator reads in ``tools/list`` answers each question once:

  * ``bernstein_status`` absorbs ``bernstein_health`` (liveness field),
    ``bernstein_tasks`` (``status`` filter) and ``bernstein_cost`` (cost
    fields), with a ``detail`` flag for the full breakdowns.
  * ``bernstein_run`` absorbs ``bernstein_create_subtask`` via an optional
    ``parent_task_id``.
  * ``bernstein_scenario`` absorbs the three scenario tools behind an
    ``action`` selector and returns the same handle shape as
    ``bernstein_run`` so one poll tool covers both.
  * The misleading names are renamed: ``bernstein_task_handle`` ->
    ``bernstein_run_status``, ``bernstein_update`` ->
    ``bernstein_post_message``, ``bernstein_context`` ->
    ``bernstein_task_capsule``, ``bernstein_stop`` ->
    ``bernstein_shutdown_orchestrator``, ``verify_chain`` ->
    ``bernstein_verify_lineage``.

Every old name stays callable for one minor release as an alias that is not
advertised, names its replacement in the result, and is gated by one
environment variable with a stated removal release.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.protocols.mcp.tool_tiers import (
    ALIAS_ENV_VAR,
    ALIAS_REMOVAL_RELEASE,
    DEPRECATED_TOOL_ALIASES,
    TOOL_TIERS,
    tools_for_tier,
)
from bernstein.mcp.server import create_mcp_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_payload() -> dict[str, Any]:
    return {
        "total": 10,
        "open": 3,
        "claimed": 2,
        "done": 4,
        "failed": 1,
        "per_role": [
            {"role": "backend", "open": 2, "claimed": 1, "done": 3, "failed": 0, "cost_usd": 0.05},
        ],
        "total_cost_usd": 0.12,
    }


def _mock_client(get_payload: Any = None, post_payload: Any = None) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get_payload is not None:
        get_response = MagicMock()
        get_response.raise_for_status = MagicMock()
        get_response.json = MagicMock(return_value=get_payload)
        client.get = AsyncMock(return_value=get_response)
    if post_payload is not None:
        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json = MagicMock(return_value=post_payload)
        client.post = AsyncMock(return_value=post_response)
    return client


async def _call_json(mcp: Any, name: str, args: dict[str, Any]) -> Any:
    result = await mcp.call_tool(name, args)
    if hasattr(result, "content"):
        text = result.content[0].text
    else:
        text = result[0][0].text
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed and "result" in parsed:
        return parsed["result"]
    return parsed


def _list_tools(mcp: Any) -> Any:
    """Invoke the low-level tools/list handler (what a client actually sees)."""
    from mcp.types import ListToolsRequest

    handler = mcp._mcp_server.request_handlers[ListToolsRequest]

    async def run() -> Any:
        response = await handler(ListToolsRequest(method="tools/list"))
        return response.root.tools

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# The advertised surface
# ---------------------------------------------------------------------------


def test_alias_table_covers_every_removed_name() -> None:
    assert DEPRECATED_TOOL_ALIASES == {
        "bernstein_health": "bernstein_status",
        "bernstein_tasks": "bernstein_status",
        "bernstein_cost": "bernstein_status",
        "bernstein_create_subtask": "bernstein_run",
        "bernstein_task_handle": "bernstein_run_status",
        "bernstein_update": "bernstein_post_message",
        "bernstein_context": "bernstein_task_capsule",
        "bernstein_stop": "bernstein_shutdown_orchestrator",
        "bernstein_scenarios": "bernstein_scenario",
        "bernstein_scenario_status": "bernstein_scenario",
        "verify_chain": "bernstein_verify_lineage",
    }
    # Every replacement is a declared, advertised tool; no alias is.
    assert set(DEPRECATED_TOOL_ALIASES.values()) <= set(TOOL_TIERS)
    assert not set(DEPRECATED_TOOL_ALIASES) & set(TOOL_TIERS)


def test_no_deprecated_name_is_advertised_at_any_tier(tmp_path: Any) -> None:
    for tier in ("core", "standard", "all"):
        mcp = create_mcp_server(tier=tier, lineage_enabled=True, lineage_root=tmp_path)
        wire_names = {t.name for t in _list_tools(mcp)}
        assert not wire_names & set(DEPRECATED_TOOL_ALIASES), tier


def test_wire_tools_list_hides_aliases_and_shows_the_consolidated_surface(tmp_path: Any) -> None:
    mcp = create_mcp_server(tier="all", lineage_enabled=True, lineage_root=tmp_path)
    wire_names = {t.name for t in _list_tools(mcp)}
    assert wire_names == set(tools_for_tier("all"))
    assert not wire_names & set(DEPRECATED_TOOL_ALIASES)
    assert {
        "bernstein_run",
        "bernstein_status",
        "bernstein_run_status",
        "bernstein_post_message",
        "bernstein_task_capsule",
        "bernstein_shutdown_orchestrator",
        "bernstein_cancel",
        "bernstein_scenario",
        "bernstein_verify_lineage",
    } <= wire_names


def test_core_tier_is_the_run_loop() -> None:
    assert tools_for_tier("core") == ["bernstein_run", "bernstein_run_status", "bernstein_status"]


# ---------------------------------------------------------------------------
# Folded bernstein_status: liveness + counts + cost + status filter + detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_liveness_counts_and_cost() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=_mock_client(get_payload=_status_payload())):
        body = await _call_json(mcp, "bernstein_status", {})
    assert body["live"] is True
    assert body["counts"]["total"] == 10
    assert body["cost"]["total_cost_usd"] == pytest.approx(0.12)
    assert body["cost"]["per_role"] == [{"role": "backend", "cost_usd": 0.05}]
    # Compact by default: the full per-role breakdown needs detail=true.
    assert "per_role" not in body
    assert "tasks" not in body


@pytest.mark.asyncio
async def test_status_filter_lists_matching_tasks() -> None:
    tasks = [
        {"id": "t1", "title": "one", "role": "backend", "status": "failed", "description": "d"},
    ]
    client = _mock_client(get_payload=_status_payload())
    tasks_response = MagicMock()
    tasks_response.raise_for_status = MagicMock()
    tasks_response.json = MagicMock(return_value=tasks)
    status_response = MagicMock()
    status_response.raise_for_status = MagicMock()
    status_response.json = MagicMock(return_value=_status_payload())

    def _route(url: str, **_kwargs: Any) -> MagicMock:
        return tasks_response if url.endswith("/tasks") else status_response

    client.get = AsyncMock(side_effect=_route)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        body = await _call_json(mcp, "bernstein_status", {"status": "failed"})
    assert body["status_filter"] == "failed"
    assert [t["id"] for t in body["tasks"]] == ["t1"]
    # Compact rows: id/title/role/status only, unless detail is set.
    assert set(body["tasks"][0]) == {"id", "title", "role", "status"}


@pytest.mark.asyncio
async def test_status_detail_includes_full_breakdowns() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=_mock_client(get_payload=_status_payload())):
        body = await _call_json(mcp, "bernstein_status", {"detail": True})
    assert body["per_role"] == _status_payload()["per_role"]


@pytest.mark.asyncio
async def test_status_stays_live_when_the_task_server_is_down() -> None:
    """Folding health in means the tool is the liveness answer: it must not
    turn a task-server outage into a bare error with no liveness signal."""
    mcp = create_mcp_server(server_url="http://localhost:8052")
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=ConnectionError("refused"))
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        body = await _call_json(mcp, "bernstein_status", {})
    assert body["live"] is True
    assert "error" in body


# ---------------------------------------------------------------------------
# bernstein_run absorbs create_subtask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_parent_task_id_posts_to_self_create() -> None:
    created = {"id": "sub-1", "title": "sub", "status": "open", "parent_task_id": "t-parent"}
    client = _mock_client(post_payload=created)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        body = await _call_json(mcp, "bernstein_run", {"goal": "split work", "parent_task_id": "t-parent"})
    url = client.post.call_args[0][0]
    assert url.endswith("/tasks/self-create")
    posted = client.post.call_args.kwargs["json"]
    assert posted["parent_task_id"] == "t-parent"
    assert body["task_id"] == "sub-1"
    assert body["parent_task_id"] == "t-parent"
    assert body["run_id"]
    assert body["poll_after_ms"] > 0


@pytest.mark.asyncio
async def test_run_without_parent_still_posts_to_tasks() -> None:
    created = {"id": "abc123", "title": "Add auth", "status": "open"}
    client = _mock_client(post_payload=created)
    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        body = await _call_json(mcp, "bernstein_run", {"goal": "Add auth"})
    assert client.post.call_args[0][0].endswith("/tasks")
    assert body["task_id"] == "abc123"


# ---------------------------------------------------------------------------
# bernstein_scenario: one tool, an action selector, a pollable handle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_action_list_returns_the_library() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    body = await _call_json(mcp, "bernstein_scenario", {"action": "list"})
    assert isinstance(body, list)


@pytest.mark.asyncio
async def test_scenario_action_run_returns_the_run_handle_shape() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")

    async def fake_invoke(scenario_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "orchestration_id": "orch-1",
            "scenario_id": scenario_id,
            "task_count": 2,
            "estimated_minutes": 30,
            "task_ids": ["t-a", "t-b"],
        }

    with patch("bernstein.mcp.routine_tools.invoke_scenario_via_server", side_effect=fake_invoke):
        body = await _call_json(mcp, "bernstein_scenario", {"action": "run", "scenario_id": "pr-review"})

    from bernstein.core.tasks.checkpoint_retry import task_run_id

    # The same handle fields bernstein_run returns, so bernstein_run_status
    # polls a scenario run with no scenario-specific tooling.
    assert body["task_id"] == "t-a"
    assert body["run_id"] == task_run_id("t-a")
    assert body["poll_after_ms"] > 0
    assert body["orchestration_id"] == "orch-1"
    assert [t["task_id"] for t in body["tasks"]] == ["t-a", "t-b"]
    assert all(t["run_id"] == task_run_id(t["task_id"]) for t in body["tasks"])


@pytest.mark.asyncio
async def test_scenario_action_status_aggregates_by_orchestration() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")

    async def fake_status(orchestration_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"orchestration_id": orchestration_id, "task_count": 0, "status_counts": {}, "tasks": []}

    with patch("bernstein.mcp.routine_tools.fetch_scenario_status", side_effect=fake_status):
        body = await _call_json(mcp, "bernstein_scenario", {"action": "status", "orchestration_id": "orch-1"})
    assert body["orchestration_id"] == "orch-1"


@pytest.mark.asyncio
async def test_scenario_run_requires_a_scenario_id() -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    body = await _call_json(mcp, "bernstein_scenario", {"action": "run"})
    assert "error" in body


# ---------------------------------------------------------------------------
# Deprecated aliases: callable, honest, gated, and scheduled for removal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_alias_is_callable_and_names_its_replacement(tmp_path: Any) -> None:
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all", lineage_enabled=True, lineage_root=tmp_path)
    registered = set(mcp._tool_manager._tools)
    assert set(DEPRECATED_TOOL_ALIASES) <= registered

    body = await _call_json(mcp, "bernstein_health", {})
    assert body["deprecated"] is True
    assert body["replacement"] == "bernstein_status"
    assert body["removal_release"] == ALIAS_REMOVAL_RELEASE
    assert body["result"] == {"status": "ok"}


@pytest.mark.asyncio
async def test_task_handle_alias_projects_the_same_handle(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    monkeypatch.chdir(tmp_path)
    journal = EventJournal(task_run_id("abc123"), tmp_path / ".sdd")
    journal.record("run_started", goal="g")
    journal.record("run_completed", result="done")

    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    new = await _call_json(mcp, "bernstein_run_status", {"run_id": "abc123"})
    old = await _call_json(mcp, "bernstein_task_handle", {"run_id": "abc123"})
    assert old["deprecated"] is True
    assert old["replacement"] == "bernstein_run_status"
    assert old["result"] == new


@pytest.mark.asyncio
async def test_aliases_can_be_disabled_by_one_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALIAS_ENV_VAR, "0")
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    assert not set(DEPRECATED_TOOL_ALIASES) & set(mcp._tool_manager._tools)
    with pytest.raises(Exception):  # noqa: B017 - any error proves it is unroutable
        await mcp.call_tool("bernstein_health", {})


def test_alias_is_gated_by_its_replacement_tier() -> None:
    """An alias never widens the surface: out-of-tier replacements have no alias."""
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="standard")
    registered = set(mcp._tool_manager._tools)
    # Scenario tools are 'all' tier, so their aliases are absent at standard.
    assert "bernstein_scenarios" not in registered
    assert "bernstein_scenario_status" not in registered
    # Core/standard replacements keep their aliases callable.
    assert "bernstein_tasks" in registered
    assert "bernstein_update" in registered


# ---------------------------------------------------------------------------
# Prompts are regenerated from the registry
# ---------------------------------------------------------------------------


def test_prompt_bodies_name_only_registered_tools() -> None:
    """A rename that lands without a prompt update must fail this test."""
    import re

    from bernstein.mcp.prompts import (
        _cost_recap_template,
        _orchestrate_goal_template,
        _triage_failed_tasks_template,
    )

    tool_name_re = re.compile(r"\b(?:bernstein_[a-z0-9_]+|load_skill|verify_chain)\b")
    bodies = [
        _orchestrate_goal_template(goal="g", role="backend", scope="medium"),
        _triage_failed_tasks_template(limit=5),
        _cost_recap_template(window="today"),
    ]
    mentioned: set[str] = set()
    for body in bodies:
        mentioned |= set(tool_name_re.findall(body))
    assert mentioned, "prompt bodies should name the tools that drive them"
    unknown = mentioned - set(TOOL_TIERS)
    assert unknown == set(), f"prompt bodies name unregistered tools: {sorted(unknown)}"
