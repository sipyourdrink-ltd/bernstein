"""Serialized quality gate executions with per-caller result delivery.

Prevents concurrent gate runs from clobbering shared state (git index, cache
directories, metrics files) by serializing execution through a single FIFO
queue.  Every caller's task is validated against its own diff — no request is
silently dropped or masked with a spurious ``passed=True``.

Flow:

- If no run is in progress → start immediately, set ``in_progress = True``.
- If a run is already active → enqueue the request with a private
  :class:`threading.Event` + result slot and **block** on the event.
- When the active run finishes → pop the next queued request, execute its
  gates, store the result in its slot, and fire its event.  The blocked caller
  wakes up and returns its own real result.

Previously this class returned a lightweight ``passed=True`` for coalesced
callers — a silent gate bypass under concurrent completions (audit-037).  The
"trailing-run-only" optimisation is unsafe because each task has its own
worktree and its own diff; reusing one run's result for another task means
gates are never actually executed against the second task's changes.

Usage::

    coalescer = QualityGateCoalescer()
    result = coalescer.run(task, run_dir, workdir, config)
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.quality.quality_gates import QualityGatesConfig, QualityGatesResult, run_quality_gates

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# Default timeout (seconds) a queued caller will wait for its turn before
# giving up and failing the task.  Gate runs that legitimately exceed this
# budget signal a deeper problem (hung subprocess, runaway tests); blocking
# the orchestrator indefinitely is worse than surfacing the stuck gate.
_DEFAULT_QUEUE_TIMEOUT_S: float = 900.0  # 15 minutes


# ---------------------------------------------------------------------------
# Internal queued-run descriptor
# ---------------------------------------------------------------------------


@dataclass
class _QueuedRun:
    """A queued gate run waiting for the in-progress run to finish.

    Each queued caller owns a private :class:`threading.Event` and a result
    slot.  When the coalescer pops this entry, it runs gates for ``task``,
    writes the outcome to ``result`` (or the raised exception to ``error``),
    and sets ``event`` to wake the caller.
    """

    task: Task
    run_dir: Path
    workdir: Path
    kwargs: dict[str, Any] = field(default_factory=dict[str, Any])
    event: threading.Event = field(default_factory=threading.Event)
    result: QualityGatesResult | None = None
    error: BaseException | None = None


# Back-compat alias: older call sites / tests referenced ``_PendingRun``.
# Kept as an alias so imports continue to work while the semantics are
# expressed through :class:`_QueuedRun`.
_PendingRun = _QueuedRun


# ---------------------------------------------------------------------------
# Coalescer
# ---------------------------------------------------------------------------


class QualityGateCoalescer:
    """Serializes quality gate runs, one task at a time, FIFO order.

    Concurrent calls to :meth:`run` are queued; each request blocks until its
    turn, then executes gates against its own task and returns its own real
    :class:`~bernstein.core.quality_gates.QualityGatesResult`.

    Attributes:
        in_progress: ``True`` when a gate run is currently executing.
        pending_count: Number of queued callers waiting for their turn.
    """

    def __init__(self, queue_timeout_s: float = _DEFAULT_QUEUE_TIMEOUT_S) -> None:
        self._lock = threading.Lock()
        self._in_progress: bool = False
        self._queue: deque[_QueuedRun] = deque()
        self._queue_timeout_s = queue_timeout_s

    # ------------------------------------------------------------------
    # Properties (thread-safe reads)
    # ------------------------------------------------------------------

    @property
    def in_progress(self) -> bool:
        """True if a gate run is currently executing."""
        with self._lock:
            return self._in_progress

    @property
    def pending_count(self) -> int:
        """Number of callers currently queued behind the active run."""
        with self._lock:
            return len(self._queue)

    # Back-compat for tests/callers that peeked at ``_pending``: expose the
    # tail of the queue (the most recently enqueued request), or ``None``.
    @property
    def _pending(self) -> _QueuedRun | None:
        with self._lock:
            return self._queue[-1] if self._queue else None

    @_pending.setter
    def _pending(self, value: _QueuedRun | None) -> None:
        # Test-only hook: assign ``None`` to clear the queue, or a single
        # :class:`_QueuedRun` to seed the queue with one entry.
        with self._lock:
            self._queue.clear()
            if value is not None:
                self._queue.append(value)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        task: Task,
        run_dir: Path,
        workdir: Path,
        config: QualityGatesConfig,
        **kwargs: Any,
    ) -> QualityGatesResult:
        """Run quality gates, serialized across concurrent callers.

        If no run is in progress, execute immediately.  Otherwise, enqueue
        the request and block on a per-caller event until its turn comes
        up, then return the real :class:`QualityGatesResult` for *this*
        caller's task.

        Args:
            task: The completed task to validate.
            run_dir: Directory to run gate commands in (agent worktree).
            workdir: Project root used for metrics and caching.
            config: Quality gates configuration.
            **kwargs: Additional keyword arguments forwarded to
                :func:`~bernstein.core.quality_gates.run_quality_gates`.

        Returns:
            The actual :class:`~bernstein.core.quality_gates.QualityGatesResult`
            produced by running gates against ``task``.

        Raises:
            TimeoutError: If the caller has been queued longer than
                ``queue_timeout_s`` without its turn coming up.
        """
        with self._lock:
            if self._in_progress:
                queued = _QueuedRun(task=task, run_dir=run_dir, workdir=workdir, kwargs=kwargs)
                self._queue.append(queued)
                logger.debug(
                    "quality_gate_coalescer: run in progress — queued task=%s at position %d",
                    task.id,
                    len(self._queue),
                )
            else:
                # No run in progress — claim the slot and run immediately.
                self._in_progress = True
                queued = None

        if queued is not None:
            return self._wait_for_queued(queued)

        # We are the active runner — execute our own task, then drain the queue.
        try:
            result = self._execute(task, run_dir, workdir, config, **kwargs)
        finally:
            self._drain_queue(config)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wait_for_queued(self, queued: _QueuedRun) -> QualityGatesResult:
        """Block until ``queued`` is serviced and return its result.

        Raises:
            TimeoutError: If the configured queue timeout elapses.
            BaseException: Re-raises any exception captured by the runner.
        """
        if not queued.event.wait(timeout=self._queue_timeout_s):
            # Best-effort cleanup: remove the entry so it isn't executed later.
            with self._lock, contextlib.suppress(ValueError):
                # ValueError means the runner already popped us — benign race.
                self._queue.remove(queued)
            raise TimeoutError(
                f"quality_gate_coalescer: task {queued.task.id} timed out "
                f"after {self._queue_timeout_s:.0f}s waiting for gate slot",
            )

        if queued.error is not None:
            # Preserve the original traceback when re-raising.
            raise queued.error
        # Invariant: event set ⇒ either result or error was populated.
        assert queued.result is not None
        return queued.result

    def _drain_queue(self, config: QualityGatesConfig) -> None:
        """Service queued requests FIFO.  Each runs with its own parameters."""
        while True:
            with self._lock:
                if not self._queue:
                    self._in_progress = False
                    return
                queued = self._queue.popleft()

            logger.info(
                "quality_gate_coalescer: servicing queued task=%s (queue_depth_after=%d)",
                queued.task.id,
                len(self._queue),
            )
            try:
                queued.result = self._execute(
                    queued.task,
                    queued.run_dir,
                    queued.workdir,
                    config,
                    **queued.kwargs,
                )
            except BaseException as exc:
                # Capture ANY exception (including KeyboardInterrupt/SystemExit-adjacent
                # runtime errors from subprocess gates) so the blocked caller receives
                # it rather than hanging forever on its event.
                queued.error = exc
            finally:
                queued.event.set()

    def _execute(
        self,
        task: Task,
        run_dir: Path,
        workdir: Path,
        config: QualityGatesConfig,
        **kwargs: Any,
    ) -> QualityGatesResult:
        """Call ``run_quality_gates`` and return the result."""
        logger.debug("quality_gate_coalescer: executing gate run for task=%s", task.id)
        return run_quality_gates(task, run_dir, workdir, config, **kwargs)
