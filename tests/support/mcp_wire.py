"""A small typed 2025 reference client using the SDK's real JSON-RPC streams.

SDK 2 intentionally removed its experimental Tasks client. Tests must exercise
the retained wire contract rather than mock that removed runtime or its result
serializer. Ordinary and task responses are validated with the SDK wire models.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp.server.mcpserver import MCPServer

_WIRE = TypeAdapter(types.JSONRPCMessage)


class LegacyMCPClient:
    def __init__(self, read: Any, write: Any) -> None:
        self.read = read
        self.write = write
        self.counter = 0
        self.lock = anyio.Lock()

    async def envelope(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.lock:
            self.counter += 1
            request: dict[str, Any] = {"jsonrpc": "2.0", "id": self.counter, "method": method}
            if params is not None:
                request["params"] = params
            await self.write.send(SessionMessage(_WIRE.validate_python(request)))
            with anyio.fail_after(20):
                while True:
                    message = await self.read.receive()
                    response = message.message.model_dump(by_alias=True, exclude_none=True)
                    if response.get("id") == self.counter:
                        return response

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.envelope(method, params)
        assert "error" not in response, response
        return response["result"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        return types.CallToolResult.model_validate(result, by_name=False)

    async def call_tool_as_task(
        self, name: str, arguments: dict[str, Any], *, meta: dict[str, Any] | None = None
    ) -> types.CreateTaskResult:
        params: dict[str, Any] = {"name": name, "arguments": arguments, "task": {"ttl": 86400000}}
        if meta is not None:
            params["_meta"] = meta
        result = await self.request("tools/call", params)
        return types.CreateTaskResult.model_validate(result, by_name=False)

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult.model_validate(await self.request("tools/list"), by_name=False)

    async def get_task(self, task_id: str) -> types.GetTaskResult:
        result = await self.request("tasks/get", {"taskId": task_id})
        return types.GetTaskResult.model_validate(result, by_name=False)


@asynccontextmanager
async def legacy_mcp_session(server: MCPServer[Any]) -> AsyncIterator[LegacyMCPClient]:
    protocol = "2025-11-25"
    low = server._lowlevel_server
    async with create_client_server_memory_streams() as ((read, write), (server_read, server_write)):
        async with anyio.create_task_group() as group:
            group.start_soon(low.run, server_read, server_write, low.create_initialization_options())
            client = LegacyMCPClient(read, write)
            try:
                initialized = await client.request(
                    "initialize",
                    {
                        "protocolVersion": protocol,
                        "capabilities": {"tasks": {}},
                        "clientInfo": {"name": "bernstein-reference-tests", "version": "1"},
                    },
                )
                assert initialized["protocolVersion"] == protocol
                assert initialized["capabilities"]["tasks"]["cancel"] == {}
                await write.send(
                    SessionMessage(_WIRE.validate_python({"jsonrpc": "2.0", "method": "notifications/initialized"}))
                )
                yield client
            finally:
                group.cancel_scope.cancel()


@asynccontextmanager
async def sdk_mcp_session(server: MCPServer[Any]) -> AsyncIterator[ClientSession]:
    """Exercise ordinary calls with the actual SDK2 client and serializer."""
    low = server._lowlevel_server
    async with create_client_server_memory_streams() as ((read, write), (server_read, server_write)):
        async with anyio.create_task_group() as group:
            group.start_soon(low.run, server_read, server_write, low.create_initialization_options())
            try:
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    yield client
            finally:
                group.cancel_scope.cancel()
