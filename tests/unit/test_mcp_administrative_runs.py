"""MCP administrative routing uses the authenticated server without orchestration."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.mcp.server import create_mcp_server

pytestmark = pytest.mark.asyncio


def tool(mcp, name):
    return mcp._tool_manager._tools[name].fn


async def test_administrative_template_and_status_route(monkeypatch):
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    mcp = create_mcp_server(tier="all", server_url="http://testserver")
    run_id = "admin-" + "a" * 32
    payload = {
        "runId": run_id,
        "template": "administrative",
        "goal": "Sprint",
        "tenant_id": "default",
        "status": "working",
        "tasks": [],
        "chainHead": "abc",
        "chainVerified": True,
        "receiptHash": "hash",
    }
    with (
        patch(
            "bernstein.mcp.server._administrative_request", new=AsyncMock(return_value=json.dumps(payload))
        ) as request,
        patch("bernstein.mcp.server._run_impl", new=AsyncMock()) as normal,
    ):
        await tool(mcp, "bernstein_run")(template="administrative")
        request.assert_awaited_once_with(
            "http://testserver", "POST", "/administrative-runs", {"goal": "Administrative run"}
        )
        normal.assert_not_awaited()
        await tool(mcp, "bernstein_run_status")(run_id=run_id)
        assert request.await_args.args == ("http://testserver", "GET", f"/administrative-runs/{run_id}")


async def test_default_template_preserves_run_impl():
    mcp = create_mcp_server(tier="all")
    with patch(
        "bernstein.mcp.server._run_impl", new=AsyncMock(return_value=json.dumps({"error": "offline", "hint": "test"}))
    ) as normal:
        await tool(mcp, "bernstein_run")(goal="Implement feature")
        normal.assert_awaited_once()


async def test_create_task_forwards_run_id():
    import httpx

    mcp = create_mcp_server(tier="all", server_url="http://testserver")
    run_id = "admin-" + "a" * 32
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        201, json={"id": "t1"}, request=httpx.Request("POST", "http://testserver/tasks")
    )
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=client):
        result = await tool(mcp, "bernstein_create_task")(title="Brief", description="Manual", run_id=run_id)
    payload = json.loads(result)
    assert payload.get("result", payload) == {"id": "t1"}
    assert client.post.await_args.kwargs["json"]["run_id"] == run_id


async def test_create_task_run_id_passes_strict_schema():
    from bernstein.mcp.input_validation import validate_or_error

    assert (
        validate_or_error(
            "bernstein_create_task",
            {"title": "Brief", "description": "Manual", "run_id": "admin-" + "a" * 32},
        )
        is None
    )


@pytest.mark.parametrize("run_id", ["ordinary-run", "admin-../secret", "admin-" + "a" * 31, "admin-" + "g" * 32])
async def test_create_task_rejects_malformed_run_id_before_http(run_id):
    mcp = create_mcp_server(tier="all", server_url="http://testserver")
    with patch("bernstein.mcp.server.httpx.AsyncClient") as client:
        result = await tool(mcp, "bernstein_create_task")(title="Brief", description="Manual", run_id=run_id)
    client.assert_not_called()
    assert "payload failed schema validation" in str(result)
