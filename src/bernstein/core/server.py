"""FastAPI task server — central coordination point for all agents.

Agents pull tasks via HTTP, report completion, and send heartbeats.
State is held in-memory and flushed periodically to JSONL for persistence.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.a2a import A2AHandler
from bernstein.core.bulletin import BulletinBoard, MessageType
from bernstein.core.cluster import NodeRegistry
from bernstein.core.models import (
    AgentSession,
    ClusterConfig,
    CompletionSignal,
    NodeInfo,
    ProgressSnapshot,
    RiskAssessment,
    RollbackPlan,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from starlette.responses import Response as StarletteResponse


# ---------------------------------------------------------------------------
# Auth middleware — bearer token validation
# ---------------------------------------------------------------------------

# Paths that are always accessible without auth (health checks, agent card)
_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/.well-known/agent.json",
        "/docs",
        "/openapi.json",
        "/webhooks/github",
        "/dashboard",
        "/dashboard/data",
        "/events",
    }
)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all requests when auth is configured.

    When ``auth_token`` is set, every request must include a matching
    ``Authorization: Bearer <token>`` header. Health and discovery
    endpoints are exempt.
    """

    def __init__(self, app: Any, auth_token: str | None = None) -> None:
        super().__init__(app)
        self._token = auth_token

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        if self._token is None:
            response: StarletteResponse = await call_next(request)
            return response

        path = request.url.path
        if path in _PUBLIC_PATHS:
            response = await call_next(request)
            return response

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )
        token = auth_header[7:]  # Strip "Bearer "
        if token != self._token:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid auth token"},
            )
        response = await call_next(request)
        return response


# Write methods that mutate state
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """Block all write operations when the server is in read-only mode.

    Useful for public demo deployments where the dashboard should be
    visible but task mutation must be disabled entirely.  All GET/HEAD/OPTIONS
    requests pass through; any write method returns 405.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        if request.method in _WRITE_METHODS:
            return JSONResponse(
                status_code=405,
                content={"detail": "Server is in read-only mode"},
                headers={"Allow": "GET, HEAD, OPTIONS"},
            )
        response: StarletteResponse = await call_next(request)
        return response


# ---------------------------------------------------------------------------
# TypedDicts for file-based state records
# ---------------------------------------------------------------------------


class TaskRecord(TypedDict):
    """JSONL record format for persisted tasks."""

    id: str
    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    estimated_minutes: int
    status: str
    task_type: str
    upgrade_details: dict[str, Any] | None
    depends_on: list[str]
    owned_files: list[str]
    assigned_agent: str | None
    result_summary: str | None
    cell_id: str | None
    version: int


class ArchiveRecord(TypedDict):
    """Archive JSONL entry written when a task reaches a terminal state."""

    task_id: str
    title: str
    role: str
    status: str
    created_at: float
    completed_at: float
    duration_seconds: float
    result_summary: str | None
    cost_usd: float | None


class ProgressEntry(TypedDict):
    """Single entry in a task's progress_log."""

    timestamp: float
    message: str
    percent: int


class SnapshotEntry(TypedDict):
    """A single machine-readable progress snapshot for stall detection."""

    timestamp: float
    files_changed: int
    tests_passing: int
    errors: int
    last_file: str


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------

_SIGNAL_TYPE = Literal["path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"]


class CompletionSignalSchema(BaseModel):
    """Pydantic schema for a single completion signal in API requests."""

    type: _SIGNAL_TYPE
    value: str


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    title: str
    description: str
    role: str
    priority: int = 2
    scope: str = "medium"
    complexity: str = "medium"
    estimated_minutes: int = 30
    depends_on: list[str] = Field(default_factory=list)
    owned_files: list[str] = Field(default_factory=list)
    cell_id: str | None = None
    task_type: str = "standard"
    upgrade_details: dict[str, Any] | None = None
    model: str | None = None  # Manager hint: "opus", "sonnet", "haiku"
    effort: str | None = None  # Manager hint: "max", "high", "medium", "low"
    completion_signals: list[CompletionSignalSchema] = Field(default_factory=lambda: list[CompletionSignalSchema]())


class TaskResponse(BaseModel):
    """Serialised task returned by every task endpoint."""

    id: str
    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    estimated_minutes: int
    status: str
    depends_on: list[str]
    owned_files: list[str]
    assigned_agent: str | None
    result_summary: str | None
    cell_id: str | None
    task_type: str
    upgrade_details: dict[str, Any] | None
    model: str | None
    effort: str | None
    completion_signals: list[dict[str, str]] = Field(default_factory=lambda: list[dict[str, str]]())
    created_at: float
    progress_log: list[ProgressEntry] = Field(default_factory=lambda: list[ProgressEntry]())
    version: int = 1


class TaskCompleteRequest(BaseModel):
    """Body for POST /tasks/{task_id}/complete."""

    result_summary: str


class TaskFailRequest(BaseModel):
    """Body for POST /tasks/{task_id}/fail."""

    reason: str = ""


