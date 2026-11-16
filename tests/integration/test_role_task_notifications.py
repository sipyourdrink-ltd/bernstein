"""Live HTTP contracts for role task subscriptions (no ASGI stream buffering)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from bernstein.core.server import create_app


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    app = create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl", auth_token="operator-secret")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    @app.get("/test-loop")
    async def capture_loop():
        app.state.test_loop = asyncio.get_running_loop()
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


async def ready(response):
    assert response.status_code == 200, await response.aread()
    lines = response.aiter_lines()
    assert await anext(lines) == "event: heartbeat"
    assert json.loads((await anext(lines))[6:])["connected"]
    assert await anext(lines) == ""
    return lines


async def notification(lines):
    async with asyncio.timeout(4):
        event = ""
        async for line in lines:
            if line.startswith("event:"):
                event = line[7:]
            if line.startswith("data:") and event == "task.created":
                return json.loads(line[6:])
    raise AssertionError("missing notification")


async def cleaned(app):
    async with asyncio.timeout(3):
        while app.state.sse_bus.subscriber_count:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_legacy_task_wire_reconnects_to_persistent_authenticated_store(live_server, monkeypatch):
    from bernstein.mcp.server import create_mcp_server
    from tests.support.mcp_wire import legacy_mcp_session

    _app, url = live_server
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "operator-secret")
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    async with legacy_mcp_session(create_mcp_server(server_url=url)) as session:
        created = await session.call_tool_as_task("bernstein_run", {"goal": "Durable legacy task"})
        task_id = created.task.task_id.split(":", 1)[0]
        pending = await session.request("tasks/result", {"taskId": task_id})
        assert pending["isError"] and "still" in pending["content"][0]["text"]

    headers = {"Authorization": "Bearer operator-secret"}
    async with httpx.AsyncClient(base_url=url, headers=headers) as client:
        stored = await client.get(f"/tasks/{task_id}")
        assert stored.status_code == 200 and stored.json()["title"] == "Durable legacy task"
        claimed = await client.post(f"/tasks/{task_id}/claim")
        assert claimed.status_code == 200, claimed.text
        completed = await client.post(f"/tasks/{task_id}/complete", json={"result_summary": "Verified stored result"})
        assert completed.status_code == 200, completed.text

    # A new MCP server and connection retrieve the authoritative persisted result.
    async with legacy_mcp_session(create_mcp_server(server_url=url)) as session:
        polled = await session.get_task(task_id)
        assert polled.status == "completed"
        result = await session.request("tasks/result", {"taskId": task_id})
        assert not result["isError"] and result["content"][0]["text"] == "Verified stored result"
        listed = await session.request("tasks/list")
        assert any(row["taskId"].split(":", 1)[0] == task_id for row in listed["tasks"])
        second = await session.call_tool_as_task("bernstein_run", {"goal": "Cancel legacy task"})
        cancelled = await session.request("tasks/cancel", {"taskId": second.task.task_id})
        assert cancelled["status"] == "cancelled"
        invalid = await session.envelope("tools/call", {"name": "bernstein_run", "arguments": "invalid", "task": {}})
        assert "error" in invalid
        forbidden = await session.request("tools/call", {"name": "bernstein_status", "arguments": {}, "task": {}})
        assert "content" in forbidden and "task" not in forbidden

    # The SDK2 ordinary client remains usable for normal tool calls.
    from tests.support.mcp_wire import sdk_mcp_session

    server = create_mcp_server(server_url=url)
    async with sdk_mcp_session(server) as client:
        plain = await client.call_tool("bernstein_status", {})
        assert not plain.is_error

    # A legacy task request still traverses the existing HTTP authorization gate.
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN")
    async with legacy_mcp_session(create_mcp_server(server_url=url)) as session:
        denied = await session.request(
            "tools/call", {"name": "bernstein_run", "arguments": {"goal": "Denied"}, "task": {}}
        )
        assert "task" not in denied
        assert "401" in json.loads(denied["content"][0]["text"])["error"]
    async with httpx.AsyncClient(base_url=url, headers=headers) as client:
        rows = await client.get("/tasks", params={"limit": 100, "offset": 0})
        assert rows.status_code == 200 and rows.json()["total"] == 2


@pytest.mark.asyncio
async def test_live_creation_fanout_is_scoped_fast_and_disconnects(live_server):
    app, url = live_server
    headers = {"Authorization": "Bearer operator-secret"}
    async with httpx.AsyncClient(base_url=url, headers=headers) as client:
        async with client.stream("GET", "/events/tasks", params={"role": "backend"}) as first:
            one = await ready(first)
            async with client.stream("GET", "/events/tasks", params={"role": "backend"}) as second:
                two = await ready(second)
                assert app.state.sse_bus.subscriber_count == 2
                assert (
                    await client.post("/tasks", json={"title": "Other role", "description": "x", "role": "reviewer"})
                ).status_code == 201
                failed = await client.post(
                    "/tasks", json={"title": "Rejected", "description": "x", "role": "backend", "scope": "invalid"}
                )
                assert failed.status_code == 422
                start = time.monotonic()
                created = await client.post(
                    "/tasks", json={"title": "Handle incoming brief", "description": "x", "role": "backend"}
                )
                assert created.status_code == 201, created.text
                expected = {"task_id": created.json()["id"], "title": "Handle incoming brief"}
                assert await notification(one) == expected
                assert await notification(two) == expected
                assert time.monotonic() - start < 5
        await cleaned(app)


@pytest.mark.asyncio
async def test_live_auth_role_tenant_and_task_allowlist(live_server):
    app, url = live_server
    identities = app.state.identity_store
    _, token_a = identities.create_identity("session-a", "backend", task_ids=[], metadata={"tenant_id": "a"})
    _, token_b = identities.create_identity("session-b", "backend", task_ids=[], metadata={"tenant_id": "b"})
    _, restricted = identities.create_identity(
        "restricted", "backend", task_ids=["allowed"], metadata={"tenant_id": "a"}
    )
    async with httpx.AsyncClient(base_url=url) as client:
        for token, status in [(None, 401), ("invalid", 401)]:
            response = await client.get(
                "/events/tasks?role=backend", headers={"Authorization": f"Bearer {token}"} if token else {}
            )
            assert response.status_code == status
        auth_a = {"Authorization": f"Bearer {token_a}"}
        assert (await client.get("/events/tasks?role=reviewer", headers=auth_a)).status_code == 403
        assert (
            await client.get("/events/tasks?role=backend", headers=auth_a | {"X-Tenant-Id": "b"})
        ).status_code == 403
        async with client.stream("GET", "/events/tasks?role=backend", headers=auth_a) as response:
            lines = await ready(response)
            assert (
                await client.post(
                    "/tasks",
                    headers={"Authorization": f"Bearer {token_b}"},
                    json={"title": "secret B", "description": "x", "role": "backend"},
                )
            ).status_code == 201
            created = await client.post(
                "/tasks", headers=auth_a, json={"title": "visible A", "description": "x", "role": "backend"}
            )
            assert created.status_code == 201, created.text
            assert (await notification(lines))["title"] == "visible A"
        await cleaned(app)
        async with client.stream(
            "GET", "/events/tasks?role=backend", headers={"Authorization": f"Bearer {restricted}"}
        ) as response:
            lines = await ready(response)
            for task_id in ["hidden", "allowed"]:
                created = await client.post(
                    "/tasks",
                    headers=auth_a,
                    json={"id": task_id, "title": task_id, "description": "x", "role": "backend"},
                )
                assert created.status_code == 201, created.text
            assert (await notification(lines))["task_id"] == "allowed"
        await cleaned(app)


@pytest.mark.asyncio
async def test_batch_direct_store_and_failed_persistence(live_server, monkeypatch):
    from bernstein.core.server import TaskCreate

    app, url = live_server
    async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer operator-secret"}) as client:
        # An unscoped dashboard queue must not receive the new title-bearing event.
        dashboard = app.state.sse_bus.subscribe()
        async with client.stream("GET", "/events/tasks?role=backend") as response:
            lines = await ready(response)
            batch = await client.post(
                "/tasks/batch",
                json={
                    "tasks": [
                        {"title": "Batch", "description": "x", "role": "backend"},
                        {"title": "Batch", "description": "x", "role": "backend"},
                    ]
                },
            )
            assert batch.status_code == 201, batch.text
            assert len(batch.json()["created"]) == 1
            assert (await notification(lines))["title"] == "Batch"
            original = app.state.store._append_jsonl

            async def fail(record):
                raise OSError("simulated disk failure")

            monkeypatch.setattr(app.state.store, "_append_jsonl", fail)
            failed = await client.post(
                "/tasks", json={"title": "Never committed", "description": "x", "role": "backend"}
            )
            assert failed.status_code == 500
            monkeypatch.setattr(app.state.store, "_append_jsonl", original)
            # Direct planner/integration writes also notify through the central listener.
            assert (await client.get("/test-loop")).status_code == 200
            future = asyncio.run_coroutine_threadsafe(
                app.state.store.create(TaskCreate(title="Direct", description="x", role="backend")),
                app.state.test_loop,
            )
            await asyncio.wrap_future(future)
            assert (await notification(lines))["title"] == "Direct"
            buffered = []
            while not dashboard.empty():
                buffered.append(dashboard.get_nowait())
            assert all("task.created" not in entry and "Never committed" not in entry for entry in buffered)
        app.state.sse_bus.unsubscribe(dashboard)
        await cleaned(app)


@pytest.mark.asyncio
async def test_mcp_wait_auth_delivery_timeout_and_cancellation(live_server, monkeypatch):
    from bernstein.mcp.server import create_mcp_server

    app, url = live_server
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "operator-secret")
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    mcp = create_mcp_server(server_url=url)

    async def subscribed():
        async with asyncio.timeout(4):
            while not app.state.sse_bus.subscriber_count:
                await asyncio.sleep(0.01)

    wait = asyncio.create_task(mcp.call_tool("bernstein_subscribe", {"role": "backend", "timeout_seconds": 4}))
    await subscribed()
    async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer operator-secret"}) as client:
        created = await client.post("/tasks", json={"title": "MCP incoming", "description": "x", "role": "backend"})
        assert created.status_code == 201
    result = await wait
    assert result.structured_content["notifications"][0]["title"] == "MCP incoming"
    await cleaned(app)
    start = time.monotonic()
    timeout_result = await mcp.call_tool("bernstein_subscribe", {"role": "backend", "timeout_seconds": 1})
    assert timeout_result.structured_content == {"notifications": [], "timed_out": True}
    assert time.monotonic() - start < 2
    await cleaned(app)
    waiting = asyncio.create_task(mcp.call_tool("bernstein_subscribe", {"role": "backend", "timeout_seconds": 5}))
    await subscribed()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await cleaned(app)


@pytest.mark.asyncio
async def test_idle_stream_keeps_subscription_and_self_create_notifies(live_server):
    app, url = live_server
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": "Bearer operator-secret"}, timeout=20
    ) as client:
        parent = await client.post("/tasks", json={"title": "Parent", "description": "x", "role": "backend"})
        assert parent.status_code == 201
        async with client.stream("GET", "/events/tasks?role=backend") as response:
            lines = await ready(response)
            async with asyncio.timeout(18):
                # Wait through an idle heartbeat, proving the queue survives.
                while await anext(lines) != "event: heartbeat":
                    pass
            assert app.state.sse_bus.subscriber_count == 1
            child = await client.post(
                "/tasks/self-create",
                json={"parent_task_id": parent.json()["id"], "title": "Child", "description": "x", "role": "backend"},
            )
            assert child.status_code == 201, child.text
            assert (await notification(lines))["task_id"] == child.json()["id"]
        await cleaned(app)
