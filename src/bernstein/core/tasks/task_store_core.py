"""CRUD operations and the TaskStore class - core task mutations.

All task mutations go through this class so the JSONL log stays consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, cast

from fastapi import HTTPException
from typing_extensions import TypedDict

from bernstein.core.defaults import TASK as _TASK_DEFAULTS
from bernstein.core.hook_events import HookEvent
from bernstein.core.persistence.durable_write import fsynced_write
from bernstein.core.persistence.runtime_state import rotate_log_file
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.tasks.errors import TaskDomainError
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

    from bernstein.core.tasks.contracts import ContractViolation, WorkerCompletion, WorkerRefusal

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
    # typed retry bookkeeping (optional for backward compat).
    retry_count: NotRequired[int]
    max_retries: NotRequired[int]
    retry_delay_s: NotRequired[float]
    terminal_reason: NotRequired[str | None]
    max_output_tokens: NotRequired[int | None]
    meta_messages: NotRequired[list[str]]
    metadata: NotRequired[dict[str, Any]]
    # Explicit compute_max_turns() override (see claude_max_turns.py). Optional
    # for backward compat with records written before this field existed.
    max_turns: NotRequired[int | None]


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
    cli: str | None
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

    # Retry bookkeeping: typed retry fields are the single source
    # of truth.  When orchestrator clones a task for retry, it passes
    # ``retry_count=previous+1`` in the request.  These fields are optional on
    # the wire (``None`` / missing => fall back to the Task dataclass default).
    retry_count: int | None
    max_retries: int | None
    retry_delay_s: float | None
    terminal_reason: str | None
    max_output_tokens: int | None
    max_turns: int | None

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

# reason used when auto-failing a task due to empty result_summary.
_EMPTY_COMPLETION_REASON = "completion missing summary"

# rotate the per-task progress JSONL file once it exceeds 5 MiB.
# Old rollovers become ``{task_id}.jsonl.1``; replay also reads them so no
# history is silently dropped between restarts.
_PROGRESS_ROTATE_BYTES: int = 5 * 1024 * 1024


class EmptyCompletionError(TaskDomainError):
    """Raised when ``complete()`` is called with an empty ``result_summary``.

    Before raising, ``complete()`` auto-transitions the task to ``FAILED``
    with ``reason=_EMPTY_COMPLETION_REASON`` so the slot is freed and the
    watchdog does not need to flip the task later.  The HTTP layer maps
    this to a 422 response so the client knows the task was marked failed.
    """

    def __init__(self, task_id: str, task: Task | None = None) -> None:
        self.task_id = task_id
        self.task = task
        super().__init__(
            f"Cannot complete task {task_id!r}: result_summary must be non-empty "
            f"(provide diff or log reference). Task auto-failed with "
            f"reason={_EMPTY_COMPLETION_REASON!r}."
        )


class TaskStore:
    """Thread-safe in-memory task store with JSONL persistence.

    All mutations go through this class so the JSONL log stays consistent.

    Concurrency model:
        Mutations are coordinated by an in-process ``asyncio.Lock`` and the
        JSONL append path does NOT take an OS-level file lock (no
        ``fcntl.flock``). The store is therefore **single-process only** -
        running the server under ``uvicorn --workers N`` (or with
        ``WEB_CONCURRENCY>1``) interleaves appends, produces torn lines
        that ``replay_jsonl`` silently drops, and lets multiple workers
        claim the same top-priority task.

        The server enforces this via
        :func:`bernstein.core.server.server_app.preflight_multi_worker_guard`,
        which refuses to boot with ``workers>1``. Long-term multi-worker
        coordination (``fcntl.flock`` / SQLite WAL / Redis) is tracked as a
        separate ticket.
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
        # parent_task_id -> set of child task ids. Maintained alongside
        # ``_by_status`` and ``_by_role_status`` inside ``self._lock`` so route
        # callers can count subtasks without walking the full task list.
        self._by_parent: dict[str, set[str]] = {}
        # Min-heaps keyed by (role, status) - entries are (priority, task_id)
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
        # directory for per-task progress JSONL files. Each
        # ``add_progress``/``add_snapshot`` call appends (and fsyncs) a line
        # here so that progress history survives a server crash.  Rebuilt on
        # startup by ``replay_progress()``.
        self._progress_dir: Path = jsonl_path.parent / "progress"

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

    def _parent_index_add(self, task: Task) -> None:
        """Add *task* to the parent->children index.

        ``_by_parent`` tracks task identity (not status), so this is invoked
        only when a task first enters ``self._tasks`` (create, batch create,
        replay). Callers must hold ``self._lock`` for create paths; replay
        runs single-threaded at startup before the lock is in use.
        """
        if task.parent_task_id is None:
            return
        children = self._by_parent.setdefault(task.parent_task_id, set())
        children.add(task.id)

    def _parent_index_remove(self, task: Task) -> None:
        """Remove *task* from the parent->children index.

        Currently unused (the store soft-archives via status; it never deletes
        records from ``self._tasks``). Provided for symmetry so that any future
        hard-delete path can keep the index consistent.
        """
        if task.parent_task_id is None:
            return
        children = self._by_parent.get(task.parent_task_id)
        if children is None:
            return
        children.discard(task.id)
        if not children:
            self._by_parent.pop(task.parent_task_id, None)

    def count_subtasks(self, parent_task_id: str) -> int:
        """Return the number of direct subtasks for *parent_task_id*.

        O(1) lookup via the ``_by_parent`` index. Replaces the
        ``sum(1 for t in store.list_tasks() ...)`` pattern in the
        self-create route, which materialised the whole task list per call.
        """
        return len(self._by_parent.get(parent_task_id, ()))

    # -- persistence --------------------------------------------------------

    def replay_jsonl(self) -> None:
        """Rebuild state from the JSONL log on disk.

        Each line is a JSON object with at least ``id`` and ``status``.
        Lines are replayed in order so the last write wins.

        after the task log is replayed we also replay the
        per-task progress JSONL files so ``progress_log`` and
        ``_progress_snapshots`` survive a server restart.  Progress replay
        runs unconditionally so fresh installations with only progress on
        disk (no committed task log yet) still hydrate correctly.
        """
        if self._jsonl_path.exists():
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
                        "Corrupted JSONL record at %s:%d - skipping: %s",
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
                    self._parent_index_add(task)
        self.replay_progress()

    def recover_stale_claimed_tasks(self) -> int:
        """Reset CLAIMED and IN_PROGRESS tasks to OPEN after a server restart.

        When the server process is killed mid-task, all CLAIMED and IN_PROGRESS
        tasks have no active agent.  This method re-queues them as OPEN so a
        fresh agent can pick them up.  Call this once after ``replay_jsonl()``
        during startup.

        The release is persisted to the JSONL log synchronously (bug
        ````): without this, the in-memory reset is lost on crash and
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

    def reopen_tasks_for_node(self, node_id: str) -> int:
        """Reset a departed cluster node's in-flight tasks back to OPEN.

        A cluster worker claims through ``/tasks/next`` with its node id
        recorded as the claim owner (``claimed_by_session``). When that node
        leaves -- by heartbeat timeout (crash) or graceful unregister -- its
        CLAIMED/IN_PROGRESS tasks would otherwise stay claimed forever with no
        live agent, and no reaper would ever release them (#2801). This
        re-queues exactly that node's tasks so a surviving worker can pick them
        up.

        Mirrors :meth:`recover_stale_claimed_tasks` but scoped to one node.
        The method performs no ``await``, so it runs atomically with respect to
        concurrent claims in the single-threaded async server loop and is safe
        to call from both the async node reaper and the sync unregister route.

        Args:
            node_id: Owning node id whose claims should be released.

        Returns:
            Number of tasks reset to open.
        """
        if not node_id:
            return 0
        reset_count = 0
        reset_tasks: list[Task] = []
        for stale_status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
            for task in list(self._by_status.get(stale_status, {}).values()):
                if task.claimed_by_session != node_id:
                    continue
                self._index_remove(task)
                transition_task(
                    task,
                    TaskStatus.OPEN,
                    actor="task_store",
                    reason="recover_node_departed",
                )
                task.claimed_at = None
                task.claimed_by_session = None
                task.version += 1
                self._index_add(task)
                reset_tasks.append(task)
                reset_count += 1
        if reset_count:
            for task in reset_tasks:
                self._append_jsonl_sync(self._task_to_record(task))
            logger.info(
                "reopen_tasks_for_node: reset %d task(s) for departed node %s",
                reset_count,
                sanitize_log(node_id),
            )
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
        with fsynced_write(self._jsonl_path) as handle:
            handle.write(line)

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
            with fsynced_write(self._jsonl_path) as f:
                f.write(data)

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
                            "Corrupted archive record at %s:%d - skipping: %s",
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
            with fsynced_write(self._archive_path) as f:
                f.write(line)

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

    # -- per-task progress JSONL persistence ---------------------

    def _progress_file(self, task_id: str) -> Path:
        """Return the JSONL file storing progress/snapshot records for *task_id*.

        The parent directory is created on demand so callers never have to
        check it first.
        """
        self._progress_dir.mkdir(parents=True, exist_ok=True)
        return self._progress_dir / f"{task_id}.jsonl"

    def _append_progress_record(self, task_id: str, record: dict[str, Any]) -> None:
        """Append *record* to the task's progress JSONL file with fsync.

        The write is synchronous on purpose: progress is often posted from
        short-lived agents that may be SIGKILL'd seconds later.  Rotation is
        attempted before every append so that a single hot task cannot grow
        the file without bound.
        """
        path = self._progress_file(task_id)
        rotate_log_file(path, max_bytes=_PROGRESS_ROTATE_BYTES, max_backups=1)
        line = json.dumps(record, default=str) + "\n"
        try:
            with fsynced_write(path) as handle:
                handle.write(line)
        except OSError as exc:
            # Progress is advisory: the in-memory log is already updated and
            # the task itself has its own durable JSONL.  Log and move on so
            # a full disk cannot break ``/progress``.
            logger.warning("Failed to persist progress for %s: %s", task_id, exc)

    def replay_progress(self) -> None:
        """Rebuild ``progress_log`` and ``_progress_snapshots`` from disk.

        Scans ``.sdd/runtime/progress/*.jsonl`` (and any rotated ``*.jsonl.N``
        companions) and replays each record into the matching in-memory
        task.  Safe to call multiple times: entries are appended in file
        order, so the resulting list matches what was on disk.

        Callers are expected to invoke this after ``replay_jsonl()`` so the
        target tasks exist before progress is applied.
        """
        if not self._progress_dir.exists():
            return
        # Collect the live file plus any rotated backups, grouped by task id.
        per_task: dict[str, list[Path]] = {}
        for entry in self._progress_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(".jsonl"):
                task_id = name[: -len(".jsonl")]
                per_task.setdefault(task_id, []).append(entry)
            elif ".jsonl." in name:
                base, _, _ = name.rpartition(".jsonl.")
                per_task.setdefault(base, []).append(entry)

        def _order(path: Path) -> tuple[int, str]:
            # Rotated backups sort before the live file: ``.jsonl.2`` before
            # ``.jsonl.1`` before ``.jsonl`` (older entries first).
            suffix = path.suffix.lstrip(".")
            if suffix.isdigit():
                return (-int(suffix), path.name)
            return (0, path.name)

        for task_id, paths in per_task.items():
            task = self._tasks.get(task_id)
            if task is None:
                # Owning task has been purged - progress is orphaned, skip.
                continue
            progress: list[ProgressEntry] = cast("list[ProgressEntry]", task.progress_log)  # type: ignore[reportUnknownMemberType]
            snap_q = self._progress_snapshots.setdefault(task_id, deque(maxlen=10))
            for path in sorted(paths, key=_order):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    logger.warning("Cannot read progress file %s: %s", path, exc)
                    continue
                for line_num, raw in enumerate(lines, 1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        logger.error(
                            "Corrupted progress record at %s:%d - skipping: %s",
                            path,
                            line_num,
                            stripped[:500],
                        )
                        continue
                    kind = record.get("kind")
                    if kind == "entry":
                        progress.append(
                            {
                                "timestamp": float(record.get("timestamp", 0.0)),
                                "message": str(record.get("message", "")),
                                "percent": int(record.get("percent", 0)),
                            }
                        )
                    elif kind == "snapshot":
                        snap_q.append(
                            ProgressSnapshot(
                                timestamp=float(record.get("timestamp", 0.0)),
                                files_changed=int(record.get("files_changed", 0)),
                                tests_passing=int(record.get("tests_passing", -1)),
                                errors=int(record.get("errors", 0)),
                                last_file=str(record.get("last_file", "")),
                            )
                        )
                    # Unknown kinds are tolerated for forward compatibility.

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
            # retry bookkeeping (typed source of truth).
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "retry_delay_s": task.retry_delay_s,
            "terminal_reason": task.terminal_reason,
            "max_output_tokens": task.max_output_tokens,
            "meta_messages": list(task.meta_messages),
            "metadata": dict(task.metadata),
            "max_turns": task.max_turns,
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

        # DFS from new_task only - existing tasks were validated on insertion.
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
        # A dependency is satisfied by either terminal-success status: tasks
        # move from "done" to "closed" once their agent is reaped and its
        # branch merged (the store soft-archives via status). Accepting only
        # "done" here rejected claims of dependents whose dependency had
        # already completed and been closed.
        completed_tasks = list(self._by_status[TaskStatus.DONE].values()) + list(
            self._by_status[TaskStatus.CLOSED].values()
        )
        done_ids = {done_task.id for done_task in completed_tasks}
        if not all(dep in done_ids for dep in task.depends_on):
            return False
        if task.depends_on_repo is None:
            return True
        if not task.depends_on:
            return any(done_task.repo == task.depends_on_repo for done_task in completed_tasks)
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

        # Forward retry bookkeeping so the typed fields survive
        # across task clones.  ``None`` => keep Task dataclass default.
        retry_count_raw = getattr(req, "retry_count", None)
        max_retries_raw = getattr(req, "max_retries", None)
        retry_delay_raw = getattr(req, "retry_delay_s", None)
        meta_messages_raw = getattr(req, "meta_messages", None)
        max_turns_raw = getattr(req, "max_turns", None)
        logger.info(
            "TaskStore.create: max_turns=%r for title=%r (None => auto-computed at spawn time)",
            max_turns_raw,
            sanitize_log(req.title),
        )

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
            cli=getattr(req, "cli", None),
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
            max_turns=int(max_turns_raw) if max_turns_raw is not None else None,
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
            self._parent_index_add(task)
            await self._append_jsonl(self._task_to_record(task))

        # SOC 2 audit: log task creation (not a status transition, so lifecycle doesn't cover it)
        from bernstein.core.tasks.lifecycle import _content_hash, get_audit_log

        audit = get_audit_log()
        if audit is not None:
            input_data = {"title": task.title, "role": task.role, "priority": task.priority}
            output_data = {"task_id": task.id, "status": task.status.value}
            audit.log(
                event_type=HookEvent.TASK_CREATED.value,
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

    # -- clearance-gate wiring (#2556) --------------------------------------
    # A ``blocker`` bulletin signal materializes a clearance task with a
    # deterministic id (derived from the blocker content hash) plus injected
    # ``depends_on`` edges onto the open dependent tasks in scope. The clearance
    # task participates as an ordinary dependency edge, so ``claim_next`` and the
    # ``list_tasks(status="open")`` exclusion already withhold dependent work
    # until the gate is terminal. These additive helpers let the deterministic
    # projection create a task at a chosen id and inject the edges without going
    # through the id-generating ``create`` path.

    async def create_gate_task(
        self,
        *,
        clearance_task_id: str,
        title: str,
        role: str,
        cell_id: str | None = None,
        priority: int = 1,
        tenant_id: str = "default",
    ) -> Task:
        """Create a clearance task with a caller-supplied deterministic id.

        Args:
            clearance_task_id: The projected clearance-task id (``clearance-…``).
            title: Human-readable gate title.
            role: Role lane the clearance task belongs to (kept distinct from
                worker roles so workers never claim the gate itself).
            cell_id: Cell scope the gate belongs to.
            priority: Task priority (defaults to 1 so the gate surfaces first).
            tenant_id: Tenant scope.

        Returns:
            The created clearance :class:`Task`.

        Raises:
            ValueError: If a task with ``clearance_task_id`` already exists.
        """
        task = Task(
            id=clearance_task_id,
            title=title,
            description=f"Clearance gate for a bulletin blocker in cell {cell_id or 'global'}.",
            role=role,
            priority=priority,
            status=TaskStatus.OPEN,
            cell_id=cell_id,
            tenant_id=normalize_tenant_id(tenant_id),
        )
        async with self._lock:
            if clearance_task_id in self._tasks:
                raise ValueError(f"clearance task already exists: {clearance_task_id}")
            self._tasks[task.id] = task
            self._index_add(task)
            self._parent_index_add(task)
            await self._append_jsonl(self._task_to_record(task))
        return task

    async def create_gate_with_edges(
        self,
        *,
        clearance_task_id: str,
        title: str,
        role: str,
        cell_id: str | None = None,
        priority: int = 1,
        tenant_id: str = "default",
        dependent_task_ids: Sequence[str] | None = None,
    ) -> tuple[Task, list[str]]:
        """Create a clearance gate and inject every dependent edge atomically.

        Creating the gate task and injecting the ``depends_on`` edges as two
        separate lock acquisitions leaves a claim race window: between the two
        steps the gate exists but the dependents are not yet gated, so
        ``claim_next`` can hand out work the gate was meant to withhold. This
        method performs the whole mutation under a single lock acquisition and
        re-selects the OPEN dependents *inside* that lock, so no claim can
        interleave and no dependent that was open at gate time is missed.

        The mutation is transactional: if any step raises (for example a failed
        journal append), every in-memory change made by this call is rolled
        back, so an interrupted materialization leaves neither an orphan gate
        task nor an orphan edge.

        Args:
            clearance_task_id: The projected clearance-task id (``clearance-…``).
            title: Human-readable gate title.
            role: Role lane the clearance task belongs to (kept distinct from
                worker roles so workers never claim the gate itself).
            cell_id: Cell scope the gate belongs to.
            priority: Task priority (defaults to 1 so the gate surfaces first).
            tenant_id: Tenant scope.
            dependent_task_ids: Optional explicit dependent set. When supplied,
                only these ids receive an edge (intersected with the OPEN tasks
                re-selected under the lock). When ``None``, every OPEN task in
                the gate's cell scope **and tenant** is gated; tasks belonging
                to another tenant are never gated by this gate.

        Returns:
            A tuple of ``(gate_task, injected_dependent_ids)`` where the id list
            is sorted and de-duplicated.

        Raises:
            ValueError: If a task with ``clearance_task_id`` already exists.
        """
        gate = Task(
            id=clearance_task_id,
            title=title,
            description=f"Clearance gate for a bulletin blocker in cell {cell_id or 'global'}.",
            role=role,
            priority=priority,
            status=TaskStatus.OPEN,
            cell_id=cell_id,
            tenant_id=normalize_tenant_id(tenant_id),
        )
        requested = set(dependent_task_ids) if dependent_task_ids is not None else None

        async with self._lock:
            if clearance_task_id in self._tasks:
                raise ValueError(f"clearance task already exists: {clearance_task_id}")

            # Re-select the OPEN dependents under the same lock that creates the
            # gate. A gate never gates another gate, and never gates itself.
            # Scope by tenant as well as cell. Every other selection path in
            # this store filters candidates by normalized tenant; omitting it
            # here would let a gate created for one tenant inject depends_on
            # edges onto another tenant's OPEN tasks in the same cell, which is
            # a containment failure rather than a cosmetic gap (#2648).
            candidates = [
                task.id
                for task in self._by_status[TaskStatus.OPEN].values()
                if task.id != clearance_task_id
                and not task.id.startswith("clearance-")
                and (cell_id is None or task.cell_id == cell_id)
                and task.tenant_id == gate.tenant_id
                and (requested is None or task.id in requested)
            ]
            targets = sorted(set(candidates))

            edged: list[Task] = []
            created = False
            staged = len(self._write_buffer)
            try:
                self._tasks[gate.id] = gate
                self._index_add(gate)
                self._parent_index_add(gate)
                created = True

                for dependent_id in targets:
                    dependent = self._tasks[dependent_id]
                    if clearance_task_id in dependent.depends_on:
                        continue
                    dependent.depends_on = [*dependent.depends_on, clearance_task_id]
                    dependent.version += 1
                    edged.append(dependent)

                # Stage the gate row and every edge row, then flush once. The
                # buffer is written by a single fsynced write, so the journal
                # never records a gate without its edges: replaying a crashed
                # materialization restores the whole gate or none of it.
                rows = [self._task_to_record(task) for task in (gate, *edged)]
                lines = [json.dumps(record, default=str) + "\n" for record in rows]
                self._write_buffer.extend(lines)
                await self._flush_buffer_unlocked()
            except BaseException:
                # Roll back every in-memory mutation so a partial failure leaves
                # neither an orphan gate nor an orphan edge behind.
                del self._write_buffer[staged:]
                for dependent in edged:
                    dependent.depends_on = [d for d in dependent.depends_on if d != clearance_task_id]
                    dependent.version -= 1
                if created:
                    self._index_remove(gate)
                    self._parent_index_remove(gate)
                    self._tasks.pop(gate.id, None)
                raise

            # Mirror into the tenant backlog only after the primary journal
            # write committed, so a rolled-back gate never appears in the
            # tenant view. The mirror is a derived view: by this point the
            # mutation is durable, so a mirror failure is logged rather than
            # raised, which would report failure for a committed gate and
            # strand it with no receipt.
            for record, line in zip(rows, lines, strict=True):
                try:
                    await self._append_tenant_backlog_record(record, line)
                except Exception:
                    logger.exception(
                        "tenant backlog mirror failed for clearance gate %s; the gate is committed",
                        clearance_task_id,
                    )

        return gate, targets

    async def inject_dependency(self, task_id: str, depends_on_id: str) -> Task:
        """Inject a ``depends_on`` edge onto an existing non-terminal task.

        The edge is idempotent (a repeated injection is a no-op) and the target
        dependency must already exist so the dependency gate can resolve it. The
        task's version is bumped and the change is persisted.

        Args:
            task_id: The dependent task receiving the edge.
            depends_on_id: The clearance (or other) task it must now wait on.

        Returns:
            The updated dependent :class:`Task`.

        Raises:
            KeyError: If either task id is unknown.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if depends_on_id not in self._tasks:
                raise KeyError(depends_on_id)
            if depends_on_id not in task.depends_on:
                task.depends_on = [*task.depends_on, depends_on_id]
                task.version += 1
                await self._append_jsonl(self._task_to_record(task))
        return task

    async def resolve_gate_task(self, clearance_task_id: str, *, resolution: str = "cleared") -> Task:
        """Mark a clearance task terminal so its dependents are released.

        Transitions the gate ``OPEN -> CLAIMED -> DONE`` (both legal steps) so
        the terminal ``DONE`` status satisfies the dependency gate and dependent
        work becomes claimable again. ``resolution`` is recorded in the result
        summary for the archive trail.

        Args:
            clearance_task_id: The clearance task to resolve.
            resolution: ``cleared`` or ``expired`` (recorded in the summary).

        Returns:
            The resolved clearance :class:`Task`.

        Raises:
            KeyError: If ``clearance_task_id`` is unknown.
            ClearanceResolutionRefusal: If ``resolution`` is outside
                ``{cleared, expired}``. The refusal happens before the lock is
                taken, so an unrecognised resolution never reaches the task
                state or the result summary (#2648).
        """
        from bernstein.core.security.audit_chain import (
            GATE_TERMINAL_RESOLUTIONS,
            validate_gate_resolution,
        )

        validate_gate_resolution(resolution, allowed=GATE_TERMINAL_RESOLUTIONS)
        async with self._lock:
            task = self._tasks.get(clearance_task_id)
            if task is None:
                raise KeyError(clearance_task_id)
            if task.status not in (TaskStatus.DONE, TaskStatus.CLOSED):
                self._index_remove(task)
                if task.status == TaskStatus.OPEN:
                    transition_task(task, TaskStatus.CLAIMED, actor="clearance_gate", reason="gate resolve")
                transition_task(task, TaskStatus.DONE, actor="clearance_gate", reason=f"gate {resolution}")
                task.result_summary = f"clearance {resolution}"
                task.completed_at = time.time()
                task.version += 1
                self._index_add(task)
                await self._append_jsonl(self._task_to_record(task))
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
                    cli=getattr(req, "cli", None),
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
                            "create_batch: skipping %r - depends_on references non-existent task(s): %s",
                            task.title,
                            ", ".join(missing),
                        )
                        skipped_titles.append(req.title)
                        continue
                    cycle = self._detect_cycle(self._tasks, task)
                    if cycle is not None:
                        logger.warning(
                            "create_batch: skipping %r - circular dependency: %s",
                            task.title,
                            " -> ".join(cycle),
                        )
                        skipped_titles.append(req.title)
                        continue

                self._tasks[task.id] = task
                self._index_add(task)
                self._parent_index_add(task)
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
                    event_type=HookEvent.TASK_CREATED.value,
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
                # Lazy-delete stale heap entries left by priority mutations
                # (prioritize/update/update_task_priority/force_claim re-add the
                # task with the new priority but do not clean up the old entry).
                if candidate.priority != priority:
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
                    logger.info("claim_next: skipping %s - %s", task_id, overlap_msg)
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
            expected_version: If set, CAS - reject if task.version != this.
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
                # never silently re-return an already-claimed or
                # terminal task - that enables double-claim. Raise so the
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
        tenant_id: str | None = None,
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
            tenant_id: If set, tasks must belong to this tenant scope.
                Tasks outside the scope (including tasks that no longer
                exist or whose tenant has changed) are reported as failed.
                The check runs inside the lock so the authorization decision
                is atomic with the claim, eliminating a TOCTOU race against
                concurrent deletes or tenant rewrites.

        Returns:
            A tuple of (claimed_ids, failed_ids).
        """
        claimed: list[str] = []
        failed: list[str] = []
        normalized_tenant = normalize_tenant_id(tenant_id) if tenant_id is not None else None
        async with self._lock:
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task is None or task.status != TaskStatus.OPEN or not self._dependencies_satisfied(task):
                    failed.append(task_id)
                    continue
                # Tenant authorization happens inside the lock so it cannot
                # be invalidated between check and claim by a concurrent
                # request mutating the task.
                if normalized_tenant is not None and task.tenant_id != normalized_tenant:
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

    async def complete(
        self,
        task_id: str,
        result_summary: str,
        *,
        completion: WorkerCompletion | None = None,
    ) -> Task:
        """Mark a task as done.

        Args:
            task_id: Task identifier.
            result_summary: Non-empty summary of what was done (diff or log reference).
            completion: Optional contract-validated completion payload
                (#2244). When provided, the payload, contract version, and
                validation outcome are persisted in ``task.metadata`` and
                recorded as an HMAC-chained audit event.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            EmptyCompletionError: If *result_summary* is empty.
                The task is auto-transitioned to ``FAILED`` with
                ``reason='completion missing summary'`` before this is
                raised, so the slot is released atomically and the
                watchdog does not need to intervene.  The HTTP layer
                maps this to a 422 response.
        """
        # when result_summary is empty/None, auto-fail the task
        # under the lock so the slot is freed atomically.  A caller that
        # bailed out of ``complete()`` previously left the task CLAIMED
        # until the watchdog flipped it, allowing a fresh agent to
        # duplicate work that was already committed.
        if not result_summary or not result_summary.strip():
            async with self._lock:
                task = self._tasks.get(task_id)
                if task is None:
                    raise KeyError(task_id)
                # If the task is already in a terminal state, do not re-fail.
                if task.status in (
                    TaskStatus.DONE,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.CLOSED,
                    TaskStatus.REFUSED,
                ):
                    raise EmptyCompletionError(task_id, task)
                self._index_remove(task)
                transition_task(
                    task,
                    TaskStatus.FAILED,
                    actor="task_store",
                    reason=_EMPTY_COMPLETION_REASON,
                )
                task.result_summary = _EMPTY_COMPLETION_REASON
                task.completed_at = time.time()
                task.version += 1
                self._index_add(task)
                completed_at = task.completed_at
                await self._append_jsonl(self._task_to_record(task))
                await self._append_archive(task, completed_at)
            raise EmptyCompletionError(task_id, task)

        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(task, TaskStatus.DONE, actor="task_store", reason="complete")
            task.result_summary = result_summary
            if completion is not None:
                from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION

                task.metadata["worker_completion"] = completion.to_dict()
                task.metadata["contract_version"] = WORKER_CONTRACT_VERSION
                task.metadata["contract_validation"] = "valid"
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            await self._complete_parent_if_ready(task.parent_task_id)
        if completion is not None:
            self._audit_contract_outcome(task_id, outcome="valid")
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

    async def fail(self, task_id: str, reason: str, *, terminal_reason: str | None = None) -> Task:
        """Mark a task as failed.

        Args:
            task_id: Task identifier.
            reason: Why it failed.
            terminal_reason: Optional machine-readable failure class
                (e.g. ``"contract_violation"``) persisted on the task.

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
            if terminal_reason is not None:
                task.terminal_reason = terminal_reason
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
            return task

    async def fail_contract_violation(self, task_id: str, violation: ContractViolation) -> Task:
        """Fail a task whose terminal payload violated the completion contract.

        Distinct from a plain :meth:`fail` in that the contract version,
        validation outcome, and schema error path are persisted in
        ``task.metadata`` and recorded as an HMAC-chained audit event, so
        chain verification covers "this task's terminal payload was
        rejected against contract vX" (#2244).

        Args:
            task_id: Task identifier.
            violation: The schema violation raised by the contract parser.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            IllegalTransitionError: If the task cannot transition to FAILED.
        """
        from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION

        reason = f"contract_violation: {violation.path}: {violation.message}"
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(task, TaskStatus.FAILED, actor="task_store", reason=reason)
            task.result_summary = reason
            task.terminal_reason = "contract_violation"
            task.metadata["contract_version"] = WORKER_CONTRACT_VERSION
            task.metadata["contract_validation"] = "violation"
            task.metadata["contract_error_path"] = violation.path
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
        self._audit_contract_outcome(task_id, outcome="violation", schema_error_path=violation.path)
        return task

    async def refuse(self, task_id: str, refusal: WorkerRefusal) -> Task:
        """Mark *task_id* as :class:`TaskStatus.REFUSED` with a typed refusal.

        Distinct from :meth:`fail`: REFUSED is the terminal state for a
        worker that reported - via the completion contract (#2244) - that
        the task cannot proceed as specified. The validated refusal
        payload, contract version, and validation outcome are persisted
        on the task record and recorded as an HMAC-chained audit event.

        Args:
            task_id: Task identifier.
            refusal: The validated refusal payload.

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            IllegalTransitionError: If the current status cannot
                transition to REFUSED (e.g. already terminal).
        """
        from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION

        outcome = f"refused:{refusal.kind.value}"
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(task, TaskStatus.REFUSED, actor="task_store", reason=refusal.detail)
            task.result_summary = refusal.detail
            task.terminal_reason = outcome
            task.metadata["refusal"] = refusal.to_dict()
            task.metadata["contract_version"] = WORKER_CONTRACT_VERSION
            task.metadata["contract_validation"] = outcome
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)
        self._audit_contract_outcome(task_id, outcome=outcome)
        return task

    async def create_refusal_follow_ups(self, parent: Task, refusal: WorkerRefusal) -> list[Task]:
        """Create the deterministic follow-up task set for a scope_exceeded refusal.

        Task ids are content-addressed (see
        :func:`bernstein.core.tasks.contracts.derive_follow_up_specs`), so
        the same refusal payload always yields the same follow-up set and a
        redelivered refusal is a no-op for ids that already exist.

        Args:
            parent: The refused task the split derives from.
            refusal: Validated refusal; non-``scope_exceeded`` kinds
                yield an empty list.

        Returns:
            The newly created follow-up tasks (existing ids are skipped).
        """
        from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION, derive_follow_up_specs

        specs = derive_follow_up_specs(parent.id, refusal)
        created: list[Task] = []
        async with self._lock:
            for spec in specs:
                if spec.task_id in self._tasks:
                    continue
                task = Task(
                    id=spec.task_id,
                    title=spec.title,
                    description=spec.description,
                    role=parent.role,
                    priority=parent.priority,
                    scope=parent.scope,
                    complexity=parent.complexity,
                    parent_task_id=parent.id,
                    tenant_id=parent.tenant_id,
                    cell_id=parent.cell_id,
                    repo=parent.repo,
                    batch_eligible=False,
                    metadata={
                        "origin": "scope_exceeded_split",
                        "refused_task_id": parent.id,
                        "contract_version": WORKER_CONTRACT_VERSION,
                    },
                )
                self._tasks[task.id] = task
                self._index_add(task)
                self._parent_index_add(task)
                await self._append_jsonl(self._task_to_record(task))
                created.append(task)
        if created:
            logger.info(
                "Refusal split created %d follow-up task(s) for %s",
                len(created),
                sanitize_log(parent.id),
            )
        return created

    def _audit_contract_outcome(self, task_id: str, *, outcome: str, schema_error_path: str = "") -> None:
        """Record a contract-validation outcome in the HMAC-chained audit log.

        Best-effort: audit is additive, so a missing or failing audit log
        never blocks the task mutation that already happened.
        """
        from bernstein.core.tasks.contracts import WORKER_CONTRACT_VERSION
        from bernstein.core.tasks.lifecycle import get_audit_log

        audit = get_audit_log()
        if audit is None:
            return
        details: dict[str, Any] = {
            "contract_version": WORKER_CONTRACT_VERSION,
            "outcome": outcome,
        }
        if schema_error_path:
            details["schema_error_path"] = schema_error_path
        try:
            audit.log(
                event_type="task.contract_validation",
                actor="task_store",
                resource_type="task",
                resource_id=task_id,
                details=details,
            )
        except OSError as exc:
            logger.warning("Contract audit event write failed for %s: %s", sanitize_log(task_id), exc)

    async def reopen(self, task_id: str, reason: str) -> Task:
        """Reopen a done task that failed janitor verification.

        Transitions DONE -> OPEN so the SAME task (same id) is re-claimed and
        re-attempted - no new task is created. Increments
        ``metadata['janitor_reopen_count']`` so the orchestrator can bound the
        number of reopen cycles before permanent failure.

        Args:
            task_id: Task identifier.
            reason: Why the task is being reopened (e.g. failed janitor signals).

        Returns:
            The updated Task.

        Raises:
            KeyError: If task_id does not exist.
            IllegalTransitionError: If the task is not in DONE status.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            self._index_remove(task)
            transition_task(task, TaskStatus.OPEN, actor="task_store", reason=reason)
            reopen_count = int(task.metadata.get("janitor_reopen_count", 0) or 0) + 1
            task.metadata["janitor_reopen_count"] = reopen_count
            task.claimed_at = None
            task.claimed_by_session = None
            task.assigned_agent = None
            task.completed_at = None
            task.result_summary = None
            task.priority = 0
            task.version += 1
            self._index_add(task)
            await self._append_jsonl(self._task_to_record(task))
            logger.info(
                "task.reopen: task_id=%s reopen_count=%d reason=%s",
                sanitize_log(task_id),
                reopen_count,
                sanitize_log(reason),
            )
            return task

    async def abandon(
        self,
        task_id: str,
        reason: str,
        detail: str = "",
        *,
        adapter: str = "",
        agent_id: str = "",
        cost_to_date_usd: float = 0.0,
    ) -> Task:
        """Mark *task_id* as :class:`TaskStatus.ABANDONED` and record a ledger row.

        Distinct from :meth:`fail`. ABANDONED is a terminal status that
        agents reach voluntarily after deciding the task cannot be
        finished honestly (#1350). Downstream tasks that depend on this
        one cascade to :class:`TaskStatus.BLOCKED_BY_ABANDON` so the
        dependency scanner stops waiting for an output that will never
        arrive.

        Args:
            task_id: Task identifier.
            reason: One of the :class:`AbandonReason` enum values (string
                form). Unknown values raise :class:`ValueError`.
            detail: Free-form human-readable rationale.
            adapter: Adapter/CLI label that originated the call.
            agent_id: Adapter session identifier.
            cost_to_date_usd: Cost accumulated on the task so far.

        Returns:
            The updated :class:`Task`.

        Raises:
            KeyError: If ``task_id`` is unknown.
            ValueError: If *reason* is not a valid :class:`AbandonReason`.
            IllegalTransitionError: If the current task status cannot
                transition to ``ABANDONED`` (e.g. already terminal).
        """
        # Import locally to avoid bootstrapping the abandon module at
        # task_store_core import time (keeps module-import cost flat).
        from bernstein.core.tasks.abandon import (
            AbandonmentLedger,
            new_abandonment,
        )

        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            # Coerce reason early so we surface a ValueError before any
            # state mutation. ``new_abandonment`` calls ``coerce`` too,
            # but doing it here means a bad reason never partial-writes.
            row = new_abandonment(
                task_id=task_id,
                reason=reason,
                detail=detail,
                role=task.role,
                adapter=adapter,
                agent_id=agent_id,
                cost_to_date_usd=cost_to_date_usd,
                attempts=task.retry_count,
            )

            self._index_remove(task)
            transition_task(task, TaskStatus.ABANDONED, actor="task_store", reason=detail or reason)
            task.result_summary = detail or reason
            task.terminal_reason = row.reason.value
            task.completed_at = time.time()
            task.version += 1
            self._index_add(task)
            completed_at = task.completed_at
            await self._append_jsonl(self._task_to_record(task))
            await self._append_archive(task, completed_at)

            # Cascade: downstream tasks waiting on this one move to
            # BLOCKED_BY_ABANDON so consumers stop waiting forever.
            for downstream in list(self._tasks.values()):
                if task_id not in downstream.depends_on:
                    continue
                if downstream.status not in {
                    TaskStatus.OPEN,
                    TaskStatus.CLAIMED,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.WAITING_FOR_SUBTASKS,
                }:
                    continue
                self._index_remove(downstream)
                try:
                    transition_task(
                        downstream,
                        TaskStatus.BLOCKED_BY_ABANDON,
                        actor="task_store",
                        reason=f"upstream {task_id} abandoned: {row.reason.value}",
                    )
                except IllegalTransitionError:
                    # Restore index entry - leave downstream untouched.
                    self._index_add(downstream)
                    continue
                downstream.result_summary = f"upstream {task_id} abandoned"
                downstream.version += 1
                self._index_add(downstream)
                await self._append_jsonl(self._task_to_record(downstream))

            # Ledger write is last so the in-memory state is the source
            # of truth even when the ledger file is read-only / OOS.
            ledger = AbandonmentLedger(self._sdd_dir)
            try:
                ledger.append(row)
            except OSError as exc:
                logger.warning("Abandonment ledger write failed for %s: %s", task_id, exc)
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
        """Complete a waiting ancestor chain when all descendant subtasks are done.

        Walks up the ``parent_task_id`` chain iteratively (not recursively) and
        promotes each ancestor to ``DONE`` as long as it is ``WAITING_FOR_SUBTASKS``
        and every direct child is already ``DONE``. The iterative form avoids
        re-entering ``self._lock`` (``asyncio.Lock`` is not re-entrant) and the
        ``visited`` set guards against parent_task_id cycles from bad data.

        Fix for previously only the immediate parent was promoted, so
        a grandparent G never bubbled up and stayed ``WAITING_FOR_SUBTASKS``
        until timeout escalation forced it to ``BLOCKED``.
        """
        current_id = parent_task_id
        visited: set[str] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            parent = self._tasks.get(current_id)
            if parent is None or parent.status != TaskStatus.WAITING_FOR_SUBTASKS:
                return
            subtasks = [task for task in self._tasks.values() if task.parent_task_id == current_id]
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
            # Bubble up: attempt to complete this task's own parent.
            current_id = parent.parent_task_id

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
            # durably record the entry so progress history survives
            # a server crash.  I/O happens inside the lock (like
            # ``_append_jsonl``) to preserve ordering with in-memory state.
            await asyncio.to_thread(
                self._append_progress_record,
                task_id,
                {
                    "kind": "entry",
                }
                | entry,
            )
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
        # persist alongside progress entries so snapshot history
        # is recovered after a crash.  Keeping snapshots and entries in the
        # same file simplifies replay (one pass per task).
        self._append_progress_record(
            task_id,
            {
                "kind": "snapshot",
                "timestamp": snap.timestamp,
                "files_changed": snap.files_changed,
                "tests_passing": snap.tests_passing,
                "errors": snap.errors,
                "last_file": snap.last_file,
            },
        )
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
                    f"ESCALATION: subtask wait exceeded {threshold:.0f}s - "
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
        """Update mutable task fields (role, priority, model) - manager corrections.

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
            role_changed = role is not None and role != task.role
            priority_changed = priority is not None and priority != task.priority
            if role_changed or priority_changed:
                # Role and priority are both inputs to the priority heap / role
                # index; any change requires re-indexing so the heap entry
                # reflects the new key.  A stale (old_priority, id) entry may
                # remain in the old heap - claim_next lazy-deletes it on pop
                # by comparing against the live task.priority.
                self._index_remove(task)
                if role_changed:
                    task.role = cast("str", role)
                if priority_changed:
                    task.priority = cast("int", priority)
                self._index_add(task)
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
            if task.priority != 0:
                # Re-index so the priority heap learns about the new priority.
                # The old (priority, id) heap entry is lazy-deleted on pop.
                self._index_remove(task)
                task.priority = 0
                self._index_add(task)
            task.version += 1
            await self._append_jsonl(self._task_to_record(task))
            return task

    async def force_claim(self, task_id: str) -> Task:
        """Force a task back to open with priority 0 so it is claimed immediately.

        If the task is already open its priority is set to 0 and it stays open.
        If it is in a terminal state (done, failed, cancelled) it is returned
        unchanged - only open/claimed/in_progress tasks can be force-claimed.

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
            terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REFUSED}
            if task.status in terminal:
                raise ValueError(
                    f"Task '{task_id}' is in terminal state '{task.status.value}' and cannot be force-claimed"
                )
            # Set priority *before* re-indexing so the heap entry carries the
            # final priority - otherwise the pushed (old_priority, id) tuple
            # diverges from task.priority and claim_next will skip it as
            # lazy-deleted (or, worse, pop it at the wrong priority).
            if task.status != TaskStatus.OPEN:
                # Reset claimed/in_progress back to open
                self._index_remove(task)
                transition_task(task, TaskStatus.OPEN, actor="task_store", reason="force_claim")
                task.priority = 0
                self._index_add(task)
            elif task.priority != 0:
                # Already OPEN - re-index to refresh heap with priority=0.
                self._index_remove(task)
                task.priority = 0
                self._index_add(task)
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
        limit: int | None = None,
        offset: int | None = None,
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
            limit: If provided, return at most this many tasks after filtering.
                Pushed into the store so routes (#1727 pagination, export, costs)
                no longer slice in Python.
            offset: If provided, skip this many tasks after filtering. Combine
                with ``limit`` for paginated iteration.

        Returns:
            List of matching tasks.
        """
        # Choose the smallest seed collection: when status is supplied, the
        # by-status index is already partitioned, otherwise walk all tasks.
        if status is not None:
            try:
                ts = TaskStatus(status)
                seed: list[Task] = list(self._by_status[ts].values())
            except ValueError:
                return []
        else:
            seed = list(self._tasks.values())

        # Resolve filter constants once; previously each pass recomputed them
        # (or normalized strings) on every iteration.
        normalized_tenant = normalize_tenant_id(tenant_id) if tenant_id is not None else None
        check_open_deps = status == "open"

        # Single-pass filter: evaluate every predicate together so we walk
        # the task list once instead of rebuilding it N times.
        filtered = [
            t
            for t in seed
            if (cell_id is None or t.cell_id == cell_id)
            and (normalized_tenant is None or t.tenant_id == normalized_tenant)
            and (claimed_by_session is None or t.claimed_by_session == claimed_by_session)
            and (parent_session_id is None or t.parent_session_id == parent_session_id)
            and (not check_open_deps or self._dependencies_satisfied(t))
        ]

        if offset is limit is None:
            return filtered

        start = max(0, offset) if offset is not None else 0
        if limit is None:
            return filtered[start:]
        end = start + max(0, limit)
        return filtered[start:end]

    def count_by_status(self, tenant_id: str | None = None) -> dict[str, int]:
        """Return task counts per status without materialising task lists.

        This is O(N) in the worst case when tenant filtering is applied, but
        avoids serialising full task bodies - ideal for the /tasks/counts
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

        if task.priority != new_priority:
            # Refresh heap entry - the old (priority, id) tuple is lazy-deleted
            # on pop via the priority mismatch check in claim_next.
            self._index_remove(task)
            task.priority = new_priority
            self._index_add(task)
        task.version += 1

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
                    logger.warning(
                        "Ignoring illegal heartbeat transition %s -> %s for %s",
                        sanitize_log(str(agent.status)),
                        sanitize_log(str(status)),
                        sanitize_log(agent_id),
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
            entry: dict[str, Any] = {
                "role": role,
            } | counts
            if role in cost_by_role:
                entry["cost_usd"] = round(cost_by_role[role], 4)
            per_role.append(entry)

        summary: dict[str, Any] = {
            "total": len(self._tasks),
            "open": len(self._by_status.get(TaskStatus.OPEN, {})),
            "claimed": len(self._by_status.get(TaskStatus.CLAIMED, {})),
            "done": len(self._by_status.get(TaskStatus.DONE, {})),
            "failed": len(self._by_status.get(TaskStatus.FAILED, {})),
            "refused": len(self._by_status.get(TaskStatus.REFUSED, {})),
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
            logger.warning("Corrupted bandit state at %s - skipping", bandit_state_path)
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
        terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REFUSED}
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
            return self._cost_cache.copy()
        stat = self._metrics_jsonl_path.stat()
        mtime = stat.st_mtime
        if mtime == self._cost_cache_mtime:
            return self._cost_cache.copy()
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
                    "Corrupted metrics record in %s - skipping: %s",
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
        return self._cost_cache.copy()

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
