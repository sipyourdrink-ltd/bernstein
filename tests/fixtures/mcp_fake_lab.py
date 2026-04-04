"""MCP fake lab: in-memory Bernstein task server for unit tests (T602).

Wires ``create_mcp_server`` to a fake ``httpx`` transport so every MCP tool
can be exercised without a running Bernstein server or real network calls.

Usage::

    from tests.fixtures.mcp_fake_lab import McpFakeLab

    async def test_run_creates_task():
        lab = McpFakeLab()
        text = await lab.call_tool("bernstein_run", {"goal": "Do X", "role": "qa"})
        assert "fake-" in text
        lab.assert_called("POST", "/tasks")

    async def test_tasks_returns_seeded_task():
        lab = McpFakeLab()
        lab.seed_task("T-001", title="Fix bug", role="backend")
        text = await lab.call_tool("bernstein_tasks", {})
        assert "T-001" in text
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import patch

import httpx

# ---------------------------------------------------------------------------
# Default data helpers
# ---------------------------------------------------------------------------


def _default_status() -> dict[str, Any]:
    return {
        "total": 0,
        "open": 0,
        "claimed": 0,
        "done": 0,
        "failed": 0,
        "per_role": [],
        "total_cost_usd": 0.0,
    }


def _make_task_dict(
    task_id: str,
    title: str = "Fake task",
    status: str = "open",
    role: str = "backend",
    description: str = "",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    estimated_minutes: int = 30,
    result_summary: str | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    """Build a minimal task dict matching the Bernstein task server schema."""
    return {
        "id": task_id,
        "title": title,
        "description": description or title,
        "role": role,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
        "estimated_minutes": estimated_minutes,
        "status": status,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": result_summary,
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
# Fake httpx transport — routes to in-memory state
# ---------------------------------------------------------------------------

_COMPLETE_RE = re.compile(r"^/tasks/(.+)/complete$")
_FAIL_RE = re.compile(r"^/tasks/(.+)/fail$")


class _BernsteinFakeTransport(httpx.AsyncBaseTransport):
    """httpx async transport that simulates the Bernstein task server in memory.

    Operates on the data structures passed at construction time so it does not
    need to access private attributes of :class:`McpFakeLab`.

    Args:
        requests: List to append each incoming request to.
        tasks: Shared task store (mutated on POST /tasks and complete/fail).
        status_data: Fixed payload returned for GET /status.
    """

    def __init__(
        self,
        requests: list[httpx.Request],
        tasks: dict[str, dict[str, Any]],
        status_data: dict[str, Any],
    ) -> None:
        self._requests = requests
        self._tasks = tasks
        self._status_data = status_data

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._requests.append(request)
        method = request.method
        path = request.url.path

        # GET /status
        if method == "GET" and path == "/status":
            return httpx.Response(200, json=self._status_data)

        # GET /tasks[?status=...]
        if method == "GET" and path == "/tasks":
            status_filter = request.url.params.get("status")
            tasks = list(self._tasks.values())
            if status_filter:
                tasks = [t for t in tasks if t["status"] == status_filter]
            return httpx.Response(200, json=tasks)

        # POST /tasks — create new task
        if method == "POST" and path == "/tasks":
            body: dict[str, Any] = json.loads(request.content) if request.content else {}
            task_id = f"fake-{len(self._tasks) + 1:04d}"
            task = _make_task_dict(task_id=task_id, **body)
            self._tasks[task_id] = task
            return httpx.Response(201, json=task)

        # POST /tasks/{id}/complete
        m_complete = _COMPLETE_RE.match(path)
        if method == "POST" and m_complete:
            task_id = m_complete.group(1)
            if task_id not in self._tasks:
                return httpx.Response(404, json={"error": f"task {task_id!r} not found"})
            body = json.loads(request.content) if request.content else {}
            self._tasks[task_id]["status"] = "done"
            self._tasks[task_id]["result_summary"] = body.get("result_summary")
            return httpx.Response(200, json=self._tasks[task_id])

        # POST /tasks/{id}/fail
        m_fail = _FAIL_RE.match(path)
        if method == "POST" and m_fail:
            task_id = m_fail.group(1)
            if task_id not in self._tasks:
                return httpx.Response(404, json={"error": f"task {task_id!r} not found"})
            body = json.loads(request.content) if request.content else {}
            self._tasks[task_id]["status"] = "failed"
            self._tasks[task_id]["result_summary"] = body.get("result_summary")
            return httpx.Response(200, json=self._tasks[task_id])

        return httpx.Response(404, json={"error": f"fake lab: unhandled {method} {path}"})


# ---------------------------------------------------------------------------
# McpFakeLab
# ---------------------------------------------------------------------------

_FAKE_SERVER_URL = "http://bernstein-fake"


class McpFakeLab:
    """Test harness for exercising Bernstein MCP tools in isolation.

    Seeds an in-memory task store, injects it into ``create_mcp_server`` via
    a fake ``httpx`` transport, and exposes ``call_tool`` so tests can drive
    every tool without a real Bernstein server or real network calls.

    All HTTP requests made during ``call_tool`` are recorded in
    :attr:`requests` for post-call verification.

    Example::

        async def test_status_reflects_seeded_tasks():
            lab = McpFakeLab()
            lab.seed_task("T-001", status="open")
            lab.seed_task("T-002", status="done")
            lab.seed_status(total=2, open=1, done=1)
            text = await lab.call_tool("bernstein_status", {})
            assert "open" in text

    Args:
        server_url: URL passed to ``create_mcp_server``.  Defaults to a
            placeholder that is never actually contacted.
    """

    def __init__(self, server_url: str = _FAKE_SERVER_URL) -> None:
        self._server_url = server_url
        self._tasks: dict[str, dict[str, Any]] = {}
        self._status_data: dict[str, Any] = _default_status()
        self._requests: list[httpx.Request] = []

    # ------------------------------------------------------------------
    # State seeding
    # ------------------------------------------------------------------

    def seed_task(
        self,
        task_id: str,
        status: str = "open",
        title: str = "Fake task",
        role: str = "backend",
        **kwargs: Any,
    ) -> None:
        """Pre-load a task into the fake server.

        Args:
            task_id: Task identifier (e.g. ``"T-001"``).
            status: Initial task status.
            title: Task title.
            role: Assignee role.
            **kwargs: Additional fields merged into the task dict.
        """
        self._tasks[task_id] = _make_task_dict(
            task_id=task_id,
            status=status,
            title=title,
            role=role,
            **kwargs,
        )

    def seed_status(self, **kwargs: Any) -> None:
        """Override fields in the ``/status`` response.

        Args:
            **kwargs: Fields to update (e.g. ``total=5, open=2, done=3``).
        """
        self._status_data.update(kwargs)

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Invoke *tool_name* via the real FastMCP server with the fake transport.

        Patches ``httpx.AsyncClient`` in ``bernstein.mcp.server`` so every
        HTTP call hits the in-memory transport instead of the network.

        Args:
            tool_name: MCP tool name (e.g. ``"bernstein_run"``).
            args: Tool arguments dict.

        Returns:
            The first text content item from the tool's response.

        Raises:
            httpx.HTTPStatusError: If the fake transport returns a non-2xx
                response and the tool calls ``raise_for_status()``.
        """
        from bernstein.mcp.server import create_mcp_server

        transport = _BernsteinFakeTransport(
            requests=self._requests,
            tasks=self._tasks,
            status_data=self._status_data,
        )
        # Capture the real class before patching so _make_client doesn't recurse.
        _RealAsyncClient = httpx.AsyncClient

        def _make_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return _RealAsyncClient(transport=transport)

        mcp = create_mcp_server(server_url=self._server_url)
        with patch("bernstein.mcp.server.httpx.AsyncClient", side_effect=_make_client):
            result = await mcp.call_tool(tool_name, args)

        # FastMCP returns a list of lists of content objects
        return result[0][0].text  # type: ignore[index]

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    @property
    def requests(self) -> list[httpx.Request]:
        """All HTTP requests captured during this lab session, in order."""
        return list(self._requests)

    def assert_called(self, method: str, path: str) -> None:
        """Assert that at least one request with *method* and *path* was made.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, …).
            path: URL path (e.g. ``"/tasks"``).

        Raises:
            AssertionError: If no matching request was found.
        """
        matches = [
            r for r in self._requests if r.method == method and r.url.path == path
        ]
        if not matches:
            made = [(r.method, r.url.path) for r in self._requests]
            msg = f"Expected {method} {path} but requests were: {made}"
            raise AssertionError(msg)

    def assert_not_called(self, method: str, path: str) -> None:
        """Assert that no request with *method* and *path* was made.

        Args:
            method: HTTP method.
            path: URL path.

        Raises:
            AssertionError: If a matching request was found.
        """
        matches = [
            r for r in self._requests if r.method == method and r.url.path == path
        ]
        if matches:
            msg = f"Expected no {method} {path} but found {len(matches)} request(s)"
            raise AssertionError(msg)
