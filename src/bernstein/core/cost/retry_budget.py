"""Task retry budget tracking with per-task and per-run configurable limits.

Tracks how many retries each task has consumed and enforces both per-task
and global (per-run) retry budgets.

Usage::

    from bernstein.core.cost.retry_budget import RetryBudget, RetryBudgetConfig

    config = RetryBudgetConfig(max_retries_per_task=3, max_retries_per_run=20)
    budget = RetryBudget(config)
    if budget.can_retry("task-1"):
        budget.record_retry("task-1")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryBudgetConfig:
    """Configuration for retry budget limits.

    Attributes:
        max_retries_per_task: Maximum retries allowed per individual task.
            0 means no retries allowed.
        max_retries_per_run: Maximum total retries across all tasks in a run.
            0 means unlimited.
        backoff_base_seconds: Base delay for exponential backoff between retries.
        backoff_max_seconds: Maximum delay between retries.
    """

    max_retries_per_task: int = 3
    max_retries_per_run: int = 50
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 300.0


@dataclass(frozen=True)
class RetryRecord:
    """A single retry event.

    Attributes:
        task_id: The task that was retried.
        attempt: Which retry attempt this is (1-based).
        timestamp: Epoch seconds when the retry was recorded.
        reason: Why the retry was needed.
    """

    task_id: str
    attempt: int
    timestamp: float
    reason: str = ""


@dataclass
class TaskRetryState:
    """Retry state for a single task.

    Attributes:
        task_id: The task identifier.
        retries: Number of retries consumed.
        max_retries: Per-task retry limit.
        records: History of retry events.
    """

    task_id: str
    retries: int = 0
    max_retries: int = 3
    records: list[RetryRecord] = field(default_factory=list[RetryRecord])

    @property
    def exhausted(self) -> bool:
        """True if this task has used all its retry budget."""
        return self.retries >= self.max_retries

    @property
    def remaining(self) -> int:
        """Number of retries remaining."""
        return max(0, self.max_retries - self.retries)


class RetryBudget:
    """Tracks retry budget consumption for tasks within a run.

    Enforces both per-task and per-run retry limits with optional
    exponential backoff delay calculation.
    """

    def __init__(self, config: RetryBudgetConfig | None = None) -> None:
        self._config = config or RetryBudgetConfig()
        self._task_states: dict[str, TaskRetryState] = {}
        self._total_retries: int = 0

    @property
    def config(self) -> RetryBudgetConfig:
        """Current configuration."""
        return self._config

    @property
    def total_retries(self) -> int:
        """Total retries recorded across all tasks."""
        return self._total_retries

    @property
    def run_budget_remaining(self) -> int:
        """Retries remaining in the per-run budget. -1 if unlimited."""
        if self._config.max_retries_per_run == 0:
            return -1
        return max(0, self._config.max_retries_per_run - self._total_retries)

    @property
    def run_budget_exhausted(self) -> bool:
        """True if the per-run retry budget is exhausted."""
        if self._config.max_retries_per_run == 0:
            return False
        return self._total_retries >= self._config.max_retries_per_run

    def _get_state(self, task_id: str) -> TaskRetryState:
        if task_id not in self._task_states:
            self._task_states[task_id] = TaskRetryState(
                task_id=task_id,
                max_retries=self._config.max_retries_per_task,
            )
        return self._task_states[task_id]

    def can_retry(self, task_id: str) -> bool:
        """Check whether a task can be retried.

        Returns False if either the per-task or per-run budget is exhausted.

        Args:
            task_id: The task to check.

        Returns:
            True if a retry is allowed.
        """
        if self.run_budget_exhausted:
            return False
        state = self._get_state(task_id)
        return not state.exhausted

    def record_retry(
        self,
        task_id: str,
        *,
        reason: str = "",
        timestamp: float | None = None,
    ) -> RetryRecord:
        """Record a retry attempt for a task.

        Args:
            task_id: The task being retried.
            reason: Why the retry was needed.
            timestamp: Epoch seconds. Defaults to time.time().

        Returns:
            The created RetryRecord.

        Raises:
            ValueError: If the task or run retry budget is exhausted.
        """
        if not self.can_retry(task_id):
            state = self._get_state(task_id)
            if state.exhausted:
                raise ValueError(f"Task {task_id} retry budget exhausted ({state.retries}/{state.max_retries})")
            raise ValueError(f"Run retry budget exhausted ({self._total_retries}/{self._config.max_retries_per_run})")

        ts = timestamp if timestamp is not None else time.time()
        state = self._get_state(task_id)
        state.retries += 1
        self._total_retries += 1

        record = RetryRecord(
            task_id=task_id,
            attempt=state.retries,
            timestamp=ts,
            reason=reason,
        )
        state.records.append(record)

        logger.info(
            "Retry %d/%d for task %s (run total: %d). Reason: %s",
            state.retries,
            state.max_retries,
            task_id,
            self._total_retries,
            reason or "unspecified",
        )
        return record

    def backoff_seconds(self, task_id: str) -> float:
        """Calculate the exponential backoff delay for the next retry.

        Args:
            task_id: The task to calculate backoff for.

        Returns:
            Delay in seconds before the next retry should be attempted.
        """
        state = self._get_state(task_id)
        delay = self._config.backoff_base_seconds * (2**state.retries)
        return min(delay, self._config.backoff_max_seconds)

    def get_state(self, task_id: str) -> TaskRetryState:
        """Get retry state for a task.

        Args:
            task_id: The task to look up.

        Returns:
            TaskRetryState with current retry count and history.
        """
        return self._get_state(task_id)

    def summary(self) -> dict[str, object]:
        """Return a summary of the retry budget state.

        Returns:
            Dict with total_retries, run_budget_remaining, and per-task states.
        """
        return {
            "total_retries": self._total_retries,
            "run_budget_remaining": self.run_budget_remaining,
            "run_budget_exhausted": self.run_budget_exhausted,
            "per_task": {
                tid: {"retries": s.retries, "max": s.max_retries, "exhausted": s.exhausted}
                for tid, s in self._task_states.items()
            },
        }
