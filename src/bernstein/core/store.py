"""Abstract TaskStore base class for pluggable storage backends.

Concrete implementations:
- TaskStore (server.py) — in-memory with JSONL persistence, zero dependencies.
- PostgresTaskStore (store_postgres.py) — asyncpg + optional Redis locking.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bernstein.core.models import Task
    from bernstein.core.server import ArchiveRecord, TaskCreate


@dataclass
class RoleSummary:
    """Per-role task count breakdown for the status dashboard."""

    role: str
    open: int
    claimed: int
    done: int
    failed: int
    cost_usd: float = 0.0


@dataclass
class StatusSummary:
    """Aggregated task-store status, backend-agnostic.

    Returned by :meth:`BaseTaskStore.status_summary` and converted by the
    HTTP route to the Pydantic ``StatusResponse`` model.
    """

    total: int
    open: int
    claimed: int
    done: int
    failed: int
    per_role: list[RoleSummary] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    total_cost_usd: float = 0.0


class BaseTaskStore(ABC):
    """Abstract interface for all task persistence backends.

    All mutating operations are ``async``.  Read-only queries are also
    declared ``async`` so that network-backed stores (e.g. PostgreSQL) can
    issue I/O without blocking the event loop.  In-memory stores simply
    define them as ``async def`` bodies without ``await``.
    """

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    async def startup(self) -> None:
        """Initialise backend connections and load initial state."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Flush pending writes and release backend connections."""

    # -- task mutations ------------------------------------------------------

    @abstractmethod
    async def create(self, req: TaskCreate) -> Task:
        """Create and persist a new task.

        Args:
            req: Validated creation payload from the HTTP layer.

        Returns:
            The newly created :class:`~bernstein.core.models.Task`.

        Raises:
            fastapi.HTTPException: 422 if dependencies are missing or cyclic.
        """

    @abstractmethod
    async def claim_next(self, role: str) -> Task | None:
        """Claim the highest-priority open task for *role*, or ``None``.

        Args:
            role: Agent role string (e.g. ``"backend"``).

        Returns:
            The claimed :class:`~bernstein.core.models.Task`, or ``None`` if
            no open tasks exist for that role.
        """

    @abstractmethod
    async def claim_by_id(
        self,
        task_id: str,
        expected_version: int | None = None,
        agent_role: str | None = None,
    ) -> Task:
        """Claim a specific task, optionally with CAS version check and role matching.

        Args:
            task_id: Task identifier.
            expected_version: If set, the claim is rejected unless the stored
                task version matches (compare-and-swap).
            agent_role: If set, the claim is rejected unless the task's role
                matches the agent's role (role-locked claiming).

        Returns:
            The claimed :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
            ValueError: Version conflict (CAS mismatch) or role mismatch.
        """

    @abstractmethod
    async def claim_batch(
        self,
        task_ids: list[str],
        agent_id: str,
        agent_role: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Atomically claim multiple tasks by ID with optional role matching.

        Args:
            task_ids: Task identifiers to claim.
            agent_id: Claiming agent's identifier.
            agent_role: If set, only tasks whose role matches the agent's role
                can be claimed.

        Returns:
            ``(claimed_ids, failed_ids)`` — tasks not in ``OPEN`` status or
            with mismatched roles are reported in *failed_ids*.
        """

    @abstractmethod
    async def complete(self, task_id: str, result_summary: str) -> Task:
        """Mark a task done and archive it.

        Args:
            task_id: Task identifier.
            result_summary: Human-readable completion summary.

        Returns:
            The updated :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
        """

    @abstractmethod
    async def fail(self, task_id: str, reason: str) -> Task:
        """Mark a task failed and archive it.

        Args:
            task_id: Task identifier.
            reason: Failure reason.

        Returns:
            The updated :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
        """

    @abstractmethod
    async def add_progress(
        self,
        task_id: str,
        message: str,
        percent: int,
    ) -> Task:
        """Append an intermediate progress update.

        Args:
            task_id: Task identifier.
            message: Human-readable progress message.
            percent: Completion percentage (0-100).

        Returns:
            The updated :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
        """

    @abstractmethod
    async def update(self, task_id: str, role: str | None, priority: int | None) -> Task:
        """Update mutable task fields (role, priority) — manager corrections.

        Only open or failed tasks should be reassigned; claimed tasks are left
        to finish before the new assignment takes effect.

        Args:
            task_id: Task identifier.
            role: New role if provided.
            priority: New priority if provided.

        Returns:
            The updated :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
        """

    @abstractmethod
    async def cancel(self, task_id: str, reason: str) -> Task:
        """Cancel a non-terminal task.

        Args:
            task_id: Task identifier.
            reason: Cancellation reason.

        Returns:
            The updated :class:`~bernstein.core.models.Task`.

        Raises:
            KeyError: Task not found.
            ValueError: Task is already in a terminal state.
        """

    # -- queries -------------------------------------------------------------

    @abstractmethod
    async def list_tasks(
        self,
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[Task]:
        """Return tasks, optionally filtered.

        When *status* is ``"open"``, tasks whose dependencies are not all
        ``DONE`` are excluded (they are not yet claimable).

        Args:
            status: If provided, only tasks with this status are returned.
            cell_id: If provided, only tasks in this cell are returned.
        """

    @abstractmethod
    async def get_task(self, task_id: str) -> Task | None:
        """Return a single task by ID, or ``None`` if not found."""

    @abstractmethod
    async def status_summary(self) -> StatusSummary:
        """Return an aggregated task count summary for the dashboard."""

    @abstractmethod
    async def read_archive(self, limit: int = 50) -> list[ArchiveRecord]:
        """Return the last *limit* completed/failed task records, oldest-first."""

    # -- agent heartbeats ----------------------------------------------------

    @abstractmethod
    async def heartbeat(
        self,
        agent_id: str,
        role: str,
        status: Literal["starting", "working", "idle", "dead"],
    ) -> float:
        """Record an agent heartbeat.

        Args:
            agent_id: Unique agent identifier.
            role: Agent's role.
            status: Agent's self-reported status.

        Returns:
            Server-side Unix timestamp of the heartbeat.
        """

    @abstractmethod
    async def mark_stale_dead(self, threshold_s: float = 60.0) -> int:
        """Mark agents whose last heartbeat is older than *threshold_s* as dead.

        Returns:
            Number of agents marked dead.
        """

    # -- read-only properties ------------------------------------------------

    @property
    @abstractmethod
    def agent_count(self) -> int:
        """Number of known agents (may be served from a local cache)."""

    @property
    def start_ts(self) -> float:
        """Unix timestamp of when this store was initialised.

        Default implementation returns a fixed timestamp set at object
        construction; backends that restart may override this.
        """
        return self._start_ts

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

    def __init__(self) -> None:
        self._start_ts: float = time.time()
