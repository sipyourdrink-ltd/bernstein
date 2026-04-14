"""Bernstein MCP server.

Exposes Bernstein's orchestration layer as MCP tools so any MCP client
(Cursor, Claude Code, Cline, Windsurf, …) can drive multi-agent work
through Bernstein.

Transport:
    stdio  — for local IDE integration (default ``bernstein mcp``)
    sse    — for remote/web integration (``bernstein mcp --transport sse``)

Tools:
    bernstein_run     — start an orchestration run with a goal
    bernstein_status  — get task counts summary
    bernstein_tasks   — list tasks with optional status filter
    bernstein_cost    — get cost summary across all roles
    bernstein_stop    — graceful shutdown (writes SHUTDOWN signal)
    bernstein_approve — approve a pending/blocked task
    bernstein_health  — liveness check (always succeeds)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"

# Timeout for all httpx calls to the task server (seconds).
_HTTP_TIMEOUT = 5.0

logger = logging.getLogger(__name__)


def _error_response(exc: Exception, *, hint: str = "Task server may be restarting") -> str:
    """Return a JSON error string instead of letting the exception propagate.

    This keeps the MCP server alive — a crashed tool handler on stdio
    transport means all Bernstein tools are lost for the rest of the
    agent session (no reconnect).
    """
    logger.warning("MCP tool error: %s", exc)
    return json.dumps({"error": str(exc), "hint": hint})


def _register_health_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_health`` liveness-check tool."""

    @mcp.tool()
    async def bernstein_health(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Liveness check — always succeeds if the MCP server is running.

        Use this to verify the Bernstein MCP connection is still alive.

        Returns:
            JSON with status "ok".
        """
        return json.dumps({"status": "ok"})


def _register_query_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register read-only query tools: run, status, tasks, cost."""

    @mcp.tool()
    async def bernstein_run(  # pyright: ignore[reportUnusedFunction]
        goal: str,
        role: str = "backend",
        priority: int = 2,
        scope: str = "medium",
        complexity: str = "medium",
        estimated_minutes: int = 30,
    ) -> str:
        """Start an orchestration run by posting a task to the Bernstein server.

        Args:
            goal: Description of what you want Bernstein to accomplish.
            role: Specialist role to assign (backend, frontend, qa, security, …).
            priority: 1=critical, 2=normal, 3=nice-to-have.
            scope: Task scope — small, medium, or large.
            complexity: Task complexity — low, medium, or high.
            estimated_minutes: Rough time estimate in minutes.

        Returns:
            JSON with the created task ID, title, and status.
        """
        try:
            payload: dict[str, Any] = {
                "title": goal[:120],
                "description": goal,
                "role": role,
                "priority": priority,
                "scope": scope,
                "complexity": complexity,
                "estimated_minutes": estimated_minutes,
            }
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(f"{server_url}/tasks", json=payload)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {"task_id": data["id"], "title": data["title"], "status": data["status"]},
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_status(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Return a summary of all task counts from the Bernstein server.

        Returns:
            JSON with total, open, claimed, done, failed counts plus
            a per-role breakdown.
        """
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(f"{server_url}/status")
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(data, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_tasks(  # pyright: ignore[reportUnusedFunction]
        status: str | None = None,
    ) -> str:
        """List tasks from the Bernstein server.

        Args:
            status: Optional filter — open, claimed, in_progress, done,
                failed, blocked, or cancelled.

        Returns:
            JSON array of task objects.
        """
        try:
            params: dict[str, str] = {}
            if status:
                params["status"] = status
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(f"{server_url}/tasks", params=params)
                resp.raise_for_status()
                data: list[dict[str, Any]] = resp.json()
            return json.dumps(data, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_cost(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Return cost summary (total USD spent and per-role breakdown).

        Returns:
            JSON with total_cost_usd and per-role cost breakdown.
        """
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(f"{server_url}/status")
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
            cost_summary: dict[str, Any] = {
                "total_cost_usd": data.get("total_cost_usd", 0.0),
                "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
            }
            return json.dumps(cost_summary, indent=2)
        except Exception as exc:
            return _error_response(exc)


def _register_action_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register mutation tools: stop, approve, create_subtask."""

    @mcp.tool()
    async def bernstein_stop(  # pyright: ignore[reportUnusedFunction]
        workdir: str = ".",
    ) -> str:
        """Request a graceful Bernstein shutdown by writing a SHUTDOWN signal.

        Writes ``.sdd/runtime/signals/SHUTDOWN`` in the project directory,
        which the orchestrator detects and shuts down gracefully.

        Args:
            workdir: Project root directory (default: current directory).

        Returns:
            Confirmation message.
        """
        try:
            signals_dir = Path(workdir) / ".sdd" / "runtime" / "signals"
            signals_dir.mkdir(parents=True, exist_ok=True)
            shutdown_file = signals_dir / "SHUTDOWN"
            shutdown_file.write_text("mcp-stop\n", encoding="utf-8")
            return json.dumps({"status": "shutdown signal sent", "path": str(shutdown_file)})
        except Exception as exc:
            return _error_response(exc, hint="Could not write shutdown signal")

    @mcp.tool()
    async def bernstein_approve(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        note: str = "Approved via MCP",
    ) -> str:
        """Approve a pending or blocked task, marking it complete.

        This is used for approval gates — when a task is awaiting human
        sign-off before proceeding.

        Args:
            task_id: ID of the task to approve.
            note: Optional approval note recorded as the result summary.

        Returns:
            JSON with the updated task status.
        """
        try:
            payload: dict[str, Any] = {"result_summary": note}
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(f"{server_url}/tasks/{task_id}/complete", json=payload)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {"task_id": data["id"], "status": data["status"], "result_summary": data.get("result_summary")},
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_create_subtask(  # pyright: ignore[reportUnusedFunction]
        parent_task_id: str,
        goal: str,
        role: str = "auto",
        priority: int = 2,
        scope: str = "medium",
        complexity: str = "medium",
        estimated_minutes: int | None = None,
    ) -> str:
        """Create a subtask linked to a parent task.

        Agents call this to decompose their current work into subtasks
        during execution.  The parent task is automatically transitioned
        to WAITING_FOR_SUBTASKS status.

        Args:
            parent_task_id: ID of the parent task that this subtask belongs to.
            goal: Description of what the subtask should accomplish.
            role: Specialist role to assign (backend, frontend, qa, …).
            priority: 1=critical, 2=normal, 3=nice-to-have.
            scope: Task scope — small, medium, or large.
            complexity: Task complexity — low, medium, or high.
            estimated_minutes: Rough time estimate in minutes.

        Returns:
            JSON with the created subtask ID, parent_task_id, title, and status.
        """
        try:
            payload: dict[str, Any] = {
                "parent_task_id": parent_task_id,
                "title": goal[:120],
                "description": goal,
                "role": role,
                "priority": priority,
                "scope": scope,
                "complexity": complexity,
            }
            if estimated_minutes is not None:
                payload["estimated_minutes"] = estimated_minutes
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(f"{server_url}/tasks/self-create", json=payload)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {
                    "task_id": data["id"],
                    "parent_task_id": data.get("parent_task_id", parent_task_id),
                    "title": data["title"],
                    "status": data["status"],
                },
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)


def create_mcp_server(
    server_url: str = _DEFAULT_SERVER_URL,
    name: str = "bernstein",
) -> FastMCP[None]:
    """Build and return the Bernstein FastMCP server instance.

    Args:
        server_url: Base URL of the Bernstein task server.
        name: MCP server name advertised to clients.

    Returns:
        Configured FastMCP instance with all Bernstein tools registered.
    """
    mcp: FastMCP[None] = FastMCP(name)
    _register_health_tool(mcp)
    _register_query_tools(mcp, server_url)
    _register_action_tools(mcp, server_url)
    return mcp


def run_stdio(server_url: str = _DEFAULT_SERVER_URL) -> None:
    """Start the MCP server in stdio transport mode (for local IDE integration).

    Args:
        server_url: Bernstein task server URL.
    """
    mcp = create_mcp_server(server_url=server_url)
    mcp.run(transport="stdio")


def run_sse(server_url: str = _DEFAULT_SERVER_URL, host: str = "127.0.0.1", port: int = 8053) -> None:
    """Start the MCP server in SSE transport mode (for remote/web integration).

    Args:
        server_url: Bernstein task server URL.
        host: Host to bind the SSE server to.
        port: Port to bind the SSE server to.
    """
    mcp = create_mcp_server(server_url=server_url)
    import uvicorn

    uvicorn.run(mcp.sse_app(), host=host, port=port)
