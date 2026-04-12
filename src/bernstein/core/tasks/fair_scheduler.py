"""Weighted fair scheduler for multi-tenant task queuing.

Implements deficit round-robin (DRR) scheduling so that tenants with
higher weights receive proportionally more scheduling turns, while
``max_concurrent`` caps prevent any single tenant from monopolising
the worker pool.

Usage::

    from bernstein.core.tasks.fair_scheduler import (
        FairScheduler,
        TenantQuota,
    )

    scheduler = FairScheduler(quotas=[
        TenantQuota(tenant_id="team-a", weight=2.0, max_concurrent=4),
        TenantQuota(tenant_id="team-b", weight=1.0, max_concurrent=2),
    ])
    scheduler.enqueue("T-001", "team-a", priority=3)
    decision = scheduler.dequeue()
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantQuota:
    """Immutable quota configuration for a single tenant.

    Attributes:
        tenant_id: Unique tenant identifier.
        weight: Scheduling weight. Higher values receive proportionally more
            turns in the deficit round-robin cycle. Must be positive.
        max_concurrent: Maximum number of tasks this tenant may have active
            simultaneously. Zero means unlimited.
        current_active: Snapshot of currently active tasks. Only used for
            initial state; the scheduler tracks its own count.
    """

    tenant_id: str
    weight: float = 1.0
    max_concurrent: int = 0
    current_active: int = 0


@dataclass(frozen=True)
class SchedulingDecision:
    """The result of a :meth:`FairScheduler.dequeue` call.

    Attributes:
        task_id: The task that should be executed next.
        tenant_id: The tenant that owns this task.
        priority: The task's priority value (lower is higher priority).
        wait_time_s: How long the task waited in the queue (seconds).
        reason: Human-readable explanation of why this task was selected.
    """

    task_id: str
    tenant_id: str
    priority: int
    wait_time_s: float
    reason: str


@dataclass
class _QueuedTask:
    """Internal bookkeeping for a task sitting in a tenant's queue."""

    task_id: str
    tenant_id: str
    priority: int
    enqueued_at: float


@dataclass
class _TenantState:
    """Per-tenant mutable scheduling state."""

    quota: TenantQuota
    deficit: float = 0.0
    queue: list[_QueuedTask] = field(default_factory=lambda: list[_QueuedTask]())
    active_task_ids: set[str] = field(default_factory=lambda: set[str]())


@dataclass
class TenantStats:
    """Per-tenant queue and utilisation statistics.

    Attributes:
        tenant_id: The tenant identifier.
        queue_depth: Number of tasks waiting in the queue.
        active_count: Number of currently active tasks.
        max_concurrent: Configured concurrency limit.
        weight: Configured scheduling weight.
    """

    tenant_id: str
    queue_depth: int
    active_count: int
    max_concurrent: int
    weight: float


