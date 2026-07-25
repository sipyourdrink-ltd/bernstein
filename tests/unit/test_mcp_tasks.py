"""Tests for the MCP Tasks extension and trace context propagation."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp.client.experimental.task_handlers import ExperimentalTaskHandlers
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import (
    CallToolResult,
    CancelTaskRequest,
    CancelTaskRequestParams,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskPayloadRequest,
    GetTaskPayloadRequestParams,
    GetTaskRequest,
    GetTaskRequestParams,
    GetTaskResult,
    ListTasksRequest,
    ListTasksResult,
    PaginatedRequestParams,
    TextContent,
)

from bernstein.adapters.base import record_artifact_write
from bernstein.core.lineage.spine import LineageSpine, SpineStatus
from bernstein.core.routes.task_crud import create_task
from bernstein.core.server import TaskCreate
from bernstein.core.tasks.models import TaskStatus, TaskType
from bernstein.mcp.server import (
    _get_journal_head,  # pyright: ignore[reportPrivateUsage]
    _project_task_helper,  # pyright: ignore[reportPrivateUsage]
    create_mcp_server,
)

_KEY = b"k" * 32


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_task_dict(
    task_id: str,
    status: str = "open",
    result_summary: str | None = None,
    *,
    created_at: float = 1711574400.0,
    claimed_at: float | None = None,
    completed_at: float | None = None,
    closed_at: float | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": task_id,
        "title": "Test task",
        "description": "A test task description",
        "role": "backend",
        "status": status,
        "created_at": created_at,
        "result_summary": result_summary,
    }
    # Only attach transition timestamps when supplied, mirroring the server
    # payload where an unclaimed/unclosed task omits these keys entirely.
    if claimed_at is not None:
        data["claimed_at"] = claimed_at
    if completed_at is not None:
        data["completed_at"] = completed_at
    if closed_at is not None:
        data["closed_at"] = closed_at
    return data


@pytest.mark.asyncio
async def test_get_journal_head_empty_when_missing() -> None:
    assert _get_journal_head("non-existent-task") == ""


@pytest.mark.asyncio
async def test_project_task_helper() -> None:
    data = _make_task_dict("t-123", status="done", result_summary="Success summary")
    task_obj = _project_task_helper(data)
    assert task_obj.taskId == "t-123"
    assert task_obj.status == "completed"
    assert task_obj.statusMessage == "Success summary"


@pytest.mark.asyncio
async def test_get_task_endpoint(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[GetTaskRequest]  # pyright: ignore[reportPrivateUsage]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-abc", status="in_progress"))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = GetTaskRequest(params=GetTaskRequestParams(taskId="task-abc"))
        server_res = await handler(req)
        res = server_res.root
        assert isinstance(res, GetTaskResult)
        assert res.taskId == "task-abc"
        assert res.status == "working"
        assert res.statusMessage == "Task is running"


@pytest.mark.asyncio
async def test_get_task_result_endpoint(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[GetTaskPayloadRequest]  # pyright: ignore[reportPrivateUsage]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-abc", status="done", result_summary="Done task"))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = GetTaskPayloadRequest(params=GetTaskPayloadRequestParams(taskId="task-abc"))
        server_res = await handler(req)
        res = server_res.root
        assert isinstance(res, CallToolResult)
        assert not res.isError
        first_content = res.content[0]
        assert isinstance(first_content, TextContent)
        assert first_content.text == "Done task"


@pytest.mark.asyncio
async def test_list_tasks_endpoint(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[ListTasksRequest]  # pyright: ignore[reportPrivateUsage]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={
            "tasks": [
                _make_task_dict("task-1", status="done"),
                _make_task_dict("task-2", status="failed"),
            ],
            "total": 2,
            "limit": 100,
            "offset": 0,
        }
    )
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = ListTasksRequest()
        server_res = await handler(req)
        res = server_res.root
        assert isinstance(res, ListTasksResult)
        assert res.tasks is not None
        assert len(res.tasks) == 2
        assert res.tasks[0].taskId == "task-1"
        assert res.tasks[0].status == "completed"
        assert res.tasks[1].taskId == "task-2"
        assert res.tasks[1].status == "failed"


@pytest.mark.asyncio
async def test_cancel_task_endpoint(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[CancelTaskRequest]  # pyright: ignore[reportPrivateUsage]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-abc", status="cancelled"))
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = CancelTaskRequest(params=CancelTaskRequestParams(taskId="task-abc"))
        server_res = await handler(req)
        res = server_res.root
        assert isinstance(res, CancelTaskResult)
        assert res.taskId == "task-abc"
        assert res.status == "cancelled"


@pytest.mark.asyncio
async def test_bernstein_run_task_augmented_forwards_trace_context(mock_client: AsyncMock) -> None:
    from mcp.types import CallToolRequest, CallToolRequestParams

    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[CallToolRequest]  # pyright: ignore[reportPrivateUsage]

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-tasks-support", status="open"))
    mock_client.post = AsyncMock(return_value=mock_response)

    # This call is task-augmented: the client sent task metadata, which is
    # what makes a CreateTaskResult the correct response shape.
    mock_experimental = MagicMock()
    mock_experimental.is_task = True
    mock_experimental.client_supports_tasks = True

    mock_meta = MagicMock()
    mock_meta.model_extra = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "state-xyz",
        "baggage": "baggage-abc",
    }

    mock_request_context = MagicMock()
    mock_request_context.experimental = mock_experimental
    mock_request_context.meta = mock_meta

    # Set request_context on low-level server using contextvar
    from mcp.server.lowlevel.server import request_ctx

    token = request_ctx.set(mock_request_context)

    try:
        with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
            req = CallToolRequest(params=CallToolRequestParams(name="bernstein_run", arguments={"goal": "Task run"}))
            server_res = await handler(req)
            res = server_res.root
    finally:
        request_ctx.reset(token)

    assert isinstance(res, CreateTaskResult)
    assert res.task.taskId == "task-tasks-support"
    assert res.task.status == "working"

    # Assert trace context headers were forwarded
    headers = cast(dict[str, Any], mock_client.post.call_args.kwargs.get("headers") or {})
    assert headers.get("traceparent") == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert headers.get("tracestate") == "state-xyz"
    assert headers.get("baggage") == "baggage-abc"


@pytest.mark.asyncio
async def test_trace_context_propagation_to_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only orchestrator-controlled trace context is sealed (issue #2787): a
    # bernstein-prefixed baggage member survives and marks the context as ours,
    # so traceparent/tracestate are recorded alongside it.
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    monkeypatch.setenv("TRACEPARENT", "00-abc-123-01")
    monkeypatch.setenv("TRACESTATE", "state-abc")
    monkeypatch.setenv("BAGGAGE", "bernstein-task=t1")

    root = tmp_path / "lineage"
    h = record_artifact_write(
        artifact_path="src/bar.py",
        content=b"test",
        actor="agent:test",
        step_id="tc-99",
        model="claude",
        lineage_root=root,
        run_id="run-trace-01",
        hmac_key=_KEY,
        timestamp=1,
    )
    assert h

    spine = LineageSpine(root, run_id="run-trace-01", hmac_key=_KEY)
    entries = list(spine.iter_entries())
    assert len(entries) == 1
    assert entries[0].traceparent == "00-abc-123-01"
    assert entries[0].tracestate == "state-abc"
    assert entries[0].baggage == "bernstein-task=t1"

    result = spine.verify()
    assert result.status is SpineStatus.OK


@pytest.mark.asyncio
async def test_create_task_endpoint_ingests_trace_headers() -> None:
    # Mock FastAPI request
    mock_request = MagicMock()
    mock_request.app.state.sdd_dir = Path("/tmp")
    mock_request.app.state.tenant_isolation_manager.check_quota.return_value = (True, "OK")
    mock_request.app.state.seed_config = None
    mock_request.headers = {
        "traceparent": "00-abc-123-01",
        "tracestate": "state-abc",
        "baggage": "bag-abc",
        "x-tenant-id": "default",
    }

    mock_store = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-mock-id"
    mock_task.status = TaskStatus.OPEN
    mock_store.create = AsyncMock(return_value=mock_task)
    mock_store.count_by_status.return_value = {"total": 0}

    body = TaskCreate(
        title="Test",
        description="Desc",
        role="backend",
        task_type=TaskType.STANDARD.value,
    )

    with (
        patch("bernstein.core.routes.task_crud._get_store", return_value=mock_store),
        patch("bernstein.core.routes.task_crud._get_sse_bus", return_value=MagicMock()),
        patch("bernstein.core.routes.task_crud.get_plugin_manager", return_value=MagicMock()),
        patch("bernstein.core.routes.task_crud.append_assessment_log", return_value=None),
        patch("bernstein.core.routes.task_crud.task_to_response", return_value=MagicMock()),
    ):
        await create_task(body, mock_request)
        created_task_body = mock_store.create.call_args[0][0]
        assert created_task_body.metadata.get("traceparent") == "00-abc-123-01"
        assert created_task_body.metadata.get("tracestate") == "state-abc"
        assert created_task_body.metadata.get("baggage") == "bag-abc"


@pytest.mark.parametrize(
    ("task_status", "expected_mcp_status"),
    [
        # Terminal statuses must NOT project to "working" or a spec client polls forever.
        ("done", "completed"),
        ("closed", "completed"),
        ("failed", "failed"),
        ("refused", "failed"),
        ("abandoned", "failed"),
        ("cancelled", "cancelled"),
        # Non-terminal / needs-input.
        ("open", "working"),
        ("in_progress", "working"),
        ("waiting_for_subtasks", "working"),
        ("orphaned", "working"),
        ("blocked", "input_required"),
        ("pending_approval", "input_required"),
        ("planned", "input_required"),
        ("blocked_by_abandon", "input_required"),
    ],
)
def test_project_task_helper_maps_all_terminal_statuses(task_status: str, expected_mcp_status: str) -> None:
    task_obj = _project_task_helper(_make_task_dict("t-map", status=task_status))
    assert task_obj.status == expected_mcp_status


@pytest.mark.asyncio
@pytest.mark.parametrize("error_status", ["failed", "refused", "abandoned", "orphaned", "blocked_by_abandon"])
async def test_get_task_result_signals_error_for_terminal_error_states(
    mock_client: AsyncMock, error_status: str
) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[GetTaskPayloadRequest]  # pyright: ignore[reportPrivateUsage]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-err", status=error_status))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = GetTaskPayloadRequest(params=GetTaskPayloadRequestParams(taskId="task-err"))
        res = (await handler(req)).root
        assert isinstance(res, CallToolResult)
        assert res.isError is True


@pytest.mark.asyncio
async def test_get_task_result_guards_non_terminal_task(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[GetTaskPayloadRequest]  # pyright: ignore[reportPrivateUsage]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-inflight", status="in_progress"))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        req = GetTaskPayloadRequest(params=GetTaskPayloadRequestParams(taskId="task-inflight"))
        res = (await handler(req)).root
        assert isinstance(res, CallToolResult)
        # An in-flight task has no result: signal error, never fabricate "Task completed".
        assert res.isError is True
        first_content = res.content[0]
        assert isinstance(first_content, TextContent)
        assert "still" in first_content.text


def test_spawn_for_tasks_trace_env_is_scoped_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task's W3C trace-context must not leak into the next task's environment.

    Regression for the cross-task lineage-attribution leak: the spawn must set
    the trace env only for the duration of its own spawn and restore it after, so
    a task without trace-context can never inherit a previous task's traceparent.
    """
    import os

    from bernstein.core.agents.spawner_core import AgentSpawner

    spawner = AgentSpawner.__new__(AgentSpawner)  # bypass heavy __init__
    seen: dict[str, str | None] = {}

    def _fake_internal(tasks: Any, model_override: str | None = None) -> Any:
        seen["TRACEPARENT"] = os.environ.get("TRACEPARENT")
        return MagicMock()

    spawner._spawn_for_tasks_internal = _fake_internal  # type: ignore[method-assign]

    for key in ("TRACEPARENT", "TRACESTATE", "BAGGAGE"):
        monkeypatch.delenv(key, raising=False)

    task_a = MagicMock()
    task_a.metadata = {"traceparent": "tp-A", "tracestate": "ts-A", "baggage": "bg-A"}
    task_a.role = "backend"
    spawner.spawn_for_tasks([task_a])
    assert seen["TRACEPARENT"] == "tp-A"  # A's own subprocess sees A's trace
    assert os.environ.get("TRACEPARENT") is None  # restored after the spawn

    task_b = MagicMock()
    task_b.metadata = {}  # no trace-context
    task_b.role = "backend"
    spawner.spawn_for_tasks([task_b])
    assert seen["TRACEPARENT"] is None  # B must NOT inherit A's traceparent
    assert os.environ.get("TRACEPARENT") is None


