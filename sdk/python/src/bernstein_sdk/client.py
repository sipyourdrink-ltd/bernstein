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
    TaskComplexity,
    TaskCreate,
    TaskResponse,
    TaskScope,
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
        # ETag cache: maps cache-key → (etag, cached_json_data)
        self._etag_cache: dict[str, tuple[str, Any]] = {}

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> BernsteinClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def clear_etag_cache(self) -> None:
        """Clear all cached ETags (useful for testing or forced refresh)."""
        self._etag_cache.clear()

    # ------------------------------------------------------------------
    # ETag-aware GET helper
    # ------------------------------------------------------------------

    def _get_conditional(
        self, path: str, params: dict[str, str] | None = None
    ) -> Any:
        """Perform a GET request using If-None-Match / 304 conditional logic.

        On a 304 response the previously cached JSON payload is returned
        without deserialising a new body.  The ETag cache is updated
        whenever the server returns a fresh ``ETag`` response header.

        Args:
            path: URL path relative to ``base_url``.
            params: Optional query parameters.

        Returns:
            Parsed JSON from the server (or from the cache on 304).

        Raises:
            httpx.HTTPStatusError: On 4xx / 5xx responses.
        """
        cache_key = path
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            cache_key = f"{path}?{qs}"

        req_headers: dict[str, str] = {}
        if cache_key in self._etag_cache:
            etag, _ = self._etag_cache[cache_key]
            req_headers["If-None-Match"] = etag

        resp = self._client.get(path, params=params or {}, headers=req_headers)

        if resp.status_code == 304:
            log.debug("ETag cache hit (304): %s", cache_key)
            _, cached_data = self._etag_cache[cache_key]
            return cached_data

        resp.raise_for_status()
        data = resp.json()

        if etag_val := resp.headers.get("ETag"):
            self._etag_cache[cache_key] = (etag_val, data)

        return data

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

        Uses ETag-based conditional requests; returns cached data on 304.

        Raises:
            httpx.HTTPStatusError: 404 if the task does not exist.
        """
        data = self._get_conditional(f"/tasks/{task_id}")
        return TaskResponse.from_api_response(data)

    def list_tasks(self, status: TaskStatus | str | None = None) -> list[TaskResponse]:
        """Return all tasks, optionally filtered by *status*.

        Uses ETag-based conditional requests; returns cached data on 304.

        Args:
            status: If provided, only tasks in this state are returned.
        """
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = (
                status.value if isinstance(status, TaskStatus) else status
            )
        data = self._get_conditional("/tasks", params or None)
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
        """Return aggregate statistics from ``GET /status``.

        Uses ETag-based conditional requests; returns cached data on 304.
        """
        data = self._get_conditional("/status")
        return StatusSummary.from_api_response(data)

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
        # ETag cache: maps cache-key → (etag, cached_json_data)
        self._etag_cache: dict[str, tuple[str, Any]] = {}

    async def __aenter__(self) -> AsyncBernsteinClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP connection pool."""
        await self._client.aclose()

    def clear_etag_cache(self) -> None:
        """Clear all cached ETags (useful for testing or forced refresh)."""
        self._etag_cache.clear()

    async def _get_conditional(
        self, path: str, params: dict[str, str] | None = None
    ) -> Any:
        """Async GET with If-None-Match / 304 conditional logic.

        On a 304 response the previously cached JSON payload is returned
        without deserialising a new body.  The ETag cache is updated
        whenever the server returns a fresh ``ETag`` response header.
        """
        cache_key = path
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            cache_key = f"{path}?{qs}"

        req_headers: dict[str, str] = {}
        if cache_key in self._etag_cache:
            etag, _ = self._etag_cache[cache_key]
            req_headers["If-None-Match"] = etag

        resp = await self._client.get(path, params=params or {}, headers=req_headers)

        if resp.status_code == 304:
            log.debug("ETag cache hit (304): %s", cache_key)
            _, cached_data = self._etag_cache[cache_key]
            return cached_data

        resp.raise_for_status()
        data = resp.json()

        if etag_val := resp.headers.get("ETag"):
            self._etag_cache[cache_key] = (etag_val, data)

        return data

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
        """Fetch a single task by ID (async).

        Uses ETag-based conditional requests; returns cached data on 304.
        """
        data = await self._get_conditional(f"/tasks/{task_id}")
        return TaskResponse.from_api_response(data)

    async def list_tasks(
        self, status: TaskStatus | str | None = None
    ) -> list[TaskResponse]:
        """Return all tasks, optionally filtered by *status* (async).

        Uses ETag-based conditional requests; returns cached data on 304.
        """
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = (
                status.value if isinstance(status, TaskStatus) else status
            )
        data = await self._get_conditional("/tasks", params or None)
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
        """Return aggregate statistics from ``GET /status`` (async).

        Uses ETag-based conditional requests; returns cached data on 304.
        """
        data = await self._get_conditional("/status")
        return StatusSummary.from_api_response(data)

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health")
            return resp.is_success
        except httpx.TransportError:
            return False
