"""Tool schema, bounded SSE bus, and creation callback contracts."""

from __future__ import annotations

import json

import pytest

from bernstein.core.protocols.mcp.tool_tiers import TOOL_TIERS
from bernstein.core.server import SSEBus, TaskCreate, TaskStore
from bernstein.mcp.input_validation import load_registry
from bernstein.mcp.remote_transport import RemoteMCPConfig, StreamableHTTPTransport
from bernstein.mcp.server import create_mcp_server


async def advertised_tools(mcp):
    """Use the actual wire handler, including Bernstein's schema shaping."""
    from mcp.types import ListToolsRequest

    handler = mcp._mcp_server.request_handlers[ListToolsRequest]
    response = await handler(ListToolsRequest(method="tools/list"))
    return response.root.tools


@pytest.mark.asyncio
async def test_subscription_schema_and_tiers(monkeypatch):
    import jsonschema

    from bernstein.mcp.input_validation import validate_or_error

    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    mcp = create_mcp_server()
    tool = next(t for t in await advertised_tools(mcp) if t.name == "bernstein_subscribe")
    assert tool.inputSchema["required"] == ["role"]
    assert tool.outputSchema is not None
    jsonschema.validate({"notifications": [], "timed_out": True}, tool.outputSchema)
    assert TOOL_TIERS[tool.name] == "standard"
    assert all(t.name != tool.name for t in await create_mcp_server(tier="core").list_tools())
    schema = load_registry().get(tool.name)
    assert schema["timeoutSeconds"] > schema["properties"]["timeout_seconds"]["maximum"]
    assert validate_or_error(tool.name, {"role": "backend", "timeout_seconds": 61}) is not None
    assert validate_or_error(tool.name, {"role": "backend", "timeout_seconds": 0}) is not None


def test_bus_never_enqueues_unscoped_or_cross_role_title_events():
    bus = SSEBus(max_buffer=1)
    dashboard = bus.subscribe()
    scoped = bus.subscribe(task_role="backend", tenant_id="a", task_ids=frozenset({"id"}))
    for role, tenant, task_id in [("reviewer", "a", "id"), ("backend", "b", "id"), ("backend", "a", "other")]:
        bus.publish("task.created", '{"title":"secret"}', task_role=role, tenant_id=tenant, task_id=task_id)
    assert dashboard.empty() and scoped.empty()
    bus.publish("task_update", '{"id":"other"}')
    assert scoped.empty()
    bus.publish("task.created", '{"title":"visible"}', task_role="backend", tenant_id="a", task_id="id")
    bus.publish("task.created", '{"title":"overflow"}', task_role="backend", tenant_id="a", task_id="id")
    assert "visible" in scoped.get_nowait()
    assert bus.dropped_events_total == 1
    bus.unsubscribe(scoped)
    assert not bus._task_subscriptions


@pytest.mark.asyncio
async def test_created_listener_is_after_durable_write_and_best_effort(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.jsonl")
    received = []

    def listener(task):
        assert task.id in (tmp_path / "tasks.jsonl").read_text()
        received.append(task.id)
        raise ValueError("subscriber failure must not fail creation")

    store.add_task_created_listener(listener)
    task = await store.create(TaskCreate(title="Persisted", description="x", role="backend"))
    assert received == [task.id]

    async def fail(record):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_jsonl", fail)
    with pytest.raises(OSError):
        await store.create(TaskCreate(title="Failed", description="x", role="backend"))
    assert received == [task.id]
    store.remove_task_created_listener(listener)
    assert store._task_created_listeners == []


@pytest.mark.asyncio
async def test_remote_facade_advertises_and_dispatches(monkeypatch):
    import bernstein.mcp.server as server

    remote = StreamableHTTPTransport(RemoteMCPConfig(auth_type="none"), server_url="http://127.0.0.1:8052")
    definitions = await remote._method_tools_list({})
    assert any(t["name"] == "bernstein_subscribe" for t in definitions["tools"])
    calls = []

    async def subscribe(url, role, timeout_seconds):
        calls.append((url, role, timeout_seconds))
        return json.dumps({"notifications": [], "timed_out": True})

    monkeypatch.setattr(server, "_subscribe_impl", subscribe)
    assert json.loads(await remote._execute_tool("bernstein_subscribe", {"role": "backend", "timeout_seconds": 1}))[
        "timed_out"
    ]
    assert calls == [("http://127.0.0.1:8052", "backend", 1)]


@pytest.mark.asyncio
async def test_batch_partial_disk_failure_notifies_only_persisted_rows(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.jsonl")
    received = []
    store.add_task_created_listener(lambda task: received.append(task.title))
    append = store._append_jsonl
    count = 0

    async def fail_second(record):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("second row could not be written")
        await append(record)

    monkeypatch.setattr(store, "_append_jsonl", fail_second)
    with pytest.raises(OSError):
        await store.create_batch(
            [
                TaskCreate(title="Committed first", description="x", role="backend"),
                TaskCreate(title="Failed second", description="x", role="backend"),
            ]
        )
    assert received == ["Committed first"]
    assert "Committed first" in (tmp_path / "tasks.jsonl").read_text()
    assert "Failed second" not in (tmp_path / "tasks.jsonl").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["0", "1"])
@pytest.mark.parametrize(
    "payload",
    [
        {"notifications": [{"task_id": "id", "title": "New task"}], "timed_out": False},
        {"notifications": [], "timed_out": True},
        {"error": "Unauthorized", "hint": "Task server may be restarting"},
    ],
)
async def test_subscribe_actual_structured_payload_matches_advertised_schema(monkeypatch, meter, payload):
    import jsonschema

    import bernstein.mcp.server as server

    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", meter)

    async def subscribe(*args, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr(server, "_subscribe_impl", subscribe)
    mcp = create_mcp_server()
    definition = next(t for t in await advertised_tools(mcp) if t.name == "bernstein_subscribe")
    result = await mcp.call_tool("bernstein_subscribe", {"role": "backend"})
    assert result.structuredContent == json.loads(result.content[0].text)
    jsonschema.validate(result.structuredContent, definition.outputSchema)

    remote = StreamableHTTPTransport(RemoteMCPConfig(auth_type="none"))
    definitions = await remote._method_tools_list({})
    definition = next(t for t in definitions["tools"] if t["name"] == "bernstein_subscribe")
    result = await remote._method_tools_call({"name": "bernstein_subscribe", "arguments": {"role": "backend"}})
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    jsonschema.validate(result["structuredContent"], definition["outputSchema"])