class TaskCancelRequest(BaseModel):
    """Body for POST /tasks/{task_id}/cancel."""

    reason: str = ""


class TaskBlockRequest(BaseModel):
    """Body for POST /tasks/{task_id}/block."""

    reason: str = ""


class TaskProgressRequest(BaseModel):
    """Body for POST /tasks/{task_id}/progress."""

    message: str = ""
    percent: int = 0
    # Structured snapshot fields for stall detection (optional)
    files_changed: int | None = None
    tests_passing: int | None = None
    errors: int | None = None
    last_file: str = ""


class BatchClaimRequest(BaseModel):
    """Body for POST /tasks/claim-batch."""

    task_ids: list[str]
    agent_id: str


class BatchClaimResponse(BaseModel):
    """Response for POST /tasks/claim-batch."""

    claimed: list[str]
    failed: list[str]


class RoleCounts(BaseModel):
    """Per-role open task counts."""

    role: str
    open: int
    claimed: int
    done: int
    failed: int
    cost_usd: float = 0.0


class StatusResponse(BaseModel):
    """Body for GET /status."""

    total: int
    open: int
    claimed: int
    done: int
    failed: int
    per_role: list[RoleCounts]
    total_cost_usd: float = 0.0


class HeartbeatRequest(BaseModel):
    """Body for POST /agents/{agent_id}/heartbeat."""

    role: str = ""
    status: Literal["starting", "working", "idle", "dead"] = "working"


class HeartbeatResponse(BaseModel):
    """Response for heartbeat."""

    agent_id: str
    acknowledged: bool
    server_ts: float


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str
    uptime_s: float
    task_count: int
    agent_count: int


class BulletinPostRequest(BaseModel):
    """Body for POST /bulletin."""

    agent_id: str
    type: MessageType = "status"
    content: str
    cell_id: str | None = None


# -- Cluster schemas -------------------------------------------------------


class NodeCapacitySchema(BaseModel):
    """Advertised capacity of a cluster node."""

    max_agents: int = 6
    available_slots: int = 6
    active_agents: int = 0
    gpu_available: bool = False
    supported_models: list[str] = Field(default_factory=lambda: ["sonnet", "opus", "haiku"])


class NodeRegisterRequest(BaseModel):
    """Body for POST /cluster/nodes."""

    name: str = ""
    url: str = ""
    capacity: NodeCapacitySchema = Field(default_factory=NodeCapacitySchema)
    labels: dict[str, str] = Field(default_factory=dict)
    cell_ids: list[str] = Field(default_factory=list)


class NodeHeartbeatRequest(BaseModel):
    """Body for POST /cluster/nodes/{node_id}/heartbeat."""

    capacity: NodeCapacitySchema | None = None


class NodeResponse(BaseModel):
    """Serialised node in API responses."""

    id: str
    name: str
    url: str
    status: str
    capacity: NodeCapacitySchema
    last_heartbeat: float
    registered_at: float
    labels: dict[str, str]
    cell_ids: list[str]


class ClusterStatusResponse(BaseModel):
    """Response for GET /cluster/status."""

    topology: str
    total_nodes: int
    online_nodes: int
    offline_nodes: int
    total_capacity: int
    available_slots: int
    active_agents: int
    nodes: list[NodeResponse]


class BulletinMessageResponse(BaseModel):
    """Single bulletin message in responses."""

    agent_id: str
    type: str
    content: str
    timestamp: float
    cell_id: str | None


# -- A2A protocol schemas --------------------------------------------------


class A2ATaskSendRequest(BaseModel):
    """Body for POST /a2a/tasks/send — receive a task from an external A2A agent."""

    sender: str
    message: str
    role: str = "backend"


class A2AArtifactRequest(BaseModel):
    """Body for POST /a2a/tasks/{id}/artifacts — attach an artifact."""

    name: str
    data: str = ""
    content_type: str = "text/plain"


class A2AArtifactResponse(BaseModel):
    """Single artifact in responses."""

    name: str
    content_type: str
    data: str
    created_at: float


class A2ATaskResponse(BaseModel):
    """Serialised A2A task in responses."""

    id: str
    bernstein_task_id: str | None
    sender: str
    message: str
    status: str
    artifacts: list[A2AArtifactResponse]
    created_at: float
    updated_at: float


class A2AAgentCardResponse(BaseModel):
    """Agent Card response for /.well-known/agent.json."""

    name: str
    description: str
    capabilities: list[str]
    protocol_version: str
    endpoint: str
    provider: str


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


DEFAULT_ARCHIVE_PATH = Path(".sdd/archive/tasks.jsonl")