# ---------------------------------------------------------------------------
# lastUpdatedAt determinism (a projection of an unchanged task must be idempotent)
# ---------------------------------------------------------------------------


def test_project_task_helper_last_updated_derived_from_last_transition() -> None:
    """lastUpdatedAt must reflect the task's newest stored transition timestamp.

    A closed task's last change is ``closed_at``; the projection must surface
    that instant, not wall-clock ``now()``.
    """
    created = 1711574400.0
    data = _make_task_dict(
        "t-ts",
        status="closed",
        created_at=created,
        claimed_at=created + 60,
        completed_at=created + 120,
        closed_at=created + 180,
    )
    task_obj = _project_task_helper(data)
    assert task_obj.createdAt == datetime.fromtimestamp(created, tz=UTC)
    assert task_obj.lastUpdatedAt == datetime.fromtimestamp(created + 180, tz=UTC)


def test_project_task_helper_last_updated_is_deterministic() -> None:
    """Two projections of the same unchanged task return identical timestamps.

    Non-idempotent ``lastUpdatedAt`` produces phantom updates for
    change-detection clients and breaks the deterministic-substrate contract.
    """
    data = _make_task_dict("t-idem", status="claimed", created_at=1711574400.0, claimed_at=1711574460.0)
    first = _project_task_helper(data).lastUpdatedAt
    second = _project_task_helper(data).lastUpdatedAt
    assert first == second
    # The stable value is the newest transition (claimed_at here), not now().
    assert first == datetime.fromtimestamp(1711574460.0, tz=UTC)


