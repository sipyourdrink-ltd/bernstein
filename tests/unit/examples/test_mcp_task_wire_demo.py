"""Exercise the standalone MCP example and its server over the legacy wire."""

from __future__ import annotations

import runpy

import pytest
from mcp import types

from tests.support.mcp_wire import legacy_mcp_session
from tests.unit.examples import ROOT, run_demo

DEMO = ROOT / "examples" / "mcp-task-wire" / "demo.py"


def test_demo_shows_negotiation_and_both_envelopes(tmp_path):
    result = run_demo("mcp-task-wire", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Protocol: 2025-11-25",
        "Tasks capability: True",
        "Tool task support: optional",
        "CallToolResult: hello, Ada",
        "CreateTaskResult: demo-task-0001 working ttl=86400000",
    ]


@pytest.mark.asyncio
async def test_demo_server_accepts_plain_and_augmented_calls():
    build_server = runpy.run_path(str(DEMO))["build_demo_server"]
    async with legacy_mcp_session(build_server()) as client:
        listing = await client.list_tools()
        tool = next(tool for tool in listing.tools if tool.name == "demo_greet")
        assert tool.execution is not None and tool.execution.task_support == "optional"
        ordinary = await client.call_tool(tool.name, {"name": "Grace"})
        augmented = await client.call_tool_as_task(tool.name, {"name": "Grace"})

    assert not ordinary.is_error
    assert ordinary.content == [types.TextContent(type="text", text="hello, Grace")]
    assert augmented.task.task_id == "demo-task-0001"
    assert augmented.task.status == "working"
    assert augmented.task.ttl == 86_400_000
