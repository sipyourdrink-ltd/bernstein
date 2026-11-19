"""Guard the narrow SDK2 legacy-task bridge and request-local state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from mcp.types import CreateTaskResult
from pydantic import ValidationError

from bernstein.mcp.sdk_compat import legacy_tasks_middleware, task_augmentation_requested


def _context(**changes):
    fields = {
        "method": "tools/call",
        "protocol_version": "2025-11-25",
        "params": {"name": "bernstein_run", "arguments": {}, "task": {}},
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


def _task_payload():
    return {
        "task": {
            "taskId": "test-task",
            "status": "working",
            "createdAt": "2026-11-15T00:00:00Z",
            "lastUpdatedAt": "2026-11-15T00:00:00Z",
            "ttl": 86400000,
        }
    }


@pytest.mark.asyncio
async def test_bridge_isolates_concurrent_plain_and_augmented_calls():
    bridge = legacy_tasks_middleware(frozenset({"bernstein_run"}))
    both_started = asyncio.Event()
    started = 0
    observed = []

    async def normal_pipeline(ctx):
        nonlocal started
        before = task_augmentation_requested()
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        after = task_augmentation_requested()
        observed.append((before, after))
        if after:
            return {"content": [], "structuredContent": _task_payload(), "isError": False}
        return {"content": [{"type": "text", "text": "plain"}], "isError": False}

    augmented, plain = await asyncio.gather(
        bridge(_context(), normal_pipeline),
        bridge(_context(params={"name": "bernstein_run", "arguments": {}}), normal_pipeline),
    )
    assert isinstance(augmented, CreateTaskResult)
    assert plain["content"][0]["text"] == "plain"
    assert sorted(observed) == [(False, False), (True, True)]
    assert not task_augmentation_requested()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(protocol_version="2026-07-28"),
        _context(params={"name": "bernstein_status", "task": {}}),
        _context(params={"name": "bernstein_run"}),
        _context(params={"name": "bernstein_run", "task": "invalid"}),
    ],
)
async def test_plain_forbidden_new_protocol_and_malformed_calls_are_not_bridged(context):
    result = {"content": [], "structuredContent": _task_payload(), "isError": False}
    called = []

    async def normal_pipeline(ctx):
        called.append(ctx)
        assert not task_augmentation_requested()
        return result

    out = await legacy_tasks_middleware(frozenset({"bernstein_run"}))(context, normal_pipeline)
    assert out is result
    assert called == [context]


@pytest.mark.asyncio
async def test_error_results_and_dispatch_exceptions_are_preserved_and_reset_context():
    bridge = legacy_tasks_middleware(frozenset({"bernstein_run"}))
    error = {"content": [], "structuredContent": _task_payload(), "isError": True}

    async def failing_result(ctx):
        assert task_augmentation_requested()
        return error

    assert await bridge(_context(), failing_result) is error
    assert not task_augmentation_requested()

    async def rejected(ctx):
        assert task_augmentation_requested()
        raise ValueError("normal dispatcher rejected the call")

    with pytest.raises(ValueError, match="normal dispatcher rejected"):
        await bridge(_context(), rejected)
    assert not task_augmentation_requested()


@pytest.mark.asyncio
async def test_invalid_task_payload_cannot_become_success():
    async def normal_pipeline(ctx):
        return {"content": [], "structuredContent": {"task": {"taskId": "incomplete"}}}

    with pytest.raises(ValidationError):
        await legacy_tasks_middleware(frozenset({"bernstein_run"}))(_context(), normal_pipeline)
    assert not task_augmentation_requested()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["2025-11-25", "2026-07-28"])
async def test_only_negotiated_legacy_revision_advertises_legacy_tasks(protocol):
    async def normal_pipeline(ctx):
        return {"protocolVersion": protocol, "capabilities": {"tools": {}}}

    result = await legacy_tasks_middleware(frozenset({"bernstein_run"}))(_context(method="initialize"), normal_pipeline)
    assert ("tasks" in result["capabilities"]) is (protocol == "2025-11-25")
    assert result["capabilities"]["tools"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [[], {}, 12, None])
async def test_malformed_tool_names_reach_native_parameter_validation(name):
    from bernstein.mcp.server import create_mcp_server
    from tests.support.mcp_wire import legacy_mcp_session

    async with legacy_mcp_session(create_mcp_server()) as client:
        response = await client.envelope("tools/call", {"name": name, "arguments": {}, "task": {}})
        assert response["error"]["code"] == -32602
        assert "result" not in response
        assert not task_augmentation_requested()
