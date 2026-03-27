"""FastAPI task server — central coordination point for all agents.

Agents pull tasks via HTTP, report completion, and send heartbeats.
State is held in-memory and flushed periodically to JSONL for persistence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.models import (
    AgentSession,
    RiskAssessment,
    RollbackPlan,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)

# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------


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


class TaskCompleteRequest(BaseModel):
    """Body for POST /tasks/{task_id}/complete."""

    result_summary: str


class TaskFailRequest(BaseModel):
    """Body for POST /tasks/{task_id}/fail."""

    reason: str = ""


class RoleCounts(BaseModel):
    """Per-role open task counts."""

    role: str
    open: int
    claimed: int
    done: int
    failed: int


class StatusResponse(BaseModel):
    """Body for GET /status."""

    total: int
    open: int
    claimed: int
    done: int
    failed: int
    per_role: list[RoleCounts]


class HeartbeatRequest(BaseModel):
    """Body for POST /agents/{agent_id}/heartbeat."""

    role: str = ""
    status: str = "working"


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
    type: str = "status"  # alert, blocker, finding, status, dependency
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


class TaskStore:
    """Thread-safe in-memory task store with JSONL persistence.

    All mutations go through this class so the JSONL log stays consistent.
    """

    def __init__(self, jsonl_path: Path) -> None:
        self._tasks: dict[str, Task] = {}
        self._agents: dict[str, AgentSession] = {}
        self._jsonl_path: Path = jsonl_path
        self._lock: asyncio.Lock = asyncio.Lock()
        self._dirty: bool = False
        self._start_ts: float = time.time()

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
                task.status = TaskStatus(record.get("status", task.status.value))
                task.assigned_agent = record.get("assigned_agent", task.assigned_agent)
                task.result_summary = record.get("result_summary", task.result_summary)
            else:
                from bernstein.core.models import Complexity, Scope

                self._tasks[task_id] = Task(
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

    async def _append_jsonl(self, record: dict[str, Any]) -> None:
        """Append a single JSON record to the JSONL log."""
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"
        # Blocking write is fine — file is local, lines are small.
        with self._jsonl_path.open("a") as f:
            f.write(line)

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

    async def create(self, req: TaskCreate) -> Task:
        """Create a new task and persist it.

        Args:
            req: Validated creation request.

        Returns:
            The newly created Task.
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
        )
        async with self._lock:
            self._tasks[task.id] = task
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
            candidates = [t for t in self._tasks.values() if t.role == role and t.status == TaskStatus.OPEN]
            if not candidates:
                return None
            candidates.sort(key=lambda t: t.priority)
            task = candidates[0]
            task.status = TaskStatus.CLAIMED
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
                task.status = TaskStatus.CLAIMED
                await self._append_jsonl(self._task_to_record(task))
            return task

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
            task.status = TaskStatus.DONE
            task.result_summary = result_summary
            await self._append_jsonl(self._task_to_record(task))
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
            task.status = TaskStatus.FAILED
            task.result_summary = reason
            await self._append_jsonl(self._task_to_record(task))
            return task

    def list_tasks(
        self,
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks, optionally filtered by status and/or cell_id.

        Args:
            status: If provided, only tasks with this status are returned.
            cell_id: If provided, only tasks in this cell are returned.

        Returns:
            List of matching tasks.
        """
        tasks = list(self._tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status.value == status]
        if cell_id is not None:
            tasks = [t for t in tasks if t.cell_id == cell_id]
        return tasks

    def get_task(self, task_id: str) -> Task | None:
        """Look up a single task by id."""
        return self._tasks.get(task_id)

    # -- agents / heartbeats ------------------------------------------------

    def heartbeat(self, agent_id: str, role: str, status: str) -> float:
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
            self._agents[agent_id].status = status  # type: ignore[assignment]
        else:
            self._agents[agent_id] = AgentSession(
                id=agent_id,
                role=role,
                heartbeat_ts=now,
                status=status,  # type: ignore[assignment]
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

    def status_summary(self) -> StatusResponse:
        """Build a dashboard summary of task counts."""
        tasks = list(self._tasks.values())
        total = len(tasks)

        def _count(s: TaskStatus) -> int:
            return sum(1 for t in tasks if t.status == s)

        # Per-role breakdown
        roles: dict[str, dict[str, int]] = {}
        for t in tasks:
            if t.role not in roles:
                roles[t.role] = {"open": 0, "claimed": 0, "done": 0, "failed": 0}
            bucket = roles[t.role]
            if t.status == TaskStatus.OPEN:
                bucket["open"] += 1
            elif t.status == TaskStatus.CLAIMED:
                bucket["claimed"] += 1
            elif t.status == TaskStatus.DONE:
                bucket["done"] += 1
            elif t.status == TaskStatus.FAILED:
                bucket["failed"] += 1

        per_role = [RoleCounts(role=r, **counts) for r, counts in sorted(roles.items())]

        return StatusResponse(
            total=total,
            open=_count(TaskStatus.OPEN),
            claimed=_count(TaskStatus.CLAIMED),
            done=_count(TaskStatus.DONE),
            failed=_count(TaskStatus.FAILED),
            per_role=per_role,
        )

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


def create_app(jsonl_path: Path = DEFAULT_JSONL_PATH) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        jsonl_path: Where to persist the JSONL task log.

    Returns:
        Configured FastAPI app with all routes registered.
    """

    store = TaskStore(jsonl_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
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

    @application.get("/tasks", response_model=list[TaskResponse])
    async def list_tasks(
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[TaskResponse]:
        """List all tasks, optionally filtered by status and/or cell_id."""
        return [_task_to_response(t) for t in store.list_tasks(status, cell_id)]

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
            type=body.type,  # type: ignore[arg-type]
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

    # Attach store and bulletin for testing access
    application.state.store = store  # type: ignore[attr-defined]
    application.state.bulletin = bulletin  # type: ignore[attr-defined]

    return application


# Default app instance for `uvicorn bernstein.core.server:app`
app: FastAPI = create_app()
