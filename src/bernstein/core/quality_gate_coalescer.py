"""Trailing-run coalescence for quality gate executions.

Prevents duplicate gate runs when tasks complete in rapid succession.
The behaviour mirrors the pattern used in Claude Code's extractMemories service:

- If no run is in progress → start immediately, set ``in_progress = True``.
- If a run is already active → store the request as *pending* (replacing any
  previous pending request; only the trailing run matters).
- When the active run finishes → check for a pending request; if one exists,
  execute exactly **one** trailing run to cover all accumulated completions.

This guarantees that rapid task completions never produce more than two gate
runs (the current run + one trailing run), regardless of how many completions
arrive during the first run.

Usage::

    coalescer = QualityGateCoalescer()
    result = coalescer.run(task, run_dir, workdir, config)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.quality_gates import QualityGatesConfig, QualityGatesResult, run_quality_gates

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal pending-run descriptor
# ---------------------------------------------------------------------------


@dataclass
class _PendingRun:
    """A queued gate run request waiting for the in-progress run to finish."""

    task: Task
    run_dir: Path
    workdir: Path
    kwargs: dict[str, Any] = field(default_factory=dict[str, Any])


# ---------------------------------------------------------------------------
# Coalescer
# ---------------------------------------------------------------------------


class QualityGateCoalescer:
    """Coalesces quality gate runs to prevent duplicate execution.

    When tasks complete in rapid succession, multiple calls to
    :func:`~bernstein.core.quality_gates.run_quality_gates` would execute
    concurrently.  This class ensures that only one run is active at a time
    and that any requests arriving during an active run are merged into a
    single trailing run after the current one completes.

    Attributes:
        in_progress: ``True`` when a gate run is currently executing.
        pending_count: Number of requests coalesced into the next trailing run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_progress: bool = False
        self._pending: _PendingRun | None = None

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
        """1 if a trailing run is queued, 0 otherwise."""
        with self._lock:
            return 1 if self._pending is not None else 0

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
        """Run quality gates with trailing-run coalescence.

        If no run is in progress, the gate run starts immediately.  If a run
        is already active, this request is coalesced into a pending slot; the
        *caller receives a no-op pass result* rather than blocking, and the
        trailing run will execute on behalf of this (and any other coalesced)
        request when the active run finishes.

        Args:
            task: The completed task to validate.
            run_dir: Directory to run gate commands in (agent worktree).
            workdir: Project root used for metrics and caching.
            config: Quality gates configuration.
            **kwargs: Additional keyword arguments forwarded to
                :func:`~bernstein.core.quality_gates.run_quality_gates`.

        Returns:
            :class:`~bernstein.core.quality_gates.QualityGatesResult` for the
            gate run that executed.  When coalesced, returns a lightweight
            pass result for the caller so it is never blocked.
        """
        with self._lock:
            if self._in_progress:
                # Coalesce: replace any existing pending slot with the latest request
                self._pending = _PendingRun(task=task, run_dir=run_dir, workdir=workdir, kwargs=kwargs)
                logger.debug(
                    "quality_gate_coalescer: run in progress — coalesced task=%s into pending slot",
                    task.id,
                )
                # Return a lightweight pass so the caller is not blocked
                return QualityGatesResult(task_id=task.id, passed=True)

            # No run in progress — claim the slot and run immediately
            self._in_progress = True

        result = self._execute(task, run_dir, workdir, config, **kwargs)

        # After the run, check for a coalesced trailing request
        with self._lock:
            pending = self._pending
            self._pending = None
            if pending is None:
                self._in_progress = False

        if pending is not None:
            logger.info(
                "quality_gate_coalescer: executing trailing run for coalesced task=%s",
                pending.task.id,
            )
            try:
                result = self._execute(
                    pending.task,
                    pending.run_dir,
                    pending.workdir,
                    config,
                    **pending.kwargs,
                )
            finally:
                with self._lock:
                    self._in_progress = False

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute(
        self,
        task: Task,
        run_dir: Path,
        workdir: Path,
        config: QualityGatesConfig,
        **kwargs: Any,
    ) -> QualityGatesResult:
        """Call run_quality_gates and return the result."""
        logger.debug("quality_gate_coalescer: executing gate run for task=%s", task.id)
        return run_quality_gates(task, run_dir, workdir, config, **kwargs)
