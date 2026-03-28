"""FastAPI task server — central coordination point for all agents.

Agents pull tasks via HTTP, report completion, and send heartbeats.
State is held in-memory and flushed periodically to JSONL for persistence.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bernstein.core.bulletin import BulletinBoard, BulletinMessage, MessageType
from bernstein.core.models import (
    AgentSession,
    CompletionSignal,
    RiskAssessment,
    RollbackPlan,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

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
    model: str | None = None       # Manager hint: "opus", "sonnet", "haiku"
    effort: str | None = None      # Manager hint: "max", "high", "medium", "low"
    completion_signals: list[CompletionSignalSchema] = Field(default_factory=list)


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
    completion_signals: list[dict[str, str]] = Field(default_factory=list)
    created_at: float
    progress_log: list[dict[str, Any]] = Field(default_factory=list)


class TaskCompleteRequest(BaseModel):
    """Body for POST /tasks/{task_id}/complete."""

    result_summary: str


class TaskFailRequest(BaseModel):
    """Body for POST /tasks/{task_id}/fail."""

    reason: str = ""


class TaskCancelRequest(BaseModel):
    """Body for POST /tasks/{task_id}/cancel."""

    reason: str = ""


class TaskProgressRequest(BaseModel):
    """Body for POST /tasks/{task_id}/progress."""

    message: str
    percent: int = 0


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


class BulletinMessageResponse(BaseModel):
    """Single bulletin message in responses."""

    agent_id: str
    type: str
    content: str
    timestamp: float
    cell_id: str | None


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
        self._dirty: bool = False
        self._start_ts: float = time.time()
        self._cost_cache: dict[str, float] = {}
        self._cost_cache_mtime: float = 0.0
        self._cost_cache_offset: int = 0

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
                record: dict[str, Any] = json.loads(line)
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
                from bernstein.core.models import Complexity, Scope

                task = Task(
                    id=task_id,
                    title=record.get("title", ""),
                    description=record.get("description", ""),
                    role=record.get("role", ""),
                    priority=record.get("priority", 2),
                    scope=Scope(record.get("scope", "medium")),
                    complexity=Complexity(record.get("complexity", "medium")),
                    estimated_minutes=record.get("estimated_minutes", 30),
                    status=TaskStatus(record.get("status", "open")),
                    task_type=TaskType(record.get("task_type", "standard")),
                    upgrade_details=_parse_upgrade_dict(record.get("upgrade_details")),
                    depends_on=record.get("depends_on", []),
                    owned_files=record.get("owned_files", []),
                    assigned_agent=record.get("assigned_agent"),
                    result_summary=record.get("result_summary"),
                    cell_id=record.get("cell_id"),
                )
                self._tasks[task_id] = task
                self._index_add(task)

    async def _append_jsonl(self, record: dict[str, Any]) -> None:
        """Append a single JSON record to the JSONL log."""
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"

        def _write() -> None:
            with self._jsonl_path.open("a") as f:
                f.write(line)

        await asyncio.to_thread(_write)

    async def _append_archive(self, task: Task, completed_at: float) -> None:
        """Append a completed/failed task record to the archive JSONL."""
        self._archive_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
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

    def _task_to_record(self, task: Task) -> dict[str, Any]:
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
            completion_signals=[
                CompletionSignal(type=s.type, value=s.value)
                for s in req.completion_signals
            ],
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
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def claim_by_id(self, task_id: str) -> Task:
        """Claim a specific task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            The claimed Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status == TaskStatus.OPEN:
                self._index_remove(task)
                task.status = TaskStatus.CLAIMED
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
            self._index_add(task)
            completed_at = time.time()
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
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
            task.progress_log.append({"timestamp": time.time(), "message": message, "percent": percent})
            return task

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
                raise ValueError(
                    f"Task '{task_id}' cannot be cancelled from status '{task.status.value}'"
                )
            self._index_remove(task)
            task.status = TaskStatus.CANCELLED
            task.result_summary = reason
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
            tasks = [
                t for t in tasks
                if all(dep in done_ids for dep in t.depends_on)
            ]
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
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = record.get("role", "")
            cost = record.get("cost_usd")
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
            RoleCounts(role=r, cost_usd=cost_by_role.get(r, 0.0), **counts)
            for r, counts in sorted(roles.items())
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

    def read_archive(self, limit: int = 50) -> list[dict[str, Any]]:
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
        records: list[dict[str, Any]] = []
        for raw in reversed(tail):
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return records

    @property
    def agent_count(self) -> int:
        """Number of known agents."""
        return len(self._agents)

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


def _task_to_response(task: Task) -> TaskResponse:
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
        progress_log=list(task.progress_log),
    )


# ---------------------------------------------------------------------------
# Background: stale-agent reaper
# ---------------------------------------------------------------------------


async def _reaper_loop(store: TaskStore, interval_s: float = 30.0) -> None:
    """Periodically mark stale agents as dead."""
    while True:
        await asyncio.sleep(interval_s)
        store.mark_stale_dead()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

DEFAULT_JSONL_PATH = Path(".sdd/runtime/tasks.jsonl")


