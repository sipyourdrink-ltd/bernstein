"""Tests for Bernstein MCP server tools and crash protection."""

from __future__ import annotations

import json
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
async def test_bernstein_stop_sends_stop_signal() -> None:
    """bernstein_stop writes a SHUTDOWN signal file and confirms."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path_cls.return_value = mock_path
        mock_path.__truediv__ = MagicMock(return_value=mock_path)
        mock_path.exists = MagicMock(return_value=True)
        mock_path.write_text = MagicMock()

        result = await mcp.call_tool("bernstein_stop", {})

    text = result[0][0].text  # type: ignore[index]
    assert "stop" in text.lower() or "shutdown" in text.lower()


# ---------------------------------------------------------------------------
# bernstein_approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_approve_completes_task() -> None:
    """bernstein_approve calls POST /tasks/{id}/complete to approve a task."""
    from bernstein.mcp.server import create_mcp_server

    completed = _make_task_payload("task-ap-01", status="done")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=completed)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "task-ap-01"})

    text = result[0][0].text  # type: ignore[index]
    assert "task-ap-01" in text or "approved" in text.lower() or "done" in text.lower()
    call_url = mock_client.post.call_args[0][0]
    assert "task-ap-01" in call_url
    assert "complete" in call_url


# ---------------------------------------------------------------------------
# bernstein_health — liveness check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bernstein_health_always_succeeds() -> None:
    """bernstein_health always returns {"status": "ok"} without contacting server."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")
    result = await mcp.call_tool("bernstein_health", {})
    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    assert parsed == {"status": "ok"}


# ---------------------------------------------------------------------------
# Crash protection — error_response helper
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
# Crash protection — tools return error JSON instead of crashing
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
    assert "error" in parsed


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
    assert "error" in parsed


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
    assert "error" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_approve() -> None:
    """bernstein_approve returns error JSON on httpx failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=ConnectionError("refused"))

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_approve", {"task_id": "fake"})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_crash_protection_bernstein_stop() -> None:
    """bernstein_stop returns error JSON on filesystem failure."""
    from bernstein.mcp.server import create_mcp_server

    mcp = create_mcp_server(server_url="http://localhost:8052")

    with patch("bernstein.mcp.server.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path_cls.return_value = mock_path
        mock_path.__truediv__ = MagicMock(return_value=mock_path)
        mock_path.mkdir = MagicMock(side_effect=PermissionError("not allowed"))

        result = await mcp.call_tool("bernstein_stop", {})

    text = result[0][0].text  # type: ignore[index]
    parsed = json.loads(text)
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
