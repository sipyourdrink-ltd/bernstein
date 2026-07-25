"""Tests for Bernstein MCP server tools and crash protection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_status_payload() -> dict:
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


def _non_approvable_statuses() -> set:
    """Every task status an approval must refuse, taken from the state machine.

    Derived rather than listed, so a new status added to ``TaskStatus`` is
    covered by the refusal matrix without editing this file.
    """
    from bernstein.core.tasks.lifecycle import APPROVABLE_TASK_STATUSES
    from bernstein.core.tasks.models import TaskStatus

    return set(TaskStatus) - set(APPROVABLE_TASK_STATUSES)


def _make_task_payload(
    task_id: str = "abc123",
    status: str = "open",
    title: str = "Test task",
    role: str = "backend",
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "description": "A test task",
        "role": role,
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "status": status,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "model": None,
        "effort": None,
        "completion_signals": [],
        "created_at": 1711574400.0,
        "progress_log": [],
        "version": 1,
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_mcp_server_registers_all_tools() -> None:
    """All 7 Bernstein tools must be registered on the FastMCP instance."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "bernstein_health" in tool_names
    assert "bernstein_run" in tool_names
    assert "bernstein_status" in tool_names
    assert "bernstein_tasks" in tool_names
    assert "bernstein_cost" in tool_names
    assert "bernstein_stop" in tool_names
    assert "bernstein_approve" in tool_names