class TaskStore:
    """Thread-safe in-memory task store with JSONL persistence.

    All mutations go through this class so the JSONL log stays consistent.
    """

    def __init__(
        self,
        jsonl_path: Path,
        archive_path: Path = DEFAULT_ARCHIVE_PATH,
        metrics_jsonl_path: Path | None = None,
    ) -> None:
        self._tasks: dict[str, Task] = {}
        self._agents: dict[str, AgentSession] = {}
        # Secondary indices for O(1) status/role lookups
        self._by_status: dict[TaskStatus, dict[str, Task]] = {s: {} for s in TaskStatus}
        self._by_role_status: dict[tuple[str, TaskStatus], list[str]] = {}
        # Min-heaps keyed by (role, status) — entries are (priority, task_id)
        # Uses lazy deletion: stale entries are discarded in claim_next()
        self._priority_queues: dict[tuple[str, TaskStatus], list[tuple[int, str]]] = {}
        self._jsonl_path: Path = jsonl_path
        self._archive_path: Path = archive_path
        self._metrics_jsonl_path: Path = (
            metrics_jsonl_path
            if metrics_jsonl_path is not None
            else jsonl_path.parent.parent / "metrics" / "tasks.jsonl"
        )
        self._lock: asyncio.Lock = asyncio.Lock()
        self._write_buffer: list[str] = []
        self._dirty: bool = False
        self._start_ts: float = time.time()
        self._cost_cache: dict[str, float] = {}
        self._cost_cache_mtime: float = 0.0
        self._cost_cache_offset: int = 0
        # In-memory progress snapshots for stall detection (last 10 per task)
        self._progress_snapshots: dict[str, deque[ProgressSnapshot]] = {}

    # -- index helpers -------------------------------------------------------

    def _index_add(self, task: Task) -> None:
        """Add *task* to secondary indices at its current status."""
        self._by_status[task.status][task.id] = task
        key = (task.role, task.status)
        ids = self._by_role_status.setdefault(key, [])
        if task.id not in ids:
            ids.append(task.id)
        if task.status == TaskStatus.OPEN:
            pq = self._priority_queues.setdefault(key, [])
            heapq.heappush(pq, (task.priority, task.id))

    def _index_remove(self, task: Task) -> None:
        """Remove *task* from secondary indices at its current status."""
        self._by_status[task.status].pop(task.id, None)
        ids = self._by_role_status.get((task.role, task.status))
        if ids is not None:
            with contextlib.suppress(ValueError):
                ids.remove(task.id)

    # -- persistence --------------------------------------------------------

    def replay_jsonl(self) -> None:
        """Rebuild state from the JSONL log on disk.

        Each line is a JSON object with at least ``id`` and ``status``.
        Lines are replayed in order so the last write wins.
        """
        if not self._jsonl_path.exists():
            return
        for raw_line in self._jsonl_path.read_text().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record: TaskRecord = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_id: str = record.get("id", "")
            if not task_id:
                continue
            if task_id in self._tasks:
                task = self._tasks[task_id]
                self._index_remove(task)
                task.status = TaskStatus(record.get("status", task.status.value))
                task.assigned_agent = record.get("assigned_agent", task.assigned_agent)
                task.result_summary = record.get("result_summary", task.result_summary)
                self._index_add(task)
            else:
                task = Task.from_dict(cast("dict[str, Any]", record))
                self._tasks[task_id] = task
                self._index_add(task)

    _BUFFER_MAX: int = 10

    async def _flush_buffer_unlocked(self) -> None:
        """Write buffered JSONL records to disk. Caller must hold self._lock."""
        if not self._write_buffer:
            return
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(self._write_buffer)
        self._write_buffer.clear()

        def _write() -> None:
            with self._jsonl_path.open("a") as f:
                f.write(data)

        await asyncio.to_thread(_write)

    async def _append_jsonl(self, record: TaskRecord) -> None:
        """Buffer a JSON record for batch JSONL writing.

        Records accumulate until the buffer reaches _BUFFER_MAX entries, then
        flush to disk in a single write.  Callers must eventually call
        flush_buffer() (e.g. on shutdown) to drain any remaining records.
        """
        line = json.dumps(record, default=str) + "\n"
        self._write_buffer.append(line)
        if len(self._write_buffer) >= self._BUFFER_MAX:
            await self._flush_buffer_unlocked()

    async def flush_buffer(self) -> None:
        """Flush any buffered JSONL records to disk (acquires the store lock)."""
        async with self._lock:
            await self._flush_buffer_unlocked()

    async def _append_archive(self, task: Task, completed_at: float) -> None:
        """Append a completed/failed task record to the archive JSONL."""
        self._archive_path.parent.mkdir(parents=True, exist_ok=True)
        record: ArchiveRecord = {
            "task_id": task.id,
            "title": task.title,
            "role": task.role,
            "status": task.status.value,
            "created_at": task.created_at,
            "completed_at": completed_at,
            "duration_seconds": round(completed_at - task.created_at, 3),
            "result_summary": task.result_summary,
            "cost_usd": None,
        }
        line = json.dumps(record, default=str) + "\n"

        def _write() -> None:
            with self._archive_path.open("a") as f:
                f.write(line)

        await asyncio.to_thread(_write)

    def _task_to_record(self, task: Task) -> TaskRecord:
        """Serialise a Task to a dict suitable for JSONL storage."""
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "role": task.role,
            "priority": task.priority,
            "scope": task.scope.value,
            "complexity": task.complexity.value,
            "estimated_minutes": task.estimated_minutes,
            "status": task.status.value,
            "task_type": task.task_type.value,
            "upgrade_details": asdict(task.upgrade_details) if task.upgrade_details else None,
            "depends_on": task.depends_on,
            "owned_files": task.owned_files,
            "assigned_agent": task.assigned_agent,
            "result_summary": task.result_summary,
            "cell_id": task.cell_id,
            "version": task.version,
        }

    # -- public API ---------------------------------------------------------

    @staticmethod
    def _detect_cycle(tasks: dict[str, Task], new_task: Task) -> list[str] | None:
        """Return the cycle path if adding *new_task* creates a dependency cycle, else None.

        Args:
            tasks: Existing tasks (not yet including new_task).
            new_task: The task about to be inserted.

        Returns:
            A list of task IDs forming the cycle (first == last), or None.
        """
        # Build adjacency map including the new task.
        graph: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks.values()}
        graph[new_task.id] = list(new_task.depends_on)

        # DFS from new_task only — existing tasks were validated on insertion.
        visited: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            if node in path:
                cycle_start = path.index(node)
                return [*path[cycle_start:], node]
            if node in visited:
                return None
            visited.add(node)
            path.append(node)
            for neighbour in graph.get(node, []):
                result = dfs(neighbour)
                if result is not None:
                    return result
            path.pop()
            return None

        return dfs(new_task.id)

    async def create(self, req: TaskCreate) -> Task:
        """Create a new task and persist it.

        Args:
            req: Validated creation request.

        Returns:
            The newly created Task.

        Raises:
            HTTPException: 422 if depends_on references a non-existent task or creates a cycle.
        """
        from bernstein.core.models import Complexity, Scope

        task = Task(
            id=uuid.uuid4().hex[:12],
            title=req.title,
            description=req.description,
            role=req.role,
            priority=req.priority,
            scope=Scope(req.scope),
            complexity=Complexity(req.complexity),
            estimated_minutes=req.estimated_minutes,
            depends_on=req.depends_on,
            owned_files=req.owned_files,
            cell_id=req.cell_id,
            task_type=TaskType(req.task_type),
            upgrade_details=_parse_upgrade_dict(req.upgrade_details),
            model=req.model,
            effort=req.effort,
            completion_signals=[CompletionSignal(type=s.type, value=s.value) for s in req.completion_signals],
        )
        async with self._lock:
            if task.depends_on:
                missing = [dep for dep in task.depends_on if dep not in self._tasks]
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail=f"depends_on references non-existent task(s): {', '.join(missing)}",
                    )
                cycle = self._detect_cycle(self._tasks, task)
                if cycle is not None:
                    raise HTTPException(
                        status_code=422,
                        detail="Circular dependency detected: " + " -> ".join(cycle),
                    )
            self._tasks[task.id] = task
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
        return task

    async def claim_next(self, role: str) -> Task | None:
        """Claim the highest-priority open task for *role*.

        Priority is ascending (1 = critical). Among equal priorities,
        the first inserted task wins (dict insertion order).

        Args:
            role: Agent role to match.

        Returns:
            The claimed Task, or None if nothing is available.
        """
        async with self._lock:
            pq = self._priority_queues.get((role, TaskStatus.OPEN))
            if not pq:
                return None
            task: Task | None = None
            while pq:
                _priority, task_id = heapq.heappop(pq)
                candidate = self._tasks.get(task_id)
                if candidate is not None and candidate.status == TaskStatus.OPEN:
                    task = candidate
                    break
            if task is None:
                return None
            self._index_remove(task)
            task.status = TaskStatus.CLAIMED
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def claim_by_id(self, task_id: str, expected_version: int | None = None) -> Task:
        """Claim a specific task by ID with optional optimistic locking.

        When ``expected_version`` is provided, the claim only succeeds if
        the task's current version matches (compare-and-swap). This
        prevents two nodes from claiming the same task in a distributed
        cluster.

        Args:
            task_id: Task identifier.
            expected_version: If set, CAS — reject if task.version != this.

        Returns:
            The claimed Task.

        Raises:
            KeyError: If task_id does not exist.
            ValueError: If expected_version doesn't match (CAS conflict).
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if expected_version is not None and task.version != expected_version:
                raise ValueError(
                    f"Version conflict: task {task_id} is at version {task.version}, expected {expected_version}"
                )
            if task.status == TaskStatus.OPEN:
                self._index_remove(task)
                task.status = TaskStatus.CLAIMED
                task.version += 1
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))
            return task

    async def claim_batch(self, task_ids: list[str], agent_id: str) -> tuple[list[str], list[str]]:
        """Atomically claim multiple tasks by ID.

        Tasks that are not in OPEN status are skipped and reported as failed.

        Args:
            task_ids: List of task identifiers to claim.
            agent_id: The agent claiming the tasks.

        Returns:
            A tuple of (claimed_ids, failed_ids).
        """
        claimed: list[str] = []
        failed: list[str] = []
        async with self._lock:
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task is None or task.status != TaskStatus.OPEN:
                    failed.append(task_id)
                    continue
                self._index_remove(task)
                task.status = TaskStatus.CLAIMED
                task.assigned_agent = agent_id
                task.version += 1
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))
                claimed.append(task_id)
        return claimed, failed

    async def complete(self, task_id: str, result_summary: str) -> Task:
        """Mark a task as done.

        Args:
            task_id: Task identifier.
            result_summary: Human-readable summary of what was done.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            task.status = TaskStatus.DONE
            task.result_summary = result_summary
            task.version += 1
            self._index_add(task)
            completed_at = time.time()
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            return task

    async def fail(self, task_id: str, reason: str) -> Task:
        """Mark a task as failed.

        Args:
            task_id: Task identifier.
            reason: Why it failed.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            task.status = TaskStatus.FAILED
            task.result_summary = reason
            task.version += 1
            self._index_add(task)
            completed_at = time.time()
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            return task

    async def block(self, task_id: str, reason: str) -> Task:
        """Mark a task as blocked (requires human intervention).

        Args:
            task_id: Task identifier.
            reason: Why the task is blocked.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            task.status = TaskStatus.BLOCKED
            task.result_summary = reason
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def add_progress(self, task_id: str, message: str, percent: int) -> Task:
        """Append an intermediate progress update to a task.

        Args:
            task_id: Task identifier.
            message: Human-readable progress message.
            percent: Completion percentage (0-100).

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            entry: ProgressEntry = {"timestamp": time.time(), "message": message, "percent": percent}
            progress: list[ProgressEntry] = cast("list[ProgressEntry]", task.progress_log)  # type: ignore[reportUnknownMemberType]
            progress.append(entry)
            return task

    def add_snapshot(
        self,
        task_id: str,
        files_changed: int,
        tests_passing: int,
        errors: int,
        last_file: str,
    ) -> ProgressSnapshot:
        """Store a progress snapshot for a task (last 10 kept).

        Args:
            task_id: Task identifier.
            files_changed: Number of files modified since agent start.
            tests_passing: Number of tests currently passing (-1 = unknown).
            errors: Number of active errors / compilation failures.
            last_file: Last file the agent was editing.

        Returns:
            The new ProgressSnapshot.
        """
        snap = ProgressSnapshot(
            timestamp=time.time(),
            files_changed=files_changed,
            tests_passing=tests_passing,
            errors=errors,
            last_file=last_file,
        )
        q = self._progress_snapshots.setdefault(task_id, deque(maxlen=10))
        q.append(snap)
        return snap

    def get_snapshots(self, task_id: str) -> list[ProgressSnapshot]:
        """Return stored progress snapshots for a task, oldest-first.

        Args:
            task_id: Task identifier.

        Returns:
            List of ProgressSnapshot objects (up to 10), oldest-first.
        """
        return list(self._progress_snapshots.get(task_id, deque()))

    async def cancel(self, task_id: str, reason: str) -> Task:
        """Cancel a task that has not yet finished.

        Args:
            task_id: Task identifier.
            reason: Why it was cancelled.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            ValueError: If the task is in a terminal state (done, failed, cancelled).
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status not in (TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
                raise ValueError(f"Task '{task_id}' cannot be cancelled from status '{task.status.value}'")
            self._index_remove(task)
            task.status = TaskStatus.CANCELLED
            task.result_summary = reason
            task.version += 1
            self._index_add(task)
            completed_at = time.time()
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            return task

    def list_tasks(
        self,
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks, optionally filtered by status and/or cell_id.

        When status='open', tasks whose dependencies are not all done are
        excluded (they are not yet available for agents to pick up).

        Args:
            status: If provided, only tasks with this status are returned.
            cell_id: If provided, only tasks in this cell are returned.

        Returns:
            List of matching tasks.
        """
        if status is not None:
            try:
                ts = TaskStatus(status)
                tasks: list[Task] = list(self._by_status[ts].values())
            except ValueError:
                tasks = []
        else:
            tasks = list(self._tasks.values())
        if cell_id is not None:
            tasks = [t for t in tasks if t.cell_id == cell_id]
        if status == "open":
            done_ids = {t.id for t in self._by_status[TaskStatus.DONE].values()}
            tasks = [t for t in tasks if all(dep in done_ids for dep in t.depends_on)]
        return tasks

    def get_task(self, task_id: str) -> Task | None:
        """Look up a single task by id."""
        return self._tasks.get(task_id)

    # -- agents / heartbeats ------------------------------------------------

    def heartbeat(self, agent_id: str, role: str, status: Literal["starting", "working", "idle", "dead"]) -> float:
        """Record agent heartbeat.

        Args:
            agent_id: Unique agent identifier.
            role: Agent's role.
            status: Agent's self-reported status.

        Returns:
            Server timestamp of the heartbeat.
        """
        now = time.time()
        if agent_id in self._agents:
            self._agents[agent_id].heartbeat_ts = now
            self._agents[agent_id].status = status
        else:
            self._agents[agent_id] = AgentSession(
                id=agent_id,
                role=role,
                heartbeat_ts=now,
                status=status,
            )
        return now

    def stale_agents(self, threshold_s: float = 60.0) -> list[AgentSession]:
        """Return agents whose last heartbeat is older than *threshold_s*."""
        now = time.time()
        return [a for a in self._agents.values() if now - a.heartbeat_ts > threshold_s]

    def mark_stale_dead(self, threshold_s: float = 60.0) -> int:
        """Mark agents with stale heartbeats as dead.

        Returns:
            Number of agents marked dead.
        """
        count = 0
        for agent in self.stale_agents(threshold_s):
            agent.status = "dead"
            count += 1
        return count

    # -- status summary -----------------------------------------------------

    def _read_cost_by_role(self) -> dict[str, float]:
        """Return cost_usd summed per role, using an mtime+offset-based cache.

        The metrics JSONL is append-only, so when the file changes we only
        read bytes beyond the last known offset.  This makes the hot path
        O(new_lines) instead of O(all_lines).
        """
        if not self._metrics_jsonl_path.exists():
            return dict(self._cost_cache)
        stat = self._metrics_jsonl_path.stat()
        mtime = stat.st_mtime
        if mtime == self._cost_cache_mtime:
            return dict(self._cost_cache)
        file_size = stat.st_size
        # Handle truncation: if offset is past end of file, reset.
        if self._cost_cache_offset > file_size:
            self._cost_cache_offset = 0
            self._cost_cache = {}
        with self._metrics_jsonl_path.open("rb") as fh:
            fh.seek(self._cost_cache_offset)
            new_bytes = fh.read()
            new_offset = self._cost_cache_offset + len(new_bytes)
        for raw_line in new_bytes.decode(errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record_data: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = record_data.get("role", "")
            cost = record_data.get("cost_usd")
            if role and isinstance(cost, (int, float)):
                self._cost_cache[role] = self._cost_cache.get(role, 0.0) + float(cost)
        self._cost_cache_offset = new_offset
        self._cost_cache_mtime = mtime
        return dict(self._cost_cache)

    def status_summary(self) -> StatusResponse:
        """Build a dashboard summary of task counts."""
        total = len(self._tasks)
        open_count = len(self._by_status[TaskStatus.OPEN])
        claimed_count = len(self._by_status[TaskStatus.CLAIMED])
        done_count = len(self._by_status[TaskStatus.DONE])
        failed_count = len(self._by_status[TaskStatus.FAILED])

        # Per-role breakdown using _by_role_status index
        all_roles = {role for role, _ in self._by_role_status}
        roles: dict[str, dict[str, int]] = {}
        for role in all_roles:
            roles[role] = {
                "open": len(self._by_role_status.get((role, TaskStatus.OPEN), [])),
                "claimed": len(self._by_role_status.get((role, TaskStatus.CLAIMED), [])),
                "done": len(self._by_role_status.get((role, TaskStatus.DONE), [])),
                "failed": len(self._by_role_status.get((role, TaskStatus.FAILED), [])),
            }

        cost_by_role = self._read_cost_by_role()
        total_cost_usd = sum(cost_by_role.values())

        per_role = [
            RoleCounts(role=r, cost_usd=cost_by_role.get(r, 0.0), **counts) for r, counts in sorted(roles.items())
        ]

        return StatusResponse(
            total=total,
            open=open_count,
            claimed=claimed_count,
            done=done_count,
            failed=failed_count,
            per_role=per_role,
            total_cost_usd=total_cost_usd,
        )

    def read_archive(self, limit: int = 50) -> list[ArchiveRecord]:
        """Return the last *limit* records from the archive JSONL.

        Uses reverse file seeking (tail-style) so only O(limit) bytes are
        read regardless of archive size.

        Args:
            limit: Maximum number of records to return (default 50).

        Returns:
            List of archive record dicts, oldest-first up to *limit*.
        """
        if not self._archive_path.exists():
            return []

        chunk_size = 8192
        raw_lines: list[bytes] = []

        with self._archive_path.open("rb") as f:
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if file_size == 0:
                return []

            pos = file_size
            buf = b""
            while pos > 0 and (limit <= 0 or len(raw_lines) < limit):
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                buf = chunk + buf
                parts = buf.split(b"\n")
                # Keep the leftmost partial line in buf for the next iteration.
                buf = parts[0]
                # parts[1:] are complete lines; iterate newest-first.
                for part in reversed(parts[1:]):
                    stripped = part.strip()
                    if stripped:
                        raw_lines.append(stripped)
                    if limit > 0 and len(raw_lines) >= limit:
                        break
            # Process any remaining buffered content.
            if (limit <= 0 or len(raw_lines) < limit) and buf.strip():
                raw_lines.append(buf.strip())

        # raw_lines is newest-first; take up to limit, then reverse to oldest-first.
        tail = raw_lines[:limit] if limit > 0 else raw_lines
        records: list[ArchiveRecord] = []
        for raw in reversed(tail):
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return records

    @property
    def agents(self) -> dict[str, AgentSession]:
        """All known agent sessions."""
        return self._agents

    @property
    def agent_count(self) -> int:
        """Number of known agents."""
        return len(self._agents)

    def cost_by_role(self) -> dict[str, float]:
        """Return cost_usd summed per role (public accessor)."""
        return self._read_cost_by_role()

    @property
    def start_ts(self) -> float:
        """Server start timestamp."""
        return self._start_ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_upgrade_dict(raw: dict[str, Any] | None) -> UpgradeProposalDetails | None:
    if not raw:
        return None
    risk = RiskAssessment(**raw.get("risk_assessment", {}))
    rollback = RollbackPlan(**raw.get("rollback_plan", {}))
    return UpgradeProposalDetails(
        current_state=raw.get("current_state", ""),
        proposed_change=raw.get("proposed_change", ""),
        benefits=raw.get("benefits", []),
        risk_assessment=risk,
        rollback_plan=rollback,
        cost_estimate_usd=raw.get("cost_estimate_usd", 0.0),
        performance_impact=raw.get("performance_impact", ""),
    )


def a2a_task_to_response(task: Any) -> A2ATaskResponse:
    """Convert an A2ATask to its Pydantic response model."""
    return A2ATaskResponse(
        id=task.id,
        bernstein_task_id=task.bernstein_task_id,
        sender=task.sender,
        message=task.message,
        status=task.status.value,
        artifacts=[
            A2AArtifactResponse(
                name=a.name,
                content_type=a.content_type,
                data=a.data,
                created_at=a.created_at,
            )
            for a in task.artifacts
        ],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def node_to_response(node: NodeInfo) -> NodeResponse:
    """Convert a NodeInfo to a Pydantic response model."""
    return NodeResponse(
        id=node.id,
        name=node.name,
        url=node.url,
        status=node.status.value,
        capacity=NodeCapacitySchema(
            max_agents=node.capacity.max_agents,
            available_slots=node.capacity.available_slots,
            active_agents=node.capacity.active_agents,
            gpu_available=node.capacity.gpu_available,
            supported_models=node.capacity.supported_models,
        ),
        last_heartbeat=node.last_heartbeat,
        registered_at=node.registered_at,
        labels=node.labels,
        cell_ids=node.cell_ids,
    )


def task_to_response(task: Task) -> TaskResponse:
    """Convert a domain Task to a Pydantic response model."""
    return TaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        role=task.role,
        priority=task.priority,
        scope=task.scope.value,
        complexity=task.complexity.value,
        estimated_minutes=task.estimated_minutes,
        status=task.status.value,
        depends_on=task.depends_on,
        owned_files=task.owned_files,
        assigned_agent=task.assigned_agent,
        result_summary=task.result_summary,
        cell_id=task.cell_id,
        task_type=task.task_type.value,
        upgrade_details=asdict(task.upgrade_details) if task.upgrade_details else None,
        model=task.model,
        effort=task.effort,
        completion_signals=[{"type": s.type, "value": s.value} for s in task.completion_signals],
        created_at=task.created_at,
        progress_log=list(cast("list[ProgressEntry]", task.progress_log)),  # type: ignore[reportUnknownMemberType]
        version=task.version,
    )


# ---------------------------------------------------------------------------
# SSE event bus — fan-out to all connected dashboard clients
# ---------------------------------------------------------------------------


class SSEBus:
    """Fan-out event bus for Server-Sent Events.

    Each connected client gets its own asyncio.Queue.  Publishing an event
    pushes it to every queue.  Disconnected clients are cleaned up lazily.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []

    def subscribe(self) -> asyncio.Queue[str]:
        """Create a new subscriber queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    def publish(self, event_type: str, data: str = "{}") -> None:
        """Push an event to all subscribers (non-blocking)."""
        message = f"event: {event_type}\ndata: {data}\n\n"
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)


# ---------------------------------------------------------------------------
# Background: stale-agent reaper
# ---------------------------------------------------------------------------


async def _reaper_loop(store: TaskStore, interval_s: float = 30.0) -> None:
    """Periodically mark stale agents as dead."""
    while True:
        await asyncio.sleep(interval_s)
        store.mark_stale_dead()


async def _node_reaper_loop(node_reg: NodeRegistry, interval_s: float = 15.0) -> None:
    """Periodically mark stale cluster nodes as offline."""
    while True:
        await asyncio.sleep(interval_s)
        node_reg.mark_stale()


async def _sse_heartbeat_loop(bus: SSEBus, interval_s: float = 15.0) -> None:
    """Send periodic heartbeat events to keep SSE connections alive."""
    while True:
        await asyncio.sleep(interval_s)
        bus.publish("heartbeat", json.dumps({"ts": time.time()}))


# ---------------------------------------------------------------------------
# Helpers used by route modules
# ---------------------------------------------------------------------------

DEFAULT_JSONL_PATH = Path(".sdd/runtime/tasks.jsonl")


def read_log_tail(path: Path, offset: int = 0) -> str:
    """Read a log file from *offset* bytes, skipping the partial first line.

    Args:
        path: Path to the log file.
        offset: Byte offset to start reading from.

    Returns:
        Log content as a string, with partial leading line stripped when
        offset is mid-line.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    # When seeking into the middle of a file, the first partial line is
    # incomplete — strip it so callers only see whole lines.
    if offset > 0 and not text.startswith("\n"):
        idx = text.find("\n")
        if idx == -1:
            return ""
        text = text[idx + 1 :]
    return text


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    jsonl_path: Path = DEFAULT_JSONL_PATH,
    metrics_jsonl_path: Path | None = None,
    auth_token: str | None = None,
    cluster_config: ClusterConfig | None = None,
    readonly: bool = False,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        jsonl_path: Where to persist the JSONL task log.
        metrics_jsonl_path: Path to the metrics JSONL for cost reporting.
            Defaults to <jsonl_path.parent.parent>/metrics/tasks.jsonl.
        auth_token: If set, all API requests must include a matching
            ``Authorization: Bearer <token>`` header.
        cluster_config: Cluster mode configuration. If provided and
            enabled, node registration and cluster endpoints are active.
        readonly: If True, all write operations (POST/PUT/PATCH/DELETE) are
            rejected with 405.  The dashboard, events stream, and read
            endpoints remain fully accessible.  Useful for public demo
            deployments.

    Returns:
        Configured FastAPI app with all routes registered.
    """
    from bernstein.core.routes.agents import router as agents_router
    from bernstein.core.routes.costs import router as costs_router
    from bernstein.core.routes.status import router as status_router
    from bernstein.core.routes.tasks import router as tasks_router
    from bernstein.core.routes.webhooks import router as webhooks_router

    # Resolve auth token: explicit arg > env var > None
    effective_token = auth_token or os.environ.get("BERNSTEIN_AUTH_TOKEN")

    # Cluster setup
    effective_cluster = cluster_config or ClusterConfig()
    node_registry = NodeRegistry(effective_cluster)

    store = TaskStore(jsonl_path, metrics_jsonl_path=metrics_jsonl_path)
    sse_bus = SSEBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: replay persisted state
        store.replay_jsonl()
        # Launch the stale-agent reaper
        reaper = asyncio.create_task(_reaper_loop(store))
        # Launch SSE heartbeat loop
        sse_heartbeat = asyncio.create_task(_sse_heartbeat_loop(sse_bus))
        # Launch node-stale reaper if cluster mode is on
        node_reaper: asyncio.Task[None] | None = None
        if effective_cluster.enabled:
            node_reaper = asyncio.create_task(
                _node_reaper_loop(node_registry, interval_s=effective_cluster.node_heartbeat_interval_s)
            )
        yield
        # Shutdown
        reaper.cancel()
        sse_heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        with contextlib.suppress(asyncio.CancelledError):
            await sse_heartbeat
        if node_reaper is not None:
            node_reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await node_reaper
        await store.flush_buffer()

    application = FastAPI(title="Bernstein Task Server", version="0.1.0", lifespan=lifespan)

    # Read-only mode — blocks all writes before auth is even checked
    if readonly:
        application.add_middleware(ReadOnlyMiddleware)

    # Auth middleware — only enforced when a token is configured
    application.add_middleware(BearerAuthMiddleware, auth_token=effective_token)

    # Attach shared state for route modules to access via request.app.state
    bulletin = BulletinBoard()
    a2a_handler = A2AHandler(server_url="http://localhost:8052")

    application.state.store = store  # type: ignore[attr-defined]
    application.state.bulletin = bulletin  # type: ignore[attr-defined]
    application.state.a2a_handler = a2a_handler  # type: ignore[attr-defined]
    application.state.node_registry = node_registry  # type: ignore[attr-defined]
    application.state.sse_bus = sse_bus  # type: ignore[attr-defined]
    application.state.runtime_dir = jsonl_path.parent  # type: ignore[attr-defined]  # .sdd/runtime/
    application.state.sdd_dir = jsonl_path.parent.parent  # type: ignore[attr-defined]  # .sdd/

    # Mount routers
    application.include_router(agents_router)
    application.include_router(tasks_router)
    application.include_router(status_router)
    application.include_router(webhooks_router)
    application.include_router(costs_router)

    return application


# Default app instance for `uvicorn bernstein.core.server:app`
# Auth token and cluster config are read from environment at import time.
_default_cluster_enabled = os.environ.get("BERNSTEIN_CLUSTER_ENABLED", "").lower() in ("1", "true", "yes")
_default_cluster_config = (
    ClusterConfig(
        enabled=_default_cluster_enabled,
        auth_token=os.environ.get("BERNSTEIN_AUTH_TOKEN"),
        bind_host=os.environ.get("BERNSTEIN_BIND_HOST", "127.0.0.1"),
    )
    if _default_cluster_enabled
    else None
)

app: FastAPI = create_app(
    auth_token=os.environ.get("BERNSTEIN_AUTH_TOKEN"),
    cluster_config=_default_cluster_config,
    readonly=os.environ.get("BERNSTEIN_READONLY", "").lower() in ("1", "true", "yes"),
)