def test_project_task_helper_last_updated_falls_back_to_created_at() -> None:
    """A freshly-opened task (no later transitions) reports lastUpdatedAt == createdAt."""
    data = _make_task_dict("t-open", status="open", created_at=1711574400.0)
    task_obj = _project_task_helper(data)
    assert task_obj.lastUpdatedAt == task_obj.createdAt
    assert task_obj.lastUpdatedAt == datetime.fromtimestamp(1711574400.0, tz=UTC)


def test_task_response_exposes_completion_timestamps() -> None:
    """The task API must serialise completed_at/closed_at so the MCP layer can
    derive a deterministic lastUpdatedAt for terminal tasks."""
    from bernstein.core.server.server_app import task_to_response
    from bernstein.core.tasks.models import Task, TaskStatus

    task = Task(
        id="t-serialise",
        title="t",
        description="",
        role="backend",
        status=TaskStatus.CLOSED,
        batch_eligible=False,
        claimed_at=111.0,
        completed_at=222.0,
        closed_at=333.0,
    )
    resp = task_to_response(task)
    assert resp.completed_at == 222.0
    assert resp.closed_at == 333.0


# ---------------------------------------------------------------------------
# list_tasks pagination (cursor in -> limit/offset out -> nextCursor)
# ---------------------------------------------------------------------------