def test_every_registered_tool_has_an_explicit_tier(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Every tool a server registers must declare its tier in ``TOOL_TIERS``.

    ``tool_in_tier`` falls back to the ``all`` tier for an unlisted name, so a
    tool registered without a ``TOOL_TIERS`` entry silently disappears from
    ``tools/list`` under the default ``standard`` tier. Comparing the tool
    manager's registry against the declaration turns that silent drop into a
    test failure at the moment the tool is added.
    """
    from bernstein.core.protocols.mcp.tool_tiers import TOOL_TIERS
    from bernstein.mcp.server import create_mcp_server

    # The ``all`` tier keeps every registered tool, and lineage registration
    # is opt-in, so this build is the widest possible registration set.
    mcp = create_mcp_server(
        server_url="http://localhost:8052",
        tier="all",
        lineage_enabled=True,
        lineage_root=tmp_path,
    )
    registered = {t.name for t in mcp._tool_manager.list_tools()}
    undeclared = sorted(registered - set(TOOL_TIERS))
    assert not undeclared, f"MCP tools registered without a TOOL_TIERS entry: {undeclared}"


# ---------------------------------------------------------------------------
# bernstein_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_run_creates_task() -> None:
    """bernstein_run posts a task to the Bernstein server and returns its ID."""
    from bernstein.mcp.server import create_mcp_server

    created = _make_task_payload(task_id="task-run-01", status="open", title="Add auth")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=created)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_run", {"goal": "Add auth", "role": "backend"})

    text = result[0][0].text  # type: ignore[index]
    assert "task-run-01" in text
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert "/tasks" in call_kwargs[0][0]


@pytest.mark.asyncio
async def test_bernstein_run_uses_default_role() -> None:
    """bernstein_run defaults to 'backend' role when none is provided."""
    from bernstein.mcp.server import create_mcp_server

    created = _make_task_payload(task_id="task-run-02", role="backend")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=created)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_run", {"goal": "Do something"})

    text = result[0][0].text  # type: ignore[index]
    assert "task-run-02" in text
    posted_json = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json", {})
    assert posted_json.get("role") == "backend"


# ---------------------------------------------------------------------------
# bernstein_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_status_returns_summary() -> None:
    """bernstein_status fetches /status and returns open/done/failed counts."""
    from bernstein.mcp.server import create_mcp_server

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_status_payload())

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_status", {})

    text = result[0][0].text  # type: ignore[index]
    assert "open" in text
    assert "done" in text
    mock_client.get.assert_awaited_once()
    assert "/status" in mock_client.get.call_args[0][0]


# ---------------------------------------------------------------------------
# bernstein_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_tasks_lists_tasks() -> None:
    """bernstein_tasks fetches /tasks and returns a formatted list."""
    from bernstein.mcp.server import create_mcp_server

    tasks = [_make_task_payload("t1", "open"), _make_task_payload("t2", "done")]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=tasks)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_tasks", {})

    text = result[0][0].text  # type: ignore[index]
    assert "t1" in text
    assert "t2" in text


@pytest.mark.asyncio
async def test_bernstein_tasks_filters_by_status() -> None:
    """bernstein_tasks passes status filter as query param."""
    from bernstein.mcp.server import create_mcp_server

    tasks = [_make_task_payload("t3", "open")]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=tasks)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        await mcp.call_tool("bernstein_tasks", {"status": "open"})

    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
    assert params.get("status") == "open"


# ---------------------------------------------------------------------------
# bernstein_cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_cost_returns_cost_summary() -> None:
    """bernstein_cost returns total cost and per-role breakdown."""
    from bernstein.mcp.server import create_mcp_server

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_status_payload())

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_cost", {})

    text = result[0][0].text  # type: ignore[index]
    assert "0.12" in text or "cost" in text.lower()


# ---------------------------------------------------------------------------
# bernstein_stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_stop_sends_stop_signal(tmp_path: Path) -> None:
    """bernstein_stop writes a SHUTDOWN signal file and confirms.

    A real project root on disk rather than a patched ``Path``: the handler
    now proves the signal path is contained under the resolved workdir, and
    a mocked path cannot exercise that.
    """
    from bernstein.mcp.server import create_mcp_server

    (tmp_path / ".sdd").mkdir()
    mcp = create_mcp_server(server_url="http://localhost:8052")

    result = await mcp.call_tool("bernstein_stop", {"workdir": str(tmp_path)})

    text = result[0][0].text  # type: ignore[index]
    assert "stop" in text.lower() or "shutdown" in text.lower()
    assert (tmp_path / ".sdd" / "runtime" / "signals" / "SHUTDOWN").is_file()


# ---------------------------------------------------------------------------
# bernstein_approve
# ---------------------------------------------------------------------------


def _approve_client(read_status: str, post_payload: dict) -> AsyncMock:
    """Build a mock httpx client whose GET reports *read_status*.

    The POST returns *post_payload*, so a test can assert both which endpoint
    the approval reached and that it reached one at all.
    """
    read_response = MagicMock()
    read_response.raise_for_status = MagicMock()
    read_response.json = MagicMock(return_value=_make_task_payload("task-ap-01", status=read_status))

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()
    post_response.json = MagicMock(return_value=post_payload)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=read_response)
    mock_client.post = AsyncMock(return_value=post_response)
    return mock_client


def _unwrap_tool_json(result: object) -> dict:
    """Parse a tool result, stripping the MCP cost-meter envelope."""
    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed and "result" in parsed:
        parsed = parsed["result"]
    return parsed


@pytest.mark.asyncio
async def test_bernstein_approve_signs_off_pending_approval_task() -> None:
    """A pending_approval task is signed off via POST /tasks/{id}/complete."""
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client(
        "pending_approval",
        _make_task_payload("task-ap-01", status="done"),
    )

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "task-ap-01"})

    parsed = _unwrap_tool_json(result)
    assert parsed["task_id"] == "task-ap-01"
    assert parsed["approval"] == "completion_signed_off"
    call_url = mock_client.post.call_args[0][0]
    assert "task-ap-01" in call_url
    assert call_url.endswith("/complete")


@pytest.mark.asyncio
async def test_bernstein_approve_refuses_a_planned_task_and_names_the_plan_gate() -> None:
    """A planned task is held by plan mode, whose decision is not the task's to grant.

    Releasing one task would start the work while the plan is still
    undecided, so the refusal points at the plan decision instead. No
    state-changing request is sent.
    """
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client(
        "planned",
        _make_task_payload("task-ap-01", status="open"),
    )

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "task-ap-01"})

    parsed = _unwrap_tool_json(result)
    assert parsed["error"] == "task_not_awaiting_approval"
    assert parsed["current_status"] == "planned"
    assert "plan" in parsed["hint"].lower()
    mock_client.post.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    sorted(s.value for s in _non_approvable_statuses()),
)
@pytest.mark.asyncio
async def test_bernstein_approve_refuses_non_approval_states(status: str) -> None:
    """Every state outside the approvable set is refused without any POST.

    The refusal names the current status so the caller can pick a different
    action instead of retrying the approval.
    """
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client(status, _make_task_payload("task-ap-01", status="done"))

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "task-ap-01", "note": "unstick it"})

    from bernstein.core.tasks.lifecycle import APPROVABLE_TASK_STATUSES

    parsed = _unwrap_tool_json(result)
    assert parsed["error"] == "task_not_awaiting_approval"
    assert parsed["current_status"] == status
    assert status in parsed["message"]
    assert sorted(parsed["approvable_statuses"]) == sorted(s.value for s in APPROVABLE_TASK_STATUSES)
    # The refusal has to name a different action, or the caller can only retry.
    assert "bernstein_update" in parsed["hint"] or "plan" in parsed["hint"].lower()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_bernstein_approve_refuses_task_with_no_status() -> None:
    """A task payload carrying no status fails closed rather than completing."""
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client("", _make_task_payload("task-ap-01", status="done"))

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "task-ap-01"})

    parsed = _unwrap_tool_json(result)
    assert parsed["error"] == "task_not_awaiting_approval"
    assert parsed["current_status"] == "unknown"
    mock_client.post.assert_not_awaited()


def test_bernstein_approve_description_names_the_approvable_states() -> None:
    """The advertised description names every state the tool acts on.

    A model picks the tool from its description, so the description and the
    enforced set must not drift apart.
    """
    from bernstein.core.tasks.lifecycle import APPROVABLE_TASK_STATUSES
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "bernstein_approve")
    description = tool.description or ""
    for state in APPROVABLE_TASK_STATUSES:
        assert state.value in description, f"{state.value} missing from the tool description"


# ---------------------------------------------------------------------------
# bernstein_complete
# ---------------------------------------------------------------------------


def _non_completable_statuses() -> set:
    """Every task status a worker completion must refuse, taken from the state machine.

    Derived rather than listed, so a new status added to ``TaskStatus`` is
    covered by the refusal matrix without editing this file.
    """
    from bernstein.core.tasks.lifecycle import WORKER_COMPLETABLE_TASK_STATUSES
    from bernstein.core.tasks.models import TaskStatus

    return set(TaskStatus) - set(WORKER_COMPLETABLE_TASK_STATUSES)


@pytest.mark.parametrize("status", ["open", "claimed", "in_progress"])
@pytest.mark.asyncio
async def test_bernstein_complete_posts_the_worker_summary(status: str) -> None:
    """bernstein_complete is the worker completion verb: POST /tasks/{id}/complete."""
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client(status, _make_task_payload("task-cp-01", status="done"))

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool(
            "bernstein_complete",
            {"task_id": "task-cp-01", "result_summary": "shipped the parser"},
        )

    parsed = _unwrap_tool_json(result)
    assert parsed["task_id"] == "task-cp-01"
    assert parsed["status"] == "done"
    call_url = mock_client.post.call_args[0][0]
    assert call_url.endswith("/tasks/task-cp-01/complete")
    assert mock_client.post.call_args[1]["json"]["result_summary"] == "shipped the parser"


@pytest.mark.parametrize(
    "status",
    sorted(s.value for s in _non_completable_statuses()),
)
@pytest.mark.asyncio
async def test_bernstein_complete_refuses_a_task_the_caller_is_not_executing(status: str) -> None:
    """Every state outside the worker-held set is refused without any POST.

    ``bernstein_complete`` replaced the completion path that used to sit on
    ``bernstein_approve``. Without this matrix the fix would only rename the
    force-complete: a caller could still finish a parent whose subtasks are
    running, or a task whose worker is gone, with an invented summary.
    """
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client(status, _make_task_payload("task-cp-01", status="done"))

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool(
            "bernstein_complete",
            {"task_id": "task-cp-01", "result_summary": "looked done to me"},
        )

    parsed = _unwrap_tool_json(result)
    assert parsed["error"] == "task_not_completable"
    assert parsed["current_status"] == status
    assert status in parsed["message"]
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_bernstein_complete_refuses_a_task_with_no_status() -> None:
    """A task payload carrying no status fails closed rather than completing."""
    from bernstein.mcp.server import create_mcp_server

    mock_client = _approve_client("", _make_task_payload("task-cp-01", status="done"))

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool(
            "bernstein_complete",
            {"task_id": "task-cp-01", "result_summary": "looked done to me"},
        )

    parsed = _unwrap_tool_json(result)
    assert parsed["error"] == "task_not_completable"
    assert parsed["current_status"] == "unknown"
    mock_client.post.assert_not_awaited()


def test_bernstein_complete_description_names_the_completable_states() -> None:
    """The advertised description names every state the tool acts on.

    A model picks the tool from its description, so the description and the
    enforced set must not drift apart.
    """
    from bernstein.core.tasks.lifecycle import WORKER_COMPLETABLE_TASK_STATUSES
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "bernstein_complete")
    description = tool.description or ""
    for state in WORKER_COMPLETABLE_TASK_STATUSES:
        assert state.value in description, f"{state.value} missing from the tool description"


# ---------------------------------------------------------------------------
# bernstein_health - liveness check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_health_always_succeeds() -> None:
    """bernstein_health always returns {"status": "ok"} without contacting server.

    The MCP cost-meter envelope (#1696) wraps the raw tool payload under
    a ``result`` key when the meter is enabled (the default). Unwrap
    before asserting the inner shape.
    """
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await mcp.call_tool("bernstein_health", {})
    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert parsed == {"status": "ok"}


# ---------------------------------------------------------------------------
# Crash protection - error_response helper
# ---------------------------------------------------------------------------


def test_error_response_returns_json() -> None:
    """_error_response returns valid JSON with error and hint fields."""
    from bernstein.mcp.server import _error_response

    result = _error_response(RuntimeError("boom"))
    parsed = json.loads(result)
    assert parsed["error"] == "boom"
    assert parsed["hint"] == "Task server may be restarting"


def test_error_response_custom_hint() -> None:
    """_error_response respects custom hint."""
    from bernstein.mcp.server import _error_response

    result = _error_response(ValueError("bad"), hint="custom hint")
    parsed = json.loads(result)
    assert parsed["hint"] == "custom hint"


# ---------------------------------------------------------------------------
# Crash protection - tools return error JSON instead of crashing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_protection_bernstein_run() -> None:
    """bernstein_run returns error JSON on httpx failure, not an exception."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_run", {"goal": "test"})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert "hint" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_status() -> None:
    """bernstein_status returns error JSON on httpx failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_status", {})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert "hint" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_tasks() -> None:
    """bernstein_tasks returns error JSON on httpx failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_tasks", {})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert "hint" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_cost() -> None:
    """bernstein_cost returns error JSON on httpx failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_cost", {})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert "hint" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_approve() -> None:
    """bernstein_approve returns error JSON on httpx failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=ConnectionError("refused"))
    mock_client.post = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "fake"})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert "hint" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_stop(tmp_path: Path) -> None:
    """bernstein_stop returns error JSON on filesystem failure.

    The workdir is a real project root so the call clears the containment
    barrier and fails where this test means it to: at the directory create.
    """
    import pathlib

    from bernstein.mcp.server import create_mcp_server

    (tmp_path / ".sdd").mkdir()
    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch.object(pathlib.Path, "mkdir", side_effect=PermissionError("not allowed")):
        result = await mcp.call_tool("bernstein_stop", {"workdir": str(tmp_path)})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    if isinstance(parsed, dict) and "_meter" in parsed:
        parsed = parsed["result"]
    assert "error" in parsed
    assert parsed["hint"] == "Could not write shutdown signal"


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------