def create_app(
    jsonl_path: Path = DEFAULT_JSONL_PATH,
    metrics_jsonl_path: Path | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        jsonl_path: Where to persist the JSONL task log.
        metrics_jsonl_path: Path to the metrics JSONL for cost reporting.
            Defaults to <jsonl_path.parent.parent>/metrics/tasks.jsonl.

    Returns:
        Configured FastAPI app with all routes registered.
    """

    store = TaskStore(jsonl_path, metrics_jsonl_path=metrics_jsonl_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: replay persisted state
        store.replay_jsonl()
        # Launch the stale-agent reaper
        reaper = asyncio.create_task(_reaper_loop(store))
        yield
        # Shutdown
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper

    application = FastAPI(title="Bernstein Task Server", version="0.1.0", lifespan=lifespan)

    # -- routes -------------------------------------------------------------

    @application.post("/tasks", response_model=TaskResponse, status_code=201)
    async def create_task(body: TaskCreate) -> TaskResponse:
        """Create a new task."""
        task = await store.create(body)
        return _task_to_response(task)

    @application.get("/tasks/next/{role}", response_model=TaskResponse)
    async def next_task(role: str) -> TaskResponse:
        """Claim the next available task for *role*."""
        task = await store.claim_next(role)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No open tasks for role '{role}'")
        return _task_to_response(task)

    @application.post("/tasks/claim-batch", response_model=BatchClaimResponse)
    async def claim_batch(body: BatchClaimRequest) -> BatchClaimResponse:
        """Atomically claim multiple tasks by ID for an agent."""
        claimed, failed = await store.claim_batch(body.task_ids, body.agent_id)
        return BatchClaimResponse(claimed=claimed, failed=failed)

    @application.post("/tasks/{task_id}/claim", response_model=TaskResponse)
    async def claim_task(task_id: str) -> TaskResponse:
        """Claim a specific task by ID."""
        try:
            task = await store.claim_by_id(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        return _task_to_response(task)

    @application.post("/tasks/{task_id}/complete", response_model=TaskResponse)
    async def complete_task(task_id: str, body: TaskCompleteRequest) -> TaskResponse:
        """Mark a task as done with a result summary."""
        try:
            task = await store.complete(task_id, body.result_summary)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        return _task_to_response(task)

    @application.post("/tasks/{task_id}/fail", response_model=TaskResponse)
    async def fail_task(task_id: str, body: TaskFailRequest) -> TaskResponse:
        """Mark a task as failed."""
        try:
            task = await store.fail(task_id, body.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        return _task_to_response(task)

    @application.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(task_id: str, body: TaskCancelRequest) -> TaskResponse:
        """Cancel a task that has not yet finished."""
        try:
            task = await store.cancel(task_id, body.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return _task_to_response(task)

    @application.post("/tasks/{task_id}/progress", response_model=TaskResponse)
    async def progress_task(task_id: str, body: TaskProgressRequest) -> TaskResponse:
        """Append an intermediate progress update to a task."""
        try:
            task = await store.add_progress(task_id, body.message, body.percent)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        return _task_to_response(task)

    @application.get("/tasks", response_model=list[TaskResponse])
    async def list_tasks(
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[TaskResponse]:
        """List all tasks, optionally filtered by status and/or cell_id."""
        return [_task_to_response(t) for t in store.list_tasks(status, cell_id)]

    @application.get("/tasks/archive", response_model=list[dict[str, Any]])
    async def get_archive(limit: int = 50) -> list[dict[str, Any]]:
        """Return the last N archived (done/failed) task records."""
        return store.read_archive(limit=limit)

    @application.get("/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(task_id: str) -> TaskResponse:
        """Get a single task by ID."""
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
        return _task_to_response(task)

    @application.get("/status", response_model=StatusResponse)
    async def status_dashboard() -> StatusResponse:
        """Dashboard summary of task counts."""
        return store.status_summary()

    @application.post("/agents/{agent_id}/heartbeat", response_model=HeartbeatResponse)
    async def agent_heartbeat(agent_id: str, body: HeartbeatRequest) -> HeartbeatResponse:
        """Register an agent heartbeat."""
        ts = store.heartbeat(agent_id, body.role, body.status)
        return HeartbeatResponse(agent_id=agent_id, acknowledged=True, server_ts=ts)

    @application.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """Basic liveness check."""
        return HealthResponse(
            status="ok",
            uptime_s=round(time.time() - store.start_ts, 2),
            task_count=len(store.list_tasks()),
            agent_count=store.agent_count,
        )

    # -- bulletin board routes -------------------------------------------------

    bulletin = BulletinBoard()

    @application.post("/bulletin", response_model=BulletinMessageResponse, status_code=201)
    async def post_bulletin(body: BulletinPostRequest) -> BulletinMessageResponse:
        """Append a message to the bulletin board."""
        msg = BulletinMessage(
            agent_id=body.agent_id,
            type=body.type,
            content=body.content,
            cell_id=body.cell_id,
        )
        stored = bulletin.post(msg)
        return BulletinMessageResponse(
            agent_id=stored.agent_id,
            type=stored.type,
            content=stored.content,
            timestamp=stored.timestamp,
            cell_id=stored.cell_id,
        )

    @application.get("/bulletin", response_model=list[BulletinMessageResponse])
    async def get_bulletin(since: float = 0.0) -> list[BulletinMessageResponse]:
        """Get bulletin messages since a given timestamp."""
        messages = bulletin.read_since(since)
        return [
            BulletinMessageResponse(
                agent_id=m.agent_id,
                type=m.type,
                content=m.content,
                timestamp=m.timestamp,
                cell_id=m.cell_id,
            )
            for m in messages
        ]

    # Attach store and bulletin for testing access.
    # FastAPI's `state` is a plain object with no predefined attributes;
    # type: ignore[attr-defined] is the standard pattern here.
    application.state.store = store  # type: ignore[attr-defined]
    application.state.bulletin = bulletin  # type: ignore[attr-defined]

    return application


# Default app instance for `uvicorn bernstein.core.server:app`
app: FastAPI = create_app()
