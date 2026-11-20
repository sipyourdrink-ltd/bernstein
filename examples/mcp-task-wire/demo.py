"""Compare plain and task-augmented calls over an in-memory MCP connection."""

from __future__ import annotations

from typing import Any

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.memory import create_client_server_memory_streams

from bernstein.mcp.sdk_compat import LEGACY_TASK_PROTOCOL, legacy_tasks_middleware, task_augmentation_requested

TOOL_NAME = "demo_greet"
_TASK_TTL_MS = 86_400_000
_TASK_ID = "demo-task-0001"


def build_demo_server() -> MCPServer[None]:
    """Register a greeting tool with optional legacy task support."""
    mcp: MCPServer[None] = MCPServer(
        "mcp-task-wire-demo",
        version="1",
        middleware=[legacy_tasks_middleware(frozenset({TOOL_NAME}))],
    )

    @mcp.tool()
    async def demo_greet(name: str = "world") -> types.CallToolResult:
        """Return a greeting or a sample task handle for the greeting."""
        if task_augmentation_requested():
            task = types.Task(
                taskId=_TASK_ID,
                status="working",
                createdAt="2026-11-20T09:00:00Z",
                lastUpdatedAt="2026-11-20T09:00:00Z",
                ttl=_TASK_TTL_MS,
            )
            return types.CallToolResult(
                content=[],
                structuredContent={"task": task.model_dump(by_alias=True, exclude_none=True)},
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"hello, {name}")],
        )

    async def shaped(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        tools = await mcp.list_tools()
        for tool in tools:
            if tool.name == TOOL_NAME:
                tool.execution = types.ToolExecution(taskSupport="optional")
        return types.ListToolsResult(tools=tools)

    mcp._lowlevel_server.add_request_handler("tools/list", types.PaginatedRequestParams, shaped)
    return mcp


async def main() -> None:
    server = build_demo_server()._lowlevel_server
    async with (
        create_client_server_memory_streams() as ((read, write), (server_read, server_write)),
        anyio.create_task_group() as group,
    ):
        group.start_soon(server.run, server_read, server_write, server.create_initialization_options())
        try:
            wire = JSONRPCDispatcher(read, write)
            async with ClientSession(dispatcher=wire, read_timeout_seconds=10) as client:
                initialized = await client.initialize()
                if initialized.protocol_version != LEGACY_TASK_PROTOCOL or initialized.capabilities.tasks is None:
                    raise RuntimeError("Expected legacy MCP Tasks support")
                print("Protocol:", initialized.protocol_version)
                print("Tasks capability:", initialized.capabilities.tasks is not None)

                listing = await client.list_tools()
                tool = next(tool for tool in listing.tools if tool.name == TOOL_NAME)
                if tool.execution is None or tool.execution.task_support != "optional":
                    raise RuntimeError("Expected optional task support for the greeting tool")
                print("Tool task support:", tool.execution.task_support)

                arguments = {"name": "Ada"}
                plain = await client.call_tool(TOOL_NAME, arguments)
                greeting = plain.content[0]
                if not isinstance(greeting, types.TextContent):
                    raise ValueError("Expected a text greeting")
                print(f"{type(plain).__name__}: {greeting.text}")

                # The SDK client expects CallToolResult for tools/call. Decode the
                # legacy task reply explicitly on the same negotiated connection.
                raw = await wire.send_raw_request(
                    "tools/call",
                    {"name": TOOL_NAME, "arguments": arguments, "task": {"ttl": _TASK_TTL_MS}},
                    {"timeout": 10},
                )
                created = types.CreateTaskResult.model_validate(raw, by_name=False)
                print(f"{type(created).__name__}: {created.task.task_id} {created.task.status} ttl={created.task.ttl}")
        finally:
            group.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)