def test_http_timeout_constant() -> None:
    """Verify the timeout constant is set to a reasonable value."""
    from bernstein.mcp.server import _HTTP_TIMEOUT

    assert pytest.approx(5.0) == _HTTP_TIMEOUT


# ---------------------------------------------------------------------------
# Authorization header propagation (audit-120)
# ---------------------------------------------------------------------------


def test_auth_headers_empty_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """_auth_headers returns empty dict when BERNSTEIN_AUTH_TOKEN is unset."""
    from bernstein.mcp.server import _auth_headers

    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    assert _auth_headers() == {}


def test_auth_headers_empty_when_token_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """_auth_headers returns empty dict when BERNSTEIN_AUTH_TOKEN is an empty string."""
    from bernstein.mcp.server import _auth_headers

    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "")
    assert _auth_headers() == {}


def test_auth_headers_bearer_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """_auth_headers returns a Bearer header when BERNSTEIN_AUTH_TOKEN is set."""
    from bernstein.mcp.server import _auth_headers

    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "secret-123")
    assert _auth_headers() == {"Authorization": "Bearer secret-123"}


@pytest.mark.asyncio
async def test_bernstein_status_sends_auth_header_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bernstein_status forwards the bearer token when BERNSTEIN_AUTH_TOKEN is set."""
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "tok-xyz")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_status_payload())

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        await mcp.call_tool("bernstein_status", {})

    headers = mock_client.get.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer tok-xyz"


@pytest.mark.asyncio
async def test_bernstein_status_omits_auth_header_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bernstein_status sends no Authorization header when BERNSTEIN_AUTH_TOKEN is unset."""
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_status_payload())

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        await mcp.call_tool("bernstein_status", {})

    headers = mock_client.get.call_args.kwargs.get("headers") or {}
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_bernstein_run_sends_auth_header_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bernstein_run forwards the bearer token on POST when BERNSTEIN_AUTH_TOKEN is set."""
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "run-tok")

    created = _make_task_payload(task_id="task-auth-01", status="open")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=created)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        await mcp.call_tool("bernstein_run", {"goal": "authed task"})

    headers = mock_client.post.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer run-tok"


# ---------------------------------------------------------------------------
# bernstein_run -> bernstein_task_handle round trip
#
# The polling loop is the load-bearing interaction of this surface: a run
# takes minutes to hours, so the caller starts it, gets a response body, and
# comes back later with an identifier out of that body. These tests pin that
# the identifiers the start call hands out are the identifiers the poll tool
# accepts, without the caller deriving anything from our source.
# ---------------------------------------------------------------------------


def _run_tool_fn(mcp, name):
    return mcp._tool_manager._tools[name].fn


async def _call_unwrapped(mcp, name, **kwargs):
    raw = await _run_tool_fn(mcp, name)(**kwargs)
    data = json.loads(raw)
    # Tools are wrapped in the cost-meter envelope by default; unwrap it.
    if isinstance(data, dict) and "_meter" in data and "result" in data:
        return data["result"]
    return data


def _mock_post_client(created: dict):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=created)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


async def _start_run(mcp, created: dict) -> dict:
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=_mock_post_client(created)):
        return await _call_unwrapped(mcp, "bernstein_run", goal="watch me")


@pytest.mark.asyncio
async def test_bernstein_run_response_carries_poll_identifiers() -> None:
    """The start response names the journal run id and an advisory poll delay."""
    from bernstein.core.tasks.checkpoint_retry import task_run_id
    from bernstein.mcp.server import create_mcp_server

    created = _make_task_payload(task_id="abc123", status="open", title="Add auth")
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")

    body = await _start_run(mcp, created)

    # The three fields that shipped before must keep their values and order.
    assert list(body)[:3] == ["task_id", "title", "status"]
    assert body["task_id"] == "abc123"
    assert body["title"] == "Add auth"
    assert body["status"] == "open"
    # The added fields make the poll loop reachable from the response alone.
    assert body["run_id"] == task_run_id("abc123")
    assert isinstance(body["poll_after_ms"], int)
    assert body["poll_after_ms"] > 0


@pytest.mark.asyncio
async def test_run_then_poll_with_task_id_from_response(tmp_path, monkeypatch) -> None:
    """Polling with the task id the start call returned finds the run journal."""
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.chdir(tmp_path)
    journal = EventJournal(task_run_id("abc123"), tmp_path / ".sdd")
    journal.record("run_started", goal="g")
    journal.record("run_completed", result="done")

    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    body = await _start_run(mcp, _make_task_payload(task_id="abc123"))

    handle = await _call_unwrapped(mcp, "bernstein_task_handle", run_id=body["task_id"])
    assert handle["status"] == "completed"
    assert handle["journalHead"] == journal.head()


@pytest.mark.asyncio
async def test_task_handle_both_id_forms_project_the_same_handle(tmp_path, monkeypatch) -> None:
    """The task id and the journal run id project a byte-identical handle."""
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.chdir(tmp_path)
    journal = EventJournal(task_run_id("abc123"), tmp_path / ".sdd")
    journal.record("run_started", goal="g")

    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    body = await _start_run(mcp, _make_task_payload(task_id="abc123"))

    by_task_id = await _call_unwrapped(mcp, "bernstein_task_handle", run_id=body["task_id"])
    by_run_id = await _call_unwrapped(mcp, "bernstein_task_handle", run_id=body["run_id"])
    assert by_task_id == by_run_id
    assert by_task_id["journalHead"] == journal.head()
    assert by_task_id["receiptHash"] == by_run_id["receiptHash"]


@pytest.mark.asyncio
async def test_task_handle_prefixed_id_resolves_to_exactly_one_journal(tmp_path, monkeypatch) -> None:
    """A task id already in ``task-*`` form cannot address two journals."""
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.chdir(tmp_path)
    assert task_run_id("task-abc") == "task-task-abc"

    direct = EventJournal("task-abc", tmp_path / ".sdd")
    direct.record("run_started", goal="direct")
    direct.record("run_completed", result="done")

    derived = EventJournal("task-task-abc", tmp_path / ".sdd")
    derived.record("run_started", goal="derived")

    assert direct.head() != derived.head()

    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")
    handle = await _call_unwrapped(mcp, "bernstein_task_handle", run_id="task-abc")

    # Journal run id first: the collision resolves to the direct journal only.
    assert handle["journalHead"] == direct.head()
    assert handle["journalHead"] != derived.head()
    assert handle["status"] == "completed"


@pytest.mark.asyncio
async def test_task_handle_unresolvable_ids_keep_existing_shapes(tmp_path, monkeypatch) -> None:
    """An id matching neither form answers exactly as it does today."""
    from bernstein.mcp.server import create_mcp_server

    monkeypatch.chdir(tmp_path)
    mcp = create_mcp_server(server_url="http://localhost:8052", tier="all")

    rejected = await _call_unwrapped(mcp, "bernstein_task_handle", run_id="../../etc")
    assert "error" in rejected

    unknown = await _call_unwrapped(mcp, "bernstein_task_handle", run_id="nope")
    assert unknown["runId"] == "nope"
    assert unknown["status"] == "working"
# Connect-time server instructions and module docstring (issue #3076)
# ---------------------------------------------------------------------------

#: Every tool name Bernstein advertises matches one of these shapes, so a
#: rename that leaves prose behind is caught rather than silently shipped.
_TOOL_NAME_RE = re.compile(r"\b(?:bernstein_[a-z0-9_]+|load_skill|verify_chain)\b")

#: Character budget for the text pinned in a connected model's context for
#: the whole session.
_INSTRUCTIONS_BUDGET = 900


def _registered_tool_names(tmp_path: Path) -> set[str]:
    """Return every tool name registered by ``bernstein.mcp.server``.

    Built at the widest tier with lineage on, so the set covers every tool
    the module can register rather than just the default tier.
    """
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(
        server_url="http://localhost:8052",
        tier="all",
        lineage_enabled=True,
        lineage_root=tmp_path / "lineage",
    )
    return set(mcp._tool_manager._tools)


def _tool_names_in(text: str) -> set[str]:
    """Return the tool-shaped identifiers mentioned in ``text``."""
    return set(_TOOL_NAME_RE.findall(text))


def test_server_instructions_fit_the_context_budget() -> None:
    """The connect-time instructions stay inside their character budget."""
    from bernstein.mcp.server import _SERVER_INSTRUCTIONS

    assert len(_SERVER_INSTRUCTIONS) <= _INSTRUCTIONS_BUDGET


def test_server_instructions_state_the_control_loop_in_order() -> None:
    """Identity, then the start-then-poll loop, then the pointer to skills."""
    from bernstein.mcp.server import _SERVER_INSTRUCTIONS

    text = _SERVER_INSTRUCTIONS
    lowered = text.lower()

    run_at = text.index("bernstein_run")
    handle_at = text.index("bernstein_task_handle")
    skill_at = text.index("load_skill")

    # Identity comes first, before any tool is named.
    assert lowered.index("bernstein") < run_at
    # Start, then poll, then the deeper-material pointer.
    assert run_at < handle_at < skill_at

    # The poll loop must say what the identifier is, how long a run lasts,
    # how often to poll, and when to stop.
    assert "run_id" in text
    assert "minutes to hours" in lowered
    assert "poll" in lowered
    for terminal in ("completed", "failed", "cancelled"):
        assert terminal in lowered


def test_server_instruction_tool_names_are_registered(tmp_path: Path) -> None:
    """Instruction text must not outlive a tool rename."""
    from bernstein.mcp.server import _SERVER_INSTRUCTIONS

    mentioned = _tool_names_in(_SERVER_INSTRUCTIONS)
    assert mentioned, "instructions should name the tools that drive a run"
    assert mentioned <= _registered_tool_names(tmp_path)


def test_module_docstring_lists_every_registered_tool(tmp_path: Path) -> None:
    """The first thing a contributor reads must match what the module ships."""
    import bernstein.mcp.server as server_module

    docstring = server_module.__doc__ or ""
    assert _tool_names_in(docstring) == _registered_tool_names(tmp_path)