def _paginated_envelope(tasks: list[dict[str, Any]], *, total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"tasks": tasks, "total": total, "limit": limit, "offset": offset}


@pytest.mark.asyncio
async def test_list_tasks_requests_pagination_and_sets_next_cursor(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[ListTasksRequest]  # pyright: ignore[reportPrivateUsage]

    page = [_make_task_dict(f"task-{i}", status="open") for i in range(100)]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_paginated_envelope(page, total=250, limit=100, offset=0))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        res = (await handler(ListTasksRequest())).root
        assert isinstance(res, ListTasksResult)

    # The handler must send explicit pagination so the server returns the
    # envelope path instead of the legacy list hard-capped at 500.
    sent_params = cast(dict[str, Any], mock_client.get.call_args.kwargs.get("params") or {})
    assert sent_params.get("limit") is not None
    assert sent_params.get("offset") == 0
    assert len(res.tasks) == 100
    # 250 total, only 100 returned -> the client can page further.
    assert res.nextCursor is not None


@pytest.mark.asyncio
async def test_list_tasks_no_next_cursor_at_tail(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[ListTasksRequest]  # pyright: ignore[reportPrivateUsage]

    page = [_make_task_dict("task-only", status="done")]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_paginated_envelope(page, total=1, limit=100, offset=0))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        res = (await handler(ListTasksRequest())).root
        assert isinstance(res, ListTasksResult)

    assert len(res.tasks) == 1
    assert res.nextCursor is None