class FairScheduler:
    """Weighted fair scheduler using deficit round-robin.

    Each tenant accumulates *deficit* proportional to its weight on every
    scheduling round. When a tenant's deficit is high enough it may emit
    a task; after emitting, the deficit is reduced by one quantum.  This
    ensures long-run proportional fairness while still respecting
    per-tenant ``max_concurrent`` limits and per-task priority ordering.

    Args:
        quotas: Initial tenant quota configurations.
    """

    _QUANTUM: float = 1.0  # cost deducted per emitted task

    def __init__(self, quotas: Sequence[TenantQuota] | None = None) -> None:
        self._tenants: dict[str, _TenantState] = {}
        # Round-robin order of tenant IDs.
        self._rr_order: deque[str] = deque()
        # Global lookup: task_id -> tenant_id (for active tracking).
        self._task_tenant: dict[str, str] = {}

        for quota in quotas or []:
            self._add_tenant(quota)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_tenant(
        self,
        tenant_id: str,
        weight: float = 1.0,
        max_concurrent: int = 0,
    ) -> None:
        """Add or update a tenant's quota.

        If the tenant already exists its weight and max_concurrent are
        updated in place; queued tasks and active counts are preserved.

        Args:
            tenant_id: Unique tenant identifier.
            weight: Scheduling weight (must be positive).
            max_concurrent: Maximum concurrent active tasks (0 = unlimited).

        Raises:
            ValueError: If *weight* is not positive.
        """
        if weight <= 0:
            msg = f"weight must be positive, got {weight}"
            raise ValueError(msg)

        if tenant_id in self._tenants:
            state = self._tenants[tenant_id]
            state.quota = TenantQuota(
                tenant_id=tenant_id,
                weight=weight,
                max_concurrent=max_concurrent,
            )
            logger.debug("Updated tenant %s: weight=%.2f max=%d", tenant_id, weight, max_concurrent)
        else:
            quota = TenantQuota(
                tenant_id=tenant_id,
                weight=weight,
                max_concurrent=max_concurrent,
            )
            self._add_tenant(quota)
            logger.debug("Registered tenant %s: weight=%.2f max=%d", tenant_id, weight, max_concurrent)

    def enqueue(
        self,
        task_id: str,
        tenant_id: str,
        priority: int = 5,
    ) -> None:
        """Add a task to the fair queue.

        The task is placed in the owning tenant's priority-sorted sub-queue.
        If the tenant has not been registered it is auto-registered with
        default quota values.

        Args:
            task_id: Unique task identifier.
            tenant_id: The tenant that owns this task.
            priority: Numeric priority (lower value = higher priority).

        Raises:
            ValueError: If *task_id* is already queued or active.
        """
        if task_id in self._task_tenant:
            msg = f"task {task_id} is already tracked by the scheduler"
            raise ValueError(msg)

        if tenant_id not in self._tenants:
            self.register_tenant(tenant_id)

        state = self._tenants[tenant_id]
        entry = _QueuedTask(
            task_id=task_id,
            tenant_id=tenant_id,
            priority=priority,
            enqueued_at=time.monotonic(),
        )
        state.queue.append(entry)
        # Keep the sub-queue sorted by priority (lower = higher urgency).
        state.queue.sort(key=lambda t: t.priority)
        self._task_tenant[task_id] = tenant_id
        logger.debug("Enqueued task %s for tenant %s (pri=%d)", task_id, tenant_id, priority)

    def dequeue(self) -> SchedulingDecision | None:
        """Return the next task according to weighted fair queuing.

        Uses deficit round-robin: each tenant accumulates deficit equal to
        its weight per credit round.  A tenant may emit tasks as long as
        its deficit >= quantum.  Credits are only added when no tenant can
        be served from existing deficit, ensuring that higher-weight
        tenants genuinely receive proportionally more turns.

        Returns:
            A :class:`SchedulingDecision` or ``None`` if no task is eligible.
        """
        if not self._rr_order:
            return None

        n = len(self._rr_order)

        # --- try to serve from existing deficit first ---------------------
        result = self._try_serve(n)
        if result is not None:
            return result

        # --- credit phase: add weight to every tenant's deficit -----------
        for tid in self._rr_order:
            self._tenants[tid].deficit += self._tenants[tid].quota.weight

        # --- try again after crediting -----------------------------------
        return self._try_serve(n)

    def _try_serve(self, n: int) -> SchedulingDecision | None:
        """Scan the round-robin order for an eligible tenant to serve.

        Args:
            n: Number of tenants to scan.

        Returns:
            A scheduling decision, or ``None`` if no tenant is eligible.
        """
        for _ in range(n):
            tid = self._rr_order[0]
            self._rr_order.rotate(-1)

            state = self._tenants[tid]

            if state.deficit < self._QUANTUM:
                continue

            if not state.queue:
                continue

            if self._at_concurrency_limit(state):
                continue

            # Pick highest-priority task from this tenant.
            task = state.queue.pop(0)
            state.deficit -= self._QUANTUM

            now = time.monotonic()
            wait = now - task.enqueued_at

            reason = (
                f"DRR selected tenant {tid} "
                f"(deficit={state.deficit + self._QUANTUM:.2f}->{state.deficit:.2f}, "
                f"weight={state.quota.weight:.2f})"
            )

            logger.debug(
                "Dequeued task %s from tenant %s (waited %.2fs)",
                task.task_id,
                tid,
                wait,
            )

            return SchedulingDecision(
                task_id=task.task_id,
                tenant_id=tid,
                priority=task.priority,
                wait_time_s=wait,
                reason=reason,
            )

        return None

    def mark_active(self, task_id: str) -> None:
        """Record that a task has started executing.

        Args:
            task_id: The task that is now active.

        Raises:
            KeyError: If *task_id* is not known to the scheduler.
        """
        tenant_id = self._task_tenant.get(task_id)
        if tenant_id is None:
            msg = f"unknown task {task_id}"
            raise KeyError(msg)
        state = self._tenants[tenant_id]
        state.active_task_ids.add(task_id)
        logger.debug(
            "Task %s active for tenant %s (%d/%s)",
            task_id,
            tenant_id,
            len(state.active_task_ids),
            state.quota.max_concurrent or "inf",
        )

    def mark_done(self, task_id: str) -> None:
        """Release a slot after a task finishes (success or failure).

        Args:
            task_id: The task that finished.

        Raises:
            KeyError: If *task_id* is not known to the scheduler.
        """
        tenant_id = self._task_tenant.get(task_id)
        if tenant_id is None:
            msg = f"unknown task {task_id}"
            raise KeyError(msg)
        state = self._tenants[tenant_id]
        state.active_task_ids.discard(task_id)
        del self._task_tenant[task_id]
        logger.debug(
            "Task %s done for tenant %s (%d/%s active)",
            task_id,
            tenant_id,
            len(state.active_task_ids),
            state.quota.max_concurrent or "inf",
        )

    def stats(self) -> list[TenantStats]:
        """Return per-tenant queue depth and utilisation statistics.

        Returns:
            List of :class:`TenantStats`, one per registered tenant,
            sorted by tenant_id.
        """
        result: list[TenantStats] = []
        for tid in sorted(self._tenants):
            state = self._tenants[tid]
            result.append(
                TenantStats(
                    tenant_id=tid,
                    queue_depth=len(state.queue),
                    active_count=len(state.active_task_ids),
                    max_concurrent=state.quota.max_concurrent,
                    weight=state.quota.weight,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_tenant(self, quota: TenantQuota) -> None:
        """Register a new tenant from a quota spec."""
        if quota.weight <= 0:
            msg = f"weight must be positive, got {quota.weight}"
            raise ValueError(msg)
        self._tenants[quota.tenant_id] = _TenantState(quota=quota)
        if quota.tenant_id not in self._rr_order:
            self._rr_order.append(quota.tenant_id)

    @staticmethod
    def _at_concurrency_limit(state: _TenantState) -> bool:
        """Check whether a tenant has hit its max_concurrent cap."""
        cap = state.quota.max_concurrent
        if cap <= 0:
            return False
        return len(state.active_task_ids) >= cap
