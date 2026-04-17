"""CRUD operations and the TaskStore class — core task mutations.

All task mutations go through this class so the JSONL log stays consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, cast

from fastapi import HTTPException
from typing_extensions import TypedDict

from bernstein.core.defaults import TASK as _TASK_DEFAULTS
from bernstein.core.tasks.lifecycle import IllegalTransitionError, transition_agent, transition_task
from bernstein.core.tasks.models import (
    AgentSession,
    CompletionSignal,
    ProgressSnapshot,
    RiskAssessment,
    RollbackPlan,
    Task,
    TaskStatus,
    TaskStoreUnavailable,
    TaskType,
    UpgradeProposalDetails,
)
from bernstein.core.tenanting import ensure_tenant_layout, normalize_tenant_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

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
    estimated_minutes: int | None
    status: str
    task_type: str
    upgrade_details: dict[str, Any] | None
    depends_on: list[str]
    parent_task_id: str | None
    depends_on_repo: str | None
    owned_files: list[str]
    assigned_agent: str | None
    result_summary: str | None
    tenant_id: str
    cell_id: str | None
    repo: str | None
    batch_eligible: bool
    eu_ai_act_risk: str
    approval_required: bool
    risk_level: str
    slack_context: dict[str, Any] | None
    version: int
    claimed_at: float | None
    completed_at: float | None
    closed_at: float | None
    claimed_by_session: str | None
    parent_session_id: str | None
    subtask_wait_started_at: float | None
    parent_context: str | None
    # audit-017: typed retry bookkeeping (optional for backward compat).
    retry_count: NotRequired[int]
    max_retries: NotRequired[int]
    retry_delay_s: NotRequired[float]
    terminal_reason: NotRequired[str | None]
    max_output_tokens: NotRequired[int | None]
    meta_messages: NotRequired[list[str]]
    metadata: NotRequired[dict[str, Any]]


class ArchiveRecord(TypedDict):
    """Archive JSONL entry written when a task reaches a terminal state."""

    task_id: str
    title: str
    role: str
    tenant_id: str
    status: str
    created_at: float
    completed_at: float
    duration_seconds: float
    result_summary: str | None
    cost_usd: float | None
    assigned_agent: str | None
    owned_files: list[str]
    claimed_by_session: str | None


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


class _CompletionSignalRequest(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def value(self) -> str: ...


class TaskCreateRequest(Protocol):
    """Protocol for validated task-create request objects."""

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    estimated_minutes: int | None

    @property
    def depends_on(self) -> Sequence[str]: ...

    parent_task_id: str | None
    depends_on_repo: str | None

    @property
    def owned_files(self) -> Sequence[str]: ...

    tenant_id: str
    cell_id: str | None
    repo: str | None
    task_type: str

    @property
    def upgrade_details(self) -> Mapping[str, Any] | None: ...

    model: str | None
    effort: str | None
    batch_eligible: bool
    approval_required: bool
    eu_ai_act_risk: str
    risk_level: str

    @property
    def completion_signals(self) -> Sequence[_CompletionSignalRequest]: ...

    @property
    def slack_context(self) -> Mapping[str, Any] | None: ...

    parent_session_id: str | None
    parent_context: str | None

    # Retry bookkeeping (audit-017): typed retry fields are the single source
    # of truth.  When orchestrator clones a task for retry, it passes
    # ``retry_count=previous+1`` in the request.  These fields are optional on
    # the wire (``None`` / missing => fall back to the Task dataclass default).
    retry_count: int | None
    max_retries: int | None
    retry_delay_s: float | None
    terminal_reason: str | None
    max_output_tokens: int | None

    @property
    def meta_messages(self) -> Sequence[str] | None: ...


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


async def _retry_io(fn: Any, *args: Any) -> Any:
    """Retry a sync file I/O function with exponential backoff.

    Retries on transient OSError (e.g. EAGAIN, NFS stale handle).
    Raises TaskStoreUnavailable after exhausting retries.
    Raises OSError immediately for non-transient errors (ENOSPC, EROFS).
    """
    import errno

    max_retries = _TASK_DEFAULTS.max_io_retries
    non_transient = {errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM}
    last_exc: OSError | None = None
    for attempt in range(max_retries):
        try:
            return await asyncio.to_thread(fn, *args)
        except OSError as exc:
            if exc.errno in non_transient:
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(0.1 * (2**attempt))
                logger.warning(
                    "Transient I/O error (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    exc,
                )
    raise TaskStoreUnavailable(f"File I/O failed after {max_retries} retries: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------

DEFAULT_ARCHIVE_PATH = Path(".sdd/archive/tasks.jsonl")

# Grace period: completed tasks remain visible in status for 30 seconds
# before any cleanup pass may evict them from the active task set.
PANEL_GRACE_MS: int = 30_000


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
        self._sdd_dir: Path = jsonl_path.parent.parent if jsonl_path.parent.name == "runtime" else jsonl_path.parent
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
        try:
            lines = self._jsonl_path.read_text().splitlines()
        except OSError as exc:
            raise TaskStoreUnavailable(f"Cannot read task JSONL at {self._jsonl_path}: {exc}") from exc
        for line_num, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record: TaskRecord = json.loads(line)
            except json.JSONDecodeError:
                logger.error(
                    "Corrupted JSONL record at %s:%d — skipping: %s",
                    self._jsonl_path,
                    line_num,
                    raw_line[:500],
                )
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
                task.tenant_id = normalize_tenant_id(str(record.get("tenant_id", task.tenant_id) or task.tenant_id))
                self._index_add(task)
            else:
                task = Task.from_dict(cast("dict[str, Any]", record))
                self._tasks[task_id] = task
                self._index_add(task)

    def recover_stale_claimed_tasks(self) -> int:
        """Reset CLAIMED and IN_PROGRESS tasks to OPEN after a server restart.

        When the server process is killed mid-task, all CLAIMED and IN_PROGRESS
        tasks have no active agent.  This method re-queues them as OPEN so a
        fresh agent can pick them up.  Call this once after ``replay_jsonl()``
        during startup.

        The release is persisted to the JSONL log synchronously (bug
        ``audit-015``): without this, the in-memory reset is lost on crash and
        the stale CLAIMED line replays on the next restart, enabling duplicate
        execution.

        Returns:
            Number of tasks reset to open.
        """
        reset_count = 0
        reset_tasks: list[Task] = []
        for stale_status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
            for task in list(self._by_status.get(stale_status, {}).values()):
                self._index_remove(task)
                # Use the FSM for the transition so audit/telemetry fire and
                # any illegal jump is caught.  CLAIMED→OPEN and
                # IN_PROGRESS→OPEN are both allow-listed in
                # ``lifecycle.TASK_TRANSITIONS``.
                transition_task(
                    task,
                    TaskStatus.OPEN,
                    actor="task_store",
                    reason="recover_stale_after_restart",
                )
                task.claimed_at = None
                task.claimed_by_session = None
                self._index_add(task)
                reset_tasks.append(task)
                reset_count += 1
        if reset_count:
            # Flush release records to the JSONL log so the reset survives a
            # subsequent crash.  Without this flush, a kill before the task's
            # next mutation replays the CLAIMED line and a new agent can claim
            # a task another agent was already running (work duplication).
            for task in reset_tasks:
                self._append_jsonl_sync(self._task_to_record(task))
            logger.info("recover_stale_claimed_tasks: reset %d task(s) to open after restart", reset_count)
        return reset_count

    def _append_jsonl_sync(self, record: TaskRecord) -> None:
        """Synchronously append a record to the JSONL log.

        Used during startup recovery where the async
        :meth:`_append_jsonl` cannot be awaited (the caller is sync) and we
        still need the mutation durable on disk before returning.  Mirrors
        the tenant-scoped backlog file the async path writes.
        """
        line = json.dumps(record, default=str) + "\n"
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self._jsonl_path.open("a") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            tenant_paths = ensure_tenant_layout(self._sdd_dir, str(record["tenant_id"]))
            target_path = tenant_paths.backlog_dir / "tasks.jsonl"
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            # Tenant mirror is best-effort during recovery; the authoritative
            # JSONL log above is already durable.
            logger.warning("Failed to mirror recover_stale record to tenant backlog: %s", exc)

    _BUFFER_MAX: int = 1

    async def _flush_buffer_unlocked(self) -> None:
        """Write buffered JSONL records to disk. Caller must hold self._lock.

        Raises:
            TaskStoreUnavailable: After exhausting retries on transient I/O errors.
            OSError: Immediately on non-transient errors (disk full, permission denied).
        """
        if not self._write_buffer:
            return
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(self._write_buffer)
        self._write_buffer.clear()

        def _write() -> None:
            with self._jsonl_path.open("a") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

        await _retry_io(_write)

    async def _append_jsonl(self, record: TaskRecord) -> None:
        """Append a JSON record to the JSONL log, flushing immediately.

        Each mutation is flushed to disk right away (_BUFFER_MAX=1) so that
        no state is lost on a server crash.  The lifespan shutdown handler
        also calls flush_buffer() as a safety net.
        """
        line = json.dumps(record, default=str) + "\n"
        self._write_buffer.append(line)
        await self._append_tenant_backlog_record(record, line)
        if len(self._write_buffer) >= self._BUFFER_MAX:
            await self._flush_buffer_unlocked()

    async def flush_buffer(self) -> None:
        """Flush any buffered JSONL records to disk (acquires the store lock)."""
        async with self._lock:
            await self._flush_buffer_unlocked()

    def read_archive(self, limit: int = 50, tenant_id: str | None = None) -> list[ArchiveRecord]:
        """Return the last *limit* archived task records, oldest-first.

        Reads from the archive JSONL file on disk.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of archive records, oldest-first (last N from file).
        """
        if not self._archive_path.exists():
            return []

        records: list[ArchiveRecord] = []
        try:
            with self._archive_path.open() as f:
                for line_num, raw_line in enumerate(f, 1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.error(
                            "Corrupted archive record at %s:%d — skipping: %s",
                            self._archive_path,
                            line_num,
                            raw_line[:500],
                        )
        except OSError as exc:
            logger.warning("Cannot read archive at %s: %s", self._archive_path, exc)
            return []

        if tenant_id is not None:
            normalized = normalize_tenant_id(tenant_id)
            records = [record for record in records if normalize_tenant_id(str(record.get("tenant_id"))) == normalized]
        return records[-limit:]

    async def _append_archive(self, task: Task, completed_at: float) -> None:
        """Append a completed/failed task record to the archive JSONL.

        Raises:
            TaskStoreUnavailable: After exhausting retries on transient I/O errors.
            OSError: Immediately on non-transient errors (disk full, permission denied).
        """
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
            "assigned_agent": task.assigned_agent,
            "owned_files": list(task.owned_files),
            "tenant_id": normalize_tenant_id(task.tenant_id),
            "claimed_by_session": task.claimed_by_session,
        }
        line = json.dumps(record, default=str) + "\n"

        def _write() -> None:
            with self._archive_path.open("a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

        await _retry_io(_write)
        await self._append_tenant_archive_record(task.tenant_id, line)

    async def _append_tenant_backlog_record(self, record: TaskRecord, line: str) -> None:
        """Mirror task lifecycle records into a tenant-scoped backlog file."""

        tenant_paths = ensure_tenant_layout(self._sdd_dir, str(record["tenant_id"]))
        target_path = tenant_paths.backlog_dir / "tasks.jsonl"

        def _write() -> None:
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        await _retry_io(_write)

    async def _append_tenant_archive_record(self, tenant_id: str, line: str) -> None:
        """Mirror archive records into a tenant-scoped backlog archive file."""

        tenant_paths = ensure_tenant_layout(self._sdd_dir, tenant_id)
        target_path = tenant_paths.backlog_dir / "archive.jsonl"

        def _write() -> None:
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        await _retry_io(_write)

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
            "parent_task_id": task.parent_task_id,
            "depends_on_repo": task.depends_on_repo,
            "owned_files": task.owned_files,
            "assigned_agent": task.assigned_agent,
            "result_summary": task.result_summary,
            "tenant_id": normalize_tenant_id(task.tenant_id),
            "cell_id": task.cell_id,
            "repo": task.repo,
            "batch_eligible": task.batch_eligible is True,
            "eu_ai_act_risk": task.eu_ai_act_risk,
            "approval_required": task.approval_required,
            "risk_level": task.risk_level,
            "slack_context": task.slack_context,
            "version": task.version,
            "claimed_at": task.claimed_at,
            "completed_at": task.completed_at,
            "closed_at": task.closed_at,
            "claimed_by_session": task.claimed_by_session,
            "parent_session_id": task.parent_session_id,
            "subtask_wait_started_at": task.subtask_wait_started_at,
            # audit-017: retry bookkeeping (typed source of truth).
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "retry_delay_s": task.retry_delay_s,
            "terminal_reason": task.terminal_reason,
            "max_output_tokens": task.max_output_tokens,
            "meta_messages": list(task.meta_messages),
            "metadata": dict(task.metadata),
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

    def _dependencies_satisfied(self, task: Task) -> bool:
        done_ids = {done_task.id for done_task in self._by_status[TaskStatus.DONE].values()}
        if not all(dep in done_ids for dep in task.depends_on):
            return False
        if task.depends_on_repo is None:
            return True
        if not task.depends_on:
            return any(
                done_task.repo == task.depends_on_repo for done_task in self._by_status[TaskStatus.DONE].values()
            )
        return all(
            (self._tasks.get(dep_id) is not None and self._tasks[dep_id].repo == task.depends_on_repo)
            for dep_id in task.depends_on
        )

    async def create(self, req: TaskCreateRequest) -> Task:
        """Create a new task and persist it.

        Args:
            req: Validated creation request (TaskCreate from server).

        Returns:
            The newly created Task.

        Raises:
            HTTPException: 422 if depends_on references a non-existent task or creates a cycle.
        """
        from bernstein.core.tasks.models import Complexity, Scope

        # Determine batch eligibility: use caller's flag, then auto-detect for non-critical tasks
        batch_eligible: bool = getattr(req, "batch_eligible", False)
        complexity_val = Complexity(req.complexity)
        if not batch_eligible and req.priority != 1:
            from bernstein.core.fast_path import TaskLevel, classify_task

            _probe = Task(
                id="__probe__",
                title=req.title,
                description=req.description,
                role=req.role,
                priority=req.priority,
                scope=Scope(req.scope),
                complexity=complexity_val,
                model=req.model,
            )
            _cls = classify_task(_probe)
            batch_eligible = _cls.level in (TaskLevel.L0, TaskLevel.L1)

        # audit-017: Forward retry bookkeeping so the typed fields survive
        # across task clones.  ``None`` => keep Task dataclass default.
        retry_count_raw = getattr(req, "retry_count", None)
        max_retries_raw = getattr(req, "max_retries", None)
        retry_delay_raw = getattr(req, "retry_delay_s", None)
        meta_messages_raw = getattr(req, "meta_messages", None)

        task = Task(
            id=uuid.uuid4().hex[:12],
            title=req.title,
            description=req.description,
            role=req.role,
            priority=req.priority,
            scope=Scope(req.scope),
            complexity=complexity_val,
            estimated_minutes=req.estimated_minutes,
            depends_on=req.depends_on,
            parent_task_id=getattr(req, "parent_task_id", None),
            owned_files=req.owned_files,
            tenant_id=normalize_tenant_id(getattr(req, "tenant_id", "default")),
            cell_id=req.cell_id,
            repo=getattr(req, "repo", None),
            depends_on_repo=getattr(req, "depends_on_repo", None),
            task_type=TaskType(req.task_type),
            upgrade_details=_parse_upgrade_dict(req.upgrade_details),
            model=req.model,
            effort=req.effort,
            batch_eligible=batch_eligible,
            eu_ai_act_risk=getattr(req, "eu_ai_act_risk", "minimal"),
            approval_required=bool(getattr(req, "approval_required", False)),
            risk_level=getattr(req, "risk_level", "low"),
            completion_signals=[CompletionSignal(type=s.type, value=s.value) for s in req.completion_signals],
            slack_context=req.slack_context,
            metadata=getattr(req, "metadata", None) or {},
            parent_session_id=getattr(req, "parent_session_id", None),
            parent_context=getattr(req, "parent_context", None),
            retry_count=int(retry_count_raw) if retry_count_raw is not None else 0,
            max_retries=int(max_retries_raw) if max_retries_raw is not None else 3,
            retry_delay_s=float(retry_delay_raw) if retry_delay_raw is not None else 0.0,
            terminal_reason=getattr(req, "terminal_reason", None),
            max_output_tokens=getattr(req, "max_output_tokens", None),
            meta_messages=list(meta_messages_raw) if meta_messages_raw is not None else [],
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
            if task.depends_on_repo is not None:
                if not task.depends_on:
                    raise HTTPException(
                        status_code=422,
                        detail="depends_on_repo requires at least one depends_on task id",
                    )
                mismatched = [
                    dep
                    for dep in task.depends_on
                    if dep in self._tasks and self._tasks[dep].repo != task.depends_on_repo
                ]
                if mismatched:
                    raise HTTPException(
                        status_code=422,
                        detail=("depends_on_repo does not match dependency repo for task(s): " + ", ".join(mismatched)),
                    )
            self._tasks[task.id] = task
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))

        # SOC 2 audit: log task creation (not a status transition, so lifecycle doesn't cover it)
        from bernstein.core.tasks.lifecycle import _content_hash, get_audit_log

        audit = get_audit_log()
        if audit is not None:
            input_data = {"title": task.title, "role": task.role, "priority": task.priority}
            output_data = {"task_id": task.id, "status": task.status.value}
            audit.log(
                event_type="task.created",
                actor="task_store",
                resource_type="task",
                resource_id=task.id,
                details={
                    "action": "create",
                    "title": task.title,
                    "role": task.role,
                    "priority": task.priority,
                    "input_hash": _content_hash(input_data),
                    "output_hash": _content_hash(output_data),
                },
            )

        return task

    async def create_batch(
        self,
        requests: list[TaskCreateRequest],
        *,
        dedup_by_title: bool = True,
    ) -> tuple[list[Task], list[str]]:
        """Atomically create multiple tasks, deduplicating by title.

        All insertions happen under a single lock acquisition so the batch
        is visible atomically to other callers.  Dependency validation
        errors skip the individual task rather than aborting the batch.

        Args:
            requests: Task creation requests to process.
            dedup_by_title: When True, skip requests whose normalised title
                (lowered + stripped) already exists in the store or earlier
                in the same batch.

        Returns:
            A tuple of (created_tasks, skipped_titles).
        """
        from bernstein.core.tasks.lifecycle import _content_hash, get_audit_log
        from bernstein.core.tasks.models import Complexity, Scope

        created_tasks: list[Task] = []
        skipped_titles: list[str] = []

        async with self._lock:
            existing_titles: set[str] = set()
            if dedup_by_title:
                existing_titles = {t.title.lower().strip() for t in self._tasks.values()}

            for req in requests:
                normalised = req.title.lower().strip()
                if dedup_by_title and normalised in existing_titles:
                    skipped_titles.append(req.title)
                    continue

                # -- build task (mirrors create() logic) --
                batch_eligible: bool = getattr(req, "batch_eligible", False)
                complexity_val = Complexity(req.complexity)
                if not batch_eligible and req.priority != 1:
                    from bernstein.core.fast_path import TaskLevel, classify_task

                    _probe = Task(
                        id="__probe__",
                        title=req.title,
                        description=req.description,
                        role=req.role,
                        priority=req.priority,
                        scope=Scope(req.scope),
                        complexity=complexity_val,
                        model=req.model,
                    )
                    _cls = classify_task(_probe)
                    batch_eligible = _cls.level in (TaskLevel.L0, TaskLevel.L1)

                task = Task(
                    id=uuid.uuid4().hex[:12],
                    title=req.title,
                    description=req.description,
                    role=req.role,
                    priority=req.priority,
                    scope=Scope(req.scope),
                    complexity=complexity_val,
                    estimated_minutes=req.estimated_minutes,
                    depends_on=req.depends_on,
                    parent_task_id=getattr(req, "parent_task_id", None),
                    owned_files=req.owned_files,
                    tenant_id=normalize_tenant_id(getattr(req, "tenant_id", "default")),
                    cell_id=req.cell_id,
                    repo=getattr(req, "repo", None),
                    depends_on_repo=getattr(req, "depends_on_repo", None),
                    task_type=TaskType(req.task_type),
                    upgrade_details=_parse_upgrade_dict(req.upgrade_details),
                    model=req.model,
                    effort=req.effort,
                    batch_eligible=batch_eligible,
                    eu_ai_act_risk=getattr(req, "eu_ai_act_risk", "minimal"),
                    approval_required=bool(getattr(req, "approval_required", False)),
                    risk_level=getattr(req, "risk_level", "low"),
                    completion_signals=[CompletionSignal(type=s.type, value=s.value) for s in req.completion_signals],
                    slack_context=req.slack_context,
                    metadata=getattr(req, "metadata", None) or {},
                    parent_session_id=getattr(req, "parent_session_id", None),
                )

                # -- dependency validation (skip on error, don't abort batch) --
                if task.depends_on:
                    missing = [dep for dep in task.depends_on if dep not in self._tasks]
                    if missing:
                        logger.warning(
                            "create_batch: skipping %r — depends_on references non-existent task(s): %s",
                            task.title,
                            ", ".join(missing),
                        )
                        skipped_titles.append(req.title)
                        continue
                    cycle = self._detect_cycle(self._tasks, task)
                    if cycle is not None:
                        logger.warning(
                            "create_batch: skipping %r — circular dependency: %s",
                            task.title,
                            " -> ".join(cycle),
                        )
                        skipped_titles.append(req.title)
                        continue

                self._tasks[task.id] = task
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))

                if dedup_by_title:
                    existing_titles.add(normalised)

                created_tasks.append(task)

        # Fire audit log entries outside the lock (non-critical I/O)
        audit = get_audit_log()
        if audit is not None:
            for task in created_tasks:
                input_data = {"title": task.title, "role": task.role, "priority": task.priority}
                output_data = {"task_id": task.id, "status": task.status.value}
                audit.log(
                    event_type="task.created",
                    actor="task_store",
                    resource_type="task",
                    resource_id=task.id,
                    details={
                        "action": "create_batch",
                        "title": task.title,
                        "role": task.role,
                        "priority": task.priority,
                        "input_hash": _content_hash(input_data),
                        "output_hash": _content_hash(output_data),
                    },
                )

        return created_tasks, skipped_titles

    async def claim_next(
        self,
        role: str,
        tenant_id: str | None = None,
        claimed_by_session: str | None = None,
        parent_session_id: str | None = None,
    ) -> Task | None:
        """Claim the highest-priority open task for *role*.

        Priority is ascending (1 = critical). Among equal priorities,
        the first inserted task wins (dict insertion order).

        Args:
            role: Agent role to match.
            tenant_id: Optional tenant scope filter.
            claimed_by_session: Parent orchestrator session ID to record as claim owner.
            parent_session_id: If set, only claim tasks whose ``parent_session_id``
                matches this value. Workers from a coordinator should pass their
                coordinator's session ID here so they never steal tasks belonging to
                a different orchestrator namespace.

        Returns:
            The claimed Task, or None if nothing is available.
        """
        async with self._lock:
            pq = self._priority_queues.get((role, TaskStatus.OPEN))
            if not pq:
                return None
            task: Task | None = None
            blocked_entries: list[tuple[int, str]] = []
            normalized_tenant = normalize_tenant_id(tenant_id) if tenant_id is not None else None
            while pq:
                priority, task_id = heapq.heappop(pq)
                candidate = self._tasks.get(task_id)
                if candidate is None or candidate.status != TaskStatus.OPEN:
                    continue
                if normalized_tenant is not None and candidate.tenant_id != normalized_tenant:
                    blocked_entries.append((priority, task_id))
                    continue
                if parent_session_id is not None and candidate.parent_session_id != parent_session_id:
                    blocked_entries.append((priority, task_id))
                    continue
                if not self._dependencies_satisfied(candidate):
                    blocked_entries.append((priority, task_id))
                    continue
                # TASK-003: file ownership overlap check
                overlap_msg = self._check_file_ownership_overlap(candidate)
                if overlap_msg is not None:
                    logger.info("claim_next: skipping %s — %s", task_id, overlap_msg)
                    blocked_entries.append((priority, task_id))
                    continue
                task = candidate
                break
            for entry in blocked_entries:
                heapq.heappush(pq, entry)
            if task is None:
                return None
            self._index_remove(task)
            transition_task(task, TaskStatus.CLAIMED, actor="task_store", reason="claim_next")
            task.claimed_at = time.time()
            task.claimed_by_session = claimed_by_session
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def claim_by_id(
        self,
        task_id: str,
        expected_version: int | None = None,
        agent_role: str | None = None,
        claimed_by_session: str | None = None,
    ) -> Task:
        """Claim a specific task by ID with optional optimistic locking and role matching.

        When ``expected_version`` is provided, the claim only succeeds if
        the task's current version matches (compare-and-swap). This
        prevents two nodes from claiming the same task in a distributed
        cluster.

        When ``agent_role`` is provided, the claim only succeeds if the
        task's role matches the agent's role (role-locked claiming).

        Args:
            task_id: Task identifier.
            expected_version: If set, CAS — reject if task.version != this.
            agent_role: If set, reject if task.role != agent_role.
            claimed_by_session: Parent orchestrator session ID to record as claim owner.

        Returns:
            The claimed Task.

        Raises:
            KeyError: If task_id does not exist.
            ValueError: If expected_version doesn't match (CAS conflict),
                if agent_role doesn't match task role, or if the task is
                not in an OPEN state (already claimed / in progress / terminal).
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if expected_version is not None and task.version != expected_version:
                raise ValueError(
                    f"Version conflict: task {task_id} is at version {task.version}, expected {expected_version}"
                )
            if agent_role is not None and task.role != agent_role:
                raise ValueError(
                    f"role mismatch: task {task_id} requires role '{task.role}', agent has role '{agent_role}'"
                )
            if task.status != TaskStatus.OPEN:
                # audit-014: never silently re-return an already-claimed or
                # terminal task — that enables double-claim. Raise so the
                # HTTP layer can map it to 409 Conflict.
                raise ValueError(
                    f"task {task_id} is not open (status={task.status.value}); "
                    f"cannot claim (already claimed by session "
                    f"{task.claimed_by_session!r})"
                )
            if not self._dependencies_satisfied(task):
                raise ValueError(f"task {task_id} has unresolved dependencies")
            # TASK-003: file ownership overlap check
            overlap_msg = self._check_file_ownership_overlap(task)
            if overlap_msg is not None:
                raise ValueError(overlap_msg)
            self._index_remove(task)
            transition_task(task, TaskStatus.CLAIMED, actor="task_store", reason="claim_by_id")
            task.claimed_at = time.time()
            task.claimed_by_session = claimed_by_session
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def claim_batch(
        self,
        task_ids: list[str],
        agent_id: str,
        agent_role: str | None = None,
        claimed_by_session: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Atomically claim multiple tasks by ID with optional role matching.

        Tasks that are not in OPEN status are skipped and reported as failed.
        If agent_role is provided, tasks with mismatched roles are also
        reported as failed (not claimed).

        Args:
            task_ids: List of task identifiers to claim.
            agent_id: The agent claiming the tasks.
            agent_role: If set, only tasks with matching role can be claimed.
            claimed_by_session: Parent orchestrator session ID to record as claim owner.

        Returns:
            A tuple of (claimed_ids, failed_ids).
        """
        claimed: list[str] = []
        failed: list[str] = []
        async with self._lock:
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task is None or task.status != TaskStatus.OPEN or not self._dependencies_satisfied(task):
                    failed.append(task_id)
                    continue
                if agent_role is not None and task.role != agent_role:
                    failed.append(task_id)
                    continue
                # TASK-003: file ownership overlap check
                if self._check_file_ownership_overlap(task) is not None:
                    failed.append(task_id)
                    continue
                self._index_remove(task)
                transition_task(task, TaskStatus.CLAIMED, actor="task_store", reason=f"claim_batch by {agent_id}")
                task.claimed_at = time.time()
                task.assigned_agent = agent_id
                task.claimed_by_session = claimed_by_session
                task.version += 1
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))
                claimed.append(task_id)
        return claimed, failed

    async def complete(self, task_id: str, result_summary: str) -> Task:
        """Mark a task as done.

        Args:
            task_id: Task identifier.
            result_summary: Non-empty summary of what was done (diff or log reference).

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            ValueError: If *result_summary* is empty (TASK-004).
        """
        # TASK-004: guard — completion requires non-empty data
        if not result_summary or not result_summary.strip():
            raise ValueError(
                f"Cannot complete task {task_id!r}: result_summary must be non-empty (provide diff or log reference)"
            )
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(task, TaskStatus.DONE, actor="task_store", reason="complete")
            task.result_summary = result_summary
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            await self._complete_parent_if_ready(task.parent_task_id)
            return task

    async def close(self, task_id: str) -> Task:
        """Mark a verified task as closed (terminal success state).

        Transitions DONE -> CLOSED after janitor verification and merge.

        Args:
            task_id: Task identifier.

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
            transition_task(task, TaskStatus.CLOSED, actor="task_store", reason="verified and closed")
            task.closed_at = time.time()
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def wait_for_subtasks(self, task_id: str, subtask_count: int) -> Task:
        """Mark a parent task as waiting for its newly created subtasks."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(
                task,
                TaskStatus.WAITING_FOR_SUBTASKS,
                actor="task_store",
                reason=f"split into {subtask_count} subtasks",
            )
            task.result_summary = f"Split into {subtask_count} subtasks"
            task.subtask_wait_started_at = time.time()
            task.version += 1
            self._index_add(task)
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
            self._index_remove(task)
            transition_task(task, TaskStatus.FAILED, actor="task_store", reason=reason)
            task.result_summary = reason
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
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
            transition_task(task, TaskStatus.BLOCKED, actor="task_store", reason=reason)
            task.result_summary = reason
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def _complete_parent_if_ready(self, parent_task_id: str | None) -> None:
        """Complete a waiting parent task when all of its subtasks are done."""
        if parent_task_id is None:
            return
        parent = self._tasks.get(parent_task_id)
        if parent is None or parent.status != TaskStatus.WAITING_FOR_SUBTASKS:
            return
        subtasks = [task for task in self._tasks.values() if task.parent_task_id == parent_task_id]
        if not subtasks or any(task.status != TaskStatus.DONE for task in subtasks):
            return
        self._index_remove(parent)
        transition_task(
            parent,
            TaskStatus.DONE,
            actor="task_store",
            reason="all subtasks completed",
        )
        parent.result_summary = f"Completed via {len(subtasks)} subtasks"
        parent.completed_at = time.time()
        parent.version += 1
        self._index_add(parent)
        completed_at = parent.completed_at
        await self._append_jsonl(self._task_to_record(parent))
        await self._append_archive(parent, completed_at)

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
            _cancellable = {
                TaskStatus.OPEN,
                TaskStatus.CLAIMED,
                TaskStatus.IN_PROGRESS,
                TaskStatus.BLOCKED,
                TaskStatus.WAITING_FOR_SUBTASKS,
                TaskStatus.PLANNED,
            }
            if task.status not in _cancellable:
                raise ValueError(f"Task '{task_id}' cannot be cancelled from status '{task.status.value}'")
            self._index_remove(task)
            transition_task(task, TaskStatus.CANCELLED, actor="task_store", reason=reason)
            task.result_summary = reason
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            return task

    # -- TASK-002: WAITING_FOR_SUBTASKS timeout with escalation ---------------

    SUBTASK_WAIT_TIMEOUT_S: float = _TASK_DEFAULTS.subtask_wait_timeout_s

    async def check_subtask_timeouts(
        self,
        timeout_s: float | None = None,
    ) -> list[Task]:
        """Find WAITING_FOR_SUBTASKS tasks that have exceeded their timeout.

        Timed-out tasks are transitioned to BLOCKED and tagged for escalation
        (``result_summary`` is set to an escalation message).

        Args:
            timeout_s: Override for the default timeout in seconds.

        Returns:
            List of tasks that were escalated due to timeout.
        """
        threshold = timeout_s if timeout_s is not None else self.SUBTASK_WAIT_TIMEOUT_S
        now = time.time()
        escalated: list[Task] = []

        async with self._lock:
            waiting = list(self._by_status.get(TaskStatus.WAITING_FOR_SUBTASKS, {}).values())
            for task in waiting:
                wait_start = task.subtask_wait_started_at or task.created_at
                if now - wait_start < threshold:
                    continue
                self._index_remove(task)
                transition_task(
                    task,
                    TaskStatus.BLOCKED,
                    actor="task_store",
                    reason=f"subtask wait timeout after {threshold:.0f}s",
                )
                task.result_summary = (
                    f"ESCALATION: subtask wait exceeded {threshold:.0f}s — "
                    "requires manager review or human intervention"
                )
                task.version += 1
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))
                escalated.append(task)

        return escalated

    # -- TASK-003: File ownership validation before claim -------------------

    def _check_file_ownership_overlap(
        self,
        task: Task,
    ) -> str | None:
        """Check if a task's owned_files overlap with any active (claimed/in-progress) task.

        Args:
            task: Task about to be claimed.

        Returns:
            Error message describing the conflict, or None if no overlap.
        """
        if not task.owned_files:
            return None

        task_files = set(task.owned_files)
        for active_status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
            for other in self._by_status.get(active_status, {}).values():
                if other.id == task.id:
                    continue
                other_files = set(other.owned_files)
                overlap = task_files & other_files
                if overlap:
                    return f"File ownership conflict: {', '.join(sorted(overlap))} already claimed by task {other.id!r}"
        return None

    # -- TASK-005: Cascading cancellation for subtasks ----------------------

    async def cancel_cascade(self, task_id: str, reason: str) -> list[Task]:
        """Cancel a task and all of its descendant subtasks.

        Walks the subtask tree (``parent_task_id`` references) and cancels
        every non-terminal descendant.  The root task itself is also cancelled.

        Args:
            task_id: Root task identifier.
            reason: Why the tree is being cancelled.

        Returns:
            List of all tasks that were cancelled (root + descendants).

        Raises:
            KeyError: If *task_id* does not exist.
        """
        cancelled: list[Task] = []
        async with self._lock:
            root = self._tasks.get(task_id)
            if root is None:
                raise KeyError(task_id)

            # Collect all descendants via BFS
            to_cancel: list[str] = [task_id]
            idx = 0
            while idx < len(to_cancel):
                parent_id = to_cancel[idx]
                idx += 1
                for t in self._tasks.values():
                    if t.parent_task_id == parent_id and t.id not in to_cancel:
                        to_cancel.append(t.id)

            # Cancel each in BFS order (parent before children)
            cancellable = {
                TaskStatus.OPEN,
                TaskStatus.CLAIMED,
                TaskStatus.IN_PROGRESS,
                TaskStatus.BLOCKED,
                TaskStatus.WAITING_FOR_SUBTASKS,
                TaskStatus.PLANNED,
            }
            for tid in to_cancel:
                task = self._tasks.get(tid)
                if task is None or task.status not in cancellable:
                    continue
                self._index_remove(task)
                transition_task(
                    task,
                    TaskStatus.CANCELLED,
                    actor="task_store",
                    reason=reason if tid == task_id else f"parent {task_id} cancelled: {reason}",
                )
                task.result_summary = reason if tid == task_id else f"Cascade: parent {task_id} cancelled"
                task.completed_at = time.time()
                task.version += 1
                self._index_add(task)
                completed_at = task.completed_at
                await self._append_jsonl(self._task_to_record(task))
                await self._append_archive(task, completed_at)
                cancelled.append(task)

        return cancelled

    async def update(
        self,
        task_id: str,
        role: str | None,
        priority: int | None,
        model: str | None = None,
    ) -> Task:
        """Update mutable task fields (role, priority, model) — manager corrections.

        Only open or failed tasks can be reassigned; claimed/in-progress tasks
        are left to finish before the new assignment takes effect.

        Args:
            task_id: Task identifier.
            role: New role if provided.
            priority: New priority if provided.
            model: New model hint if provided (e.g. "haiku", "sonnet", "opus").

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if role is not None and role != task.role:
                # Role change requires re-indexing (role is part of secondary index key)
                self._index_remove(task)
                task.role = role
                self._index_add(task)
            if priority is not None:
                task.priority = priority
            if model is not None:
                task.model = model
            task.version += 1
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def prioritize(self, task_id: str) -> Task:
        """Set a task's priority to 0 (highest) so it is claimed next.

        Works on any non-terminal task (open, claimed, in_progress).

        Args:
            task_id: Task identifier.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            task.priority = 0
            task.version += 1
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def force_claim(self, task_id: str) -> Task:
        """Force a task back to open with priority 0 so it is claimed immediately.

        If the task is already open its priority is set to 0 and it stays open.
        If it is in a terminal state (done, failed, cancelled) it is returned
        unchanged — only open/claimed/in_progress tasks can be force-claimed.

        Args:
            task_id: Task identifier.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            ValueError: If the task is in a terminal state and cannot be re-queued.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
            if task.status in terminal:
                raise ValueError(
                    f"Task '{task_id}' is in terminal state '{task.status.value}' and cannot be force-claimed"
                )
            if task.status != TaskStatus.OPEN:
                # Reset claimed/in_progress back to open
                self._index_remove(task)
                transition_task(task, TaskStatus.OPEN, actor="task_store", reason="force_claim")
                self._index_add(task)
            task.priority = 0
            task.claimed_at = None  # Clear claim timestamp on force-claim
            task.claimed_by_session = None  # Clear ownership on force-claim
            task.version += 1
            await self._append_jsonl(self._task_to_record(task))
            return task

    # -- query / listing (delegated from task_store_index) ------------------

    def list_tasks(
        self,
        status: str | None = None,
        cell_id: str | None = None,
        tenant_id: str | None = None,
        claimed_by_session: str | None = None,
        parent_session_id: str | None = None,
    ) -> list[Task]:
        """Return all tasks, optionally filtered by status, cell_id, and/or claim owner.

        When status='open', tasks whose dependencies are not all done are
        excluded (they are not yet available for agents to pick up).

        Args:
            status: If provided, only tasks with this status are returned.
            cell_id: If provided, only tasks in this cell are returned.
            tenant_id: If provided, only tasks in this tenant are returned.
            claimed_by_session: If provided, only tasks claimed by this
                parent session are returned.
            parent_session_id: If provided, only tasks whose ``parent_session_id``
                matches (tasks scoped to this coordinator session) are returned.

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
        if tenant_id is not None:
            normalized_tenant = normalize_tenant_id(tenant_id)
            tasks = [t for t in tasks if t.tenant_id == normalized_tenant]
        if claimed_by_session is not None:
            tasks = [t for t in tasks if t.claimed_by_session == claimed_by_session]
        if parent_session_id is not None:
            tasks = [t for t in tasks if t.parent_session_id == parent_session_id]
        if status == "open":
            tasks = [t for t in tasks if self._dependencies_satisfied(t)]
        return tasks

    def count_by_status(self, tenant_id: str | None = None) -> dict[str, int]:
        """Return task counts per status without materialising task lists.

        This is O(N) in the worst case when tenant filtering is applied, but
        avoids serialising full task bodies — ideal for the /tasks/counts
        endpoint that the orchestrator polls every tick.

        Args:
            tenant_id: If provided, only count tasks belonging to this tenant.

        Returns:
            Dict mapping status name -> count, plus a ``total`` key.
        """
        if tenant_id is not None:
            normalized = normalize_tenant_id(tenant_id)
            counts: dict[str, int] = {}
            total = 0
            for ts, bucket in self._by_status.items():
                n = sum(1 for t in bucket.values() if t.tenant_id == normalized)
                counts[ts.value] = n
                total += n
            counts["total"] = total
            return counts

        counts = {ts.value: len(bucket) for ts, bucket in self._by_status.items()}
        counts["total"] = len(self._tasks)
        return counts

    def get_task(self, task_id: str) -> Task | None:
        """Look up a single task by id."""
        return self._tasks.get(task_id)

    def update_task_priority(self, task_id: str, new_priority: int, version: int) -> Task | None:
        """Update task priority with optimistic locking.

        Args:
            task_id: Task identifier.
            new_priority: New priority value.
            version: Expected version for optimistic locking.

        Returns:
            Updated Task, or None if not found or version mismatch.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None

        if task.version != version:
            return None

        task.priority = new_priority
        task.version += 1
        self._index_add(task)

        return task

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
            agent = self._agents[agent_id]
            agent.heartbeat_ts = now
            if agent.status != status:
                try:
                    transition_agent(agent, status, actor="heartbeat", reason=f"agent {agent_id} self-report")
                except IllegalTransitionError:
                    from bernstein.core.sanitize import sanitize_log

                    logger.warning(
                        "Ignoring illegal heartbeat transition %s -> %s for %s",
                        sanitize_log(str(agent.status)),
                        sanitize_log(str(status)),
                        sanitize_log(str(agent_id)),
                    )
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
            if agent.status == "dead":
                continue
            transition_agent(agent, "dead", actor="task_store", reason="stale heartbeat")
            count += 1
        return count

    # -- status summary / cost tracking --------------------------------------

    def status_summary(self) -> dict[str, Any]:
        """Return aggregated task counts for the dashboard."""
        role_counts = self._build_role_counts()
        total_cost, cost_by_role = self._compute_costs()

        per_role = []
        for role, counts in sorted(role_counts.items()):
            entry: dict[str, Any] = {"role": role, **counts}
            if role in cost_by_role:
                entry["cost_usd"] = round(cost_by_role[role], 4)
            per_role.append(entry)

        summary: dict[str, Any] = {
            "total": len(self._tasks),
            "open": len(self._by_status.get(TaskStatus.OPEN, {})),
            "claimed": len(self._by_status.get(TaskStatus.CLAIMED, {})),
            "done": len(self._by_status.get(TaskStatus.DONE, {})),
            "failed": len(self._by_status.get(TaskStatus.FAILED, {})),
            "per_role": per_role,
            "total_cost_usd": round(total_cost, 4),
        }
        self._attach_bandit_stats(summary)
        return summary

    def _build_role_counts(self) -> dict[str, dict[str, int]]:
        """Build per-role breakdown across all statuses."""
        role_counts: dict[str, dict[str, int]] = {}
        for task in self._tasks.values():
            if task.role not in role_counts:
                role_counts[task.role] = {"open": 0, "claimed": 0, "done": 0, "failed": 0}
            status_key = task.status.value
            if status_key in role_counts[task.role]:
                role_counts[task.role][status_key] += 1
        return role_counts

    def _compute_costs(self) -> tuple[float, dict[str, float]]:
        """Compute total cost from in-memory tasks and metrics JSONL."""
        total_cost = sum(t.cost_usd for t in self._tasks.values() if hasattr(t, "cost_usd") and t.cost_usd)
        cost_by_role = self._read_cost_by_role()
        metrics_cost = sum(cost_by_role.values())
        if metrics_cost > total_cost:
            total_cost = metrics_cost
        return total_cost, cost_by_role

    def _attach_bandit_stats(self, summary: dict[str, Any]) -> None:
        """Attach bandit routing stats to summary if available."""
        bandit_state_path = self._jsonl_path.parent.parent / "routing" / "bandit_state.json"
        if not bandit_state_path.exists():
            return
        try:
            bandit_data = json.loads(bandit_state_path.read_text())
            summary["routing"] = {
                "mode": bandit_data.get("mode", "bandit"),
                "total_completions": bandit_data.get("total_completions", 0),
                "selection_frequency": bandit_data.get("selection_counts", {}),
                "exploration_stats": bandit_data.get("exploration_stats", {}),
                "shadow_stats": bandit_data.get("shadow_stats", {}),
            }
        except json.JSONDecodeError:
            logger.warning("Corrupted bandit state at %s — skipping", bandit_state_path)
        except OSError as exc:
            logger.warning("Cannot read bandit state at %s: %s", bandit_state_path, exc)

    def recently_completed(self, grace_ms: int = PANEL_GRACE_MS) -> list[Task]:
        """Return tasks completed within the grace period.

        These tasks should remain visible in status panels before eviction.

        Args:
            grace_ms: Grace window in milliseconds (default: PANEL_GRACE_MS).

        Returns:
            List of tasks that completed within the grace window, newest first.
        """
        cutoff = time.time() - grace_ms / 1000.0
        terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        result: list[Task] = []
        for status in terminal:
            for task in self._by_status.get(status, {}).values():
                if task.completed_at is not None and task.completed_at >= cutoff:
                    result.append(task)
        result.sort(key=lambda t: t.completed_at or 0.0, reverse=True)
        return result

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
                logger.error(
                    "Corrupted metrics record in %s — skipping: %s",
                    self._metrics_jsonl_path,
                    raw_line[:500],
                )
                continue
            role = record_data.get("role", "")
            cost = record_data.get("cost_usd")
            if role and isinstance(cost, (int, float)):
                self._cost_cache[role] = self._cost_cache.get(role, 0.0) + float(cost)
        self._cost_cache_offset = new_offset
        self._cost_cache_mtime = mtime
        return dict(self._cost_cache)

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

    @property
    def jsonl_path(self) -> Path:
        """Path to the primary task JSONL file."""
        return self._jsonl_path

    @property
    def metrics_jsonl_path(self) -> Path:
        """Path to the metrics JSONL file (for dashboard cost history)."""
        return self._metrics_jsonl_path

    @property
    def archive_path(self) -> Path:
        """Path to the append-only archive JSONL file."""
        return self._archive_path