@pytest.mark.asyncio
async def test_list_tasks_cursor_translates_to_offset(mock_client: AsyncMock) -> None:
    mcp = create_mcp_server()
    handler = mcp._mcp_server.request_handlers[ListTasksRequest]  # pyright: ignore[reportPrivateUsage]

    # Page 1: 100 of 150 -> yields a cursor pointing past the first page.
    page1 = [_make_task_dict(f"task-{i}") for i in range(100)]
    resp1 = MagicMock()
    resp1.raise_for_status = MagicMock()
    resp1.json = MagicMock(return_value=_paginated_envelope(page1, total=150, limit=100, offset=0))
    mock_client.get = AsyncMock(return_value=resp1)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        res1 = (await handler(ListTasksRequest())).root
        assert isinstance(res1, ListTasksResult)
        cursor = res1.nextCursor
        assert cursor is not None

        # Page 2: feed the cursor back; the handler must request offset=100.
        page2 = [_make_task_dict(f"task-{i}") for i in range(100, 150)]
        resp2 = MagicMock()
        resp2.raise_for_status = MagicMock()
        resp2.json = MagicMock(return_value=_paginated_envelope(page2, total=150, limit=100, offset=100))
        mock_client.get = AsyncMock(return_value=resp2)

        res2 = (await handler(ListTasksRequest(params=PaginatedRequestParams(cursor=cursor)))).root
        assert isinstance(res2, ListTasksResult)

    sent_params = cast(dict[str, Any], mock_client.get.call_args.kwargs.get("params") or {})
    assert sent_params.get("offset") == 100
    assert len(res2.tasks) == 50
    # 100 + 100 >= 150 -> tail reached.
    assert res2.nextCursor is None


# ---------------------------------------------------------------------------
# Tasks-extension gating and declaration (issue #3079)
# ---------------------------------------------------------------------------


async def _stub_list_tasks(context: Any, params: Any) -> ListTasksResult:
    """Client-side ``tasks/list`` handler used only to declare the capability."""
    return ListTasksResult(tasks=[])


