"""Synchronous HTTP client for the Bernstein task server.

Uses :mod:`httpx` for robust connection pooling and timeout handling.
A matching async client (:class:`AsyncBernsteinClient`) is available for
use inside async frameworks (FastAPI, asyncio services, etc.).

Example (sync)::

    from bernstein_sdk import BernsteinClient

    with BernsteinClient("http://127.0.0.1:8052") as client:
        task = client.create_task(title="Fix login bug", role="backend")
        client.complete_task(task.id, result_summary="Patched null-check in auth.py")

Example (async)::

    from bernstein_sdk.client import AsyncBernsteinClient

    async with AsyncBernsteinClient("http://127.0.0.1:8052") as client:
        task = await client.create_task(title="Add index to users table")
        await client.fail_task(task.id, error="Migration syntax error")
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bernstein_sdk.models import (
    StatusSummary,
    TaskCreate,
    TaskResponse,
    TaskScope,
    TaskComplexity,
    TaskStatus,
)

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class BernsteinClient:
    """Synchronous HTTP client for the Bernstein task server.

    Args:
        base_url: Bernstein server URL (default ``http://127.0.0.1:8052``).
        token: Bearer token.  Set ``BERNSTEIN_TOKEN`` env var as an
            alternative to passing it here.
        timeout: Request timeout in seconds, or an :class:`httpx.Timeout`.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8052",
        token: str = "",
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(timeout),
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "BernsteinClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    # ------------------------------------------------------------------
    # Task operations
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        role: str = "backend",
        description: str = "",
        priority: int = 2,
        scope: TaskScope = TaskScope.MEDIUM,
        complexity: TaskComplexity = TaskComplexity.MEDIUM,
        estimated_minutes: int = 30,
        depends_on: list[str] | None = None,
        external_ref: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskResponse:
        """Create a new task on the Bernstein task server.

        Args:
            title: Short imperative description.
            role: Agent role (e.g. ``"backend"``, ``"qa"``).
            description: Full task brief shown to the agent.
            priority: 1 (critical) – 3 (nice-to-have).
            scope: Rough size estimate.
            complexity: Reasoning complexity hint.
            estimated_minutes: Expected wall-clock duration.
            depends_on: Task IDs that must complete first.
            external_ref: Back-reference to the originating issue.
            metadata: Arbitrary key-value pairs attached to the task.

        Returns:
            The created :class:`TaskResponse`.

        Raises:
            httpx.HTTPStatusError: If the server returns a 4xx/5xx response.
        """
        body = TaskCreate(
            title=title,
            role=role,
            description=description,
            priority=priority,
            scope=scope,
            complexity=complexity,
            estimated_minutes=estimated_minutes,
            depends_on=depends_on or [],
            external_ref=external_ref,
            metadata=metadata or {},
        ).to_api_payload()
        resp = self._client.post("/tasks", json=body)
        resp.raise_for_status()
        return TaskResponse.from_api_response(resp.json())

    def get_task(self, task_id: str) -> TaskResponse:
        """Fetch a single task by ID.

        Raises:
            httpx.HTTPStatusError: 404 if the task does not exist.
        """
        resp = self._client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return TaskResponse.from_api_response(resp.json())

    def list_tasks(self, status: TaskStatus | str | None = None) -> list[TaskResponse]:
        """Return all tasks, optionally filtered by *status*.

        Args:
            status: If provided, only tasks in this state are returned.
        """
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = (
                status.value if isinstance(status, TaskStatus) else status
            )
        resp = self._client.get("/tasks", params=params)
        resp.raise_for_status()
        data = resp.json()
        tasks_raw: list[dict[str, Any]] = (
            data if isinstance(data, list) else data.get("tasks", [])
        )
        return [TaskResponse.from_api_response(t) for t in tasks_raw]

    def complete_task(self, task_id: str, result_summary: str = "") -> None:
        """Mark a task as ``done``.

        Args:
            task_id: Task to complete.
            result_summary: Brief description of what was accomplished.
        """
        resp = self._client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": result_summary},
        )
        resp.raise_for_status()

    def fail_task(self, task_id: str, error: str = "") -> None:
        """Mark a task as ``failed``.

        Args:
            task_id: Task to fail.
            error: Error message or failure reason.
        """
        resp = self._client.post(
            f"/tasks/{task_id}/fail",
            json={"error": error},
        )
        resp.raise_for_status()

    def get_status(self) -> StatusSummary:
        """Return aggregate statistics from ``GET /status``."""
        resp = self._client.get("/status")
        resp.raise_for_status()
        return StatusSummary.from_api_response(resp.json())

    def health(self) -> bool:
        """Return ``True`` if the server is reachable and healthy."""
        try:
            resp = self._client.get("/health")
            return resp.is_success
        except httpx.TransportError:
            return False


class AsyncBernsteinClient:
    """Async HTTP client for the Bernstein task server.

    Mirrors :class:`BernsteinClient` but uses :class:`httpx.AsyncClient`.
    Suitable for use inside async frameworks (FastAPI, aiohttp, asyncio).

    Example::

        async with AsyncBernsteinClient("http://127.0.0.1:8052") as client:
            task = await client.create_task(title="Add rate limiting")
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8052",
        token: str = "",
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(timeout),
        )

    async def __aenter__(self) -> "AsyncBernsteinClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP connection pool."""
        await self._client.aclose()

    async def create_task(
        self,
        title: str,
        role: str = "backend",
        description: str = "",
        priority: int = 2,
        scope: TaskScope = TaskScope.MEDIUM,
        complexity: TaskComplexity = TaskComplexity.MEDIUM,
        estimated_minutes: int = 30,
        depends_on: list[str] | None = None,
        external_ref: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskResponse:
        """Create a new task (async version of :meth:`BernsteinClient.create_task`)."""
        body = TaskCreate(
            title=title,
            role=role,
            description=description,
            priority=priority,
            scope=scope,
            complexity=complexity,
            estimated_minutes=estimated_minutes,
            depends_on=depends_on or [],
            external_ref=external_ref,
            metadata=metadata or {},
        ).to_api_payload()
        resp = await self._client.post("/tasks", json=body)
        resp.raise_for_status()
        return TaskResponse.from_api_response(resp.json())

    async def get_task(self, task_id: str) -> TaskResponse:
        resp = await self._client.get(f"/tasks/{task_id}")
        resp.raise_for_status()
        return TaskResponse.from_api_response(resp.json())

    async def list_tasks(
        self, status: TaskStatus | str | None = None
    ) -> list[TaskResponse]:
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = (
                status.value if isinstance(status, TaskStatus) else status
            )
        resp = await self._client.get("/tasks", params=params)
        resp.raise_for_status()
        data = resp.json()
        tasks_raw: list[dict[str, Any]] = (
            data if isinstance(data, list) else data.get("tasks", [])
        )
        return [TaskResponse.from_api_response(t) for t in tasks_raw]

    async def complete_task(self, task_id: str, result_summary: str = "") -> None:
        resp = await self._client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": result_summary},
        )
        resp.raise_for_status()

    async def fail_task(self, task_id: str, error: str = "") -> None:
        resp = await self._client.post(
            f"/tasks/{task_id}/fail",
            json={"error": error},
        )
        resp.raise_for_status()

    async def get_status(self) -> StatusSummary:
        resp = await self._client.get("/status")
        resp.raise_for_status()
        return StatusSummary.from_api_response(resp.json())

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health")
            return resp.is_success
        except httpx.TransportError:
            return False