@asynccontextmanager
async def _tasks_capable_session(mcp: FastMCP[None]) -> AsyncGenerator[ClientSession, None]:
    """Yield an in-memory client session that declares the tasks capability.

    ``mcp.shared.memory.create_connected_server_and_client_session`` builds a
    session with no experimental task handlers, so it advertises no ``tasks``
    capability and cannot distinguish the two predicates under test. The SDK
    derives ``ClientTasksCapability`` from the configured handlers, so wiring
    one non-default handler is what makes the server see a tasks-capable
    client.
    """
    low = mcp._mcp_server  # pyright: ignore[reportPrivateUsage]
    handlers = ExperimentalTaskHandlers(list_tasks=_stub_list_tasks)
    # Guard the fixture itself: if the SDK stops deriving the capability from
    # the handlers, the gating tests below would silently stop testing gating.
    assert handlers.build_capability() is not None
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: low.run(
                    server_read,
                    server_write,
                    low.create_initialization_options(),
                    raise_exceptions=False,
                )
            )
            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    experimental_task_handlers=handlers,
                ) as session:
                    await session.initialize()
                    yield session
            finally:
                tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_plain_call_from_tasks_capable_client_returns_call_tool_result(
    mock_client: AsyncMock,
) -> None:
    """A plain tools/call must get a CallToolResult even from a tasks client.

    Declaring the tasks capability says the client *can* handle task handles,
    not that this call asked for one. Returning a CreateTaskResult here hands
    the caller a shape it never requested.
    """
    mcp = create_mcp_server()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-plain-call", status="open"))
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        async with _tasks_capable_session(mcp) as session:
            res = await session.call_tool("bernstein_run", {"goal": "Plain tools/call"})

    assert isinstance(res, CallToolResult)
    assert res.isError is False
    assert res.content, "a CallToolResult must carry the tool's payload"
    block = res.content[0]
    assert isinstance(block, TextContent)
    body = json.loads(block.text)
    # The cost meter wraps the tool payload under "result" when it is on.
    if "_meter" in body:
        body = body["result"]
    assert body["task_id"] == "task-plain-call"


@pytest.mark.asyncio
async def test_task_augmented_call_returns_create_task_result(mock_client: AsyncMock) -> None:
    """A tools/call carrying ``task`` metadata must get a CreateTaskResult."""
    mcp = create_mcp_server()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-augmented-call", status="open"))
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        async with _tasks_capable_session(mcp) as session:
            res = await session.experimental.call_tool_as_task(
                "bernstein_run",
                {"goal": "Task augmented tools/call"},
            )

    assert isinstance(res, CreateTaskResult)
    assert res.task.taskId == "task-augmented-call"
    assert res.task.status == "working"


@pytest.mark.asyncio
async def test_tools_list_advertises_task_support() -> None:
    """``execution.taskSupport`` must survive the wire into tools/list."""
    mcp = create_mcp_server()

    async with _tasks_capable_session(mcp) as session:
        listed = await session.list_tools()

    by_name = {tool.name: tool for tool in listed.tools}

    run_tool = by_name["bernstein_run"]
    assert run_tool.execution is not None
    assert run_tool.execution.taskSupport == "optional"

    handle_tool = by_name["bernstein_task_handle"]
    assert handle_tool.execution is not None
    assert handle_tool.execution.taskSupport == "forbidden"

    # A tool with no declared mode advertises nothing, which the extension
    # reads as the "forbidden" default.
    assert by_name["bernstein_health"].execution is None


@pytest.mark.asyncio
async def test_task_row_round_trips_to_a_polling_client(mock_client: AsyncMock) -> None:
    """A ``tasks/get`` row must survive serialisation into a real client.

    ``Task.ttl`` is required and the SDK drops ``None`` fields when it
    serialises a response, so a row built with ``ttl=None`` reaches the
    client without the field and is rejected as malformed. Poll the run
    through a real session so that failure mode cannot come back.
    """
    mcp = create_mcp_server()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=_make_task_dict("task-poll", status="in_progress"))
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        async with _tasks_capable_session(mcp) as session:
            status = await session.experimental.get_task("task-poll")

    assert isinstance(status, GetTaskResult)
    assert status.taskId == "task-poll"
    assert status.status == "working"
    assert status.ttl is not None
