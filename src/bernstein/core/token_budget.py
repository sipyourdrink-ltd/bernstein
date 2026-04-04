"""Token budget tracking and growth monitoring for context injection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default token budget per task complexity
DEFAULT_TOKEN_BUDGETS = {
    "small": 10_000,
    "medium": 25_000,
    "large": 50_000,
    "xl": 100_000,
}

# Token growth monitoring thresholds
GROWTH_MONITOR_WINDOW = 3  # Number of turns to monitor
GROWTH_THRESHOLD = 2.0  # 2x growth triggers intervention
MAX_GROWTH_MULTIPLIER = 5.0  # 5x budget triggers hard stop


@dataclass
class TokenBudget:
    """Token budget for a single task.

    Tracks token consumption across compaction events so callers see a single
    logical spend for the full task lifetime, not just the current compaction
    window.

    Attributes:
        task_id: Task identifier.
        budget_tokens: Maximum tokens allowed for context.
        used_tokens: Tokens used in the *current* compaction window.
        remaining_tokens: Tokens remaining in the current window.
        complexity: Task complexity level.
        pre_compact_used: Cumulative tokens consumed across all *previous*
            compaction windows.  Persisted before each compaction event so the
            total logical spend (``pre_compact_used + used_tokens``) stays
            accurate even after context is compressed.
        compaction_count: Number of compaction events recorded for this task.
    """

    task_id: str
    budget_tokens: int
    used_tokens: int = 0
    remaining_tokens: int = 0
    complexity: str = "medium"
    pre_compact_used: int = 0
    compaction_count: int = 0

    def __post_init__(self) -> None:
        self.remaining_tokens = self.budget_tokens - self.used_tokens

    def consume(self, tokens: int) -> bool:
        """Consume tokens from budget.

        Args:
            tokens: Number of tokens to consume.

        Returns:
            True if consumption was successful, False if over budget.
        """
        if tokens > self.remaining_tokens:
            logger.warning(
                "Task %s: Token budget exceeded (requested=%d, remaining=%d)",
                self.task_id,
                tokens,
                self.remaining_tokens,
            )
            return False

        self.used_tokens += tokens
        self.remaining_tokens = self.budget_tokens - self.used_tokens
        return True

    def record_pre_compaction(self, tokens_used: int) -> None:
        """Snapshot current usage before a compaction event.

        Called immediately before context is compressed.  Accumulates the
        pre-compaction token count into ``pre_compact_used`` so that subsequent
        calls to :meth:`total_logical_spend` and :meth:`effective_remaining`
        account for all historical spending.

        After compaction the current session restarts with a lower context
        size, so ``used_tokens`` resets externally.  ``pre_compact_used``
        retains the history.

        Args:
            tokens_used: Estimated token count of the task prompt before
                compaction (typically ``len(description) // 4``).
        """
        self.pre_compact_used += tokens_used
        self.compaction_count += 1
        logger.debug(
            "Task %s: pre-compaction snapshot %d tokens (cumulative=%d, event=%d)",
            self.task_id,
            tokens_used,
            self.pre_compact_used,
            self.compaction_count,
        )

    def reconcile_post_compaction(self) -> None:
        """Recompute remaining budget after compaction reduces context size.

        Effective remaining = ``budget_tokens`` minus the *total* logical spend
        (pre-compaction history + current window).  This keeps cost accounting
        aligned with actual model usage even when context has been summarized.
        """
        self.remaining_tokens = max(0, self.budget_tokens - self.total_logical_spend())
        logger.debug(
            "Task %s: post-compaction reconcile — effective_remaining=%d (budget=%d, pre_compact=%d, current=%d)",
            self.task_id,
            self.remaining_tokens,
            self.budget_tokens,
            self.pre_compact_used,
            self.used_tokens,
        )

    def total_logical_spend(self) -> int:
        """Total tokens consumed across all compaction windows.

        Returns:
            Sum of pre-compaction historical spend and current window usage.
        """
        return self.pre_compact_used + self.used_tokens

    def effective_remaining(self) -> int:
        """Budget remaining after accounting for all historical spend.

        Returns:
            Remaining tokens clamped to zero.
        """
        return max(0, self.budget_tokens - self.total_logical_spend())

    def utilization_pct(self) -> float:
        """Return budget utilization percentage based on total logical spend."""
        if self.budget_tokens == 0:
            return 0.0
        return (self.total_logical_spend() / self.budget_tokens) * 100


@dataclass
class TokenGrowthMonitor:
    """Monitor token growth per agent session.

    Detects quadratic token growth and triggers intervention.

    Attributes:
        session_id: Agent session identifier.
        token_history: List of token counts per turn.
        growth_rate: Current growth rate.
        intervention_triggered: Whether intervention was triggered.
        compaction_fail_count: Consecutive compaction failures.
    """

    session_id: str
    token_history: list[int] = field(default_factory=list[int])
    growth_rate: float = 0.0
    intervention_triggered: bool = False
    compaction_fail_count: int = 0

    def record_turn(self, tokens: int) -> None:
        """Record token count for a turn.

        Args:
            tokens: Token count for this turn.
        """
        self.token_history.append(tokens)

        # Calculate growth rate if we have enough history
        if len(self.token_history) >= 2:
            self._calculate_growth_rate()

            # Check for intervention
            if not self.intervention_triggered:
                self._check_intervention()

    def record_compaction_failure(self) -> None:
        """Record a compaction failure and potentially open the circuit breaker."""
        self.compaction_fail_count += 1
        logger.warning(
            "Session %s: Compaction failure recorded (total=%d)",
            self.session_id,
            self.compaction_fail_count,
        )

    def record_compaction_success(self) -> None:
        """Record a successful compaction and reset the failure counter."""
        self.compaction_fail_count = 0
        self.intervention_triggered = False

    def _calculate_growth_rate(self) -> None:
        """Calculate token growth rate over recent turns."""
        if len(self.token_history) < 2:
            return

        # Use recent window for growth calculation
        window = self.token_history[-GROWTH_MONITOR_WINDOW:]
        if len(window) < 2:
            return

        # Calculate average growth rate
        growth_rates: list[float] = []
        for i in range(1, len(window)):
            if window[i - 1] > 0:
                rate = window[i] / window[i - 1]
                growth_rates.append(rate)

        if growth_rates:
            self.growth_rate = sum(growth_rates) / len(growth_rates)

    def _check_intervention(self) -> None:
        """Check if intervention should be triggered."""
        if self.growth_rate >= GROWTH_THRESHOLD:
            logger.warning(
                "Session %s: Quadratic token growth detected (rate=%.2fx)",
                self.session_id,
                self.growth_rate,
            )
            self.intervention_triggered = True

    def should_compact(self) -> bool:
        """Return whether session should be compacted.

        Circuit breaker opens after 3 consecutive failures.
        """
        if self.compaction_fail_count >= 3:
            if self.intervention_triggered:
                logger.error(
                    "Session %s: Compaction circuit breaker OPEN after %d failures. Skipping compaction.",
                    self.session_id,
                    self.compaction_fail_count,
                )
            return False

        return self.intervention_triggered

    def get_summary(self) -> dict[str, Any]:
        """Get monitoring summary."""
        return {
            "session_id": self.session_id,
            "turns": len(self.token_history),
            "current_tokens": self.token_history[-1] if self.token_history else 0,
            "growth_rate": round(self.growth_rate, 2),
            "intervention_triggered": self.intervention_triggered,
        }


class TokenBudgetManager:
    """Manage token budgets across all tasks.

    Args:
        workdir: Project working directory.
        budgets: Optional custom budget configuration.
    """

    def __init__(
        self,
        workdir: Path,
        budgets: dict[str, int] | None = None,
    ) -> None:
        self._workdir = workdir
        self._budgets = budgets or DEFAULT_TOKEN_BUDGETS
        self._task_budgets: dict[str, TokenBudget] = {}
        self._growth_monitors: dict[str, TokenGrowthMonitor] = {}

    def get_budget(self, task_id: str, complexity: str = "medium") -> TokenBudget:
        """Get or create token budget for a task.

        Args:
            task_id: Task identifier.
            complexity: Task complexity level.

        Returns:
            TokenBudget for the task.
        """
        if task_id not in self._task_budgets:
            budget_tokens = self._budgets.get(complexity, self._budgets["medium"])
            self._task_budgets[task_id] = TokenBudget(
                task_id=task_id,
                budget_tokens=budget_tokens,
                complexity=complexity,
            )

        return self._task_budgets[task_id]

    def get_growth_monitor(self, session_id: str) -> TokenGrowthMonitor:
        """Get or create growth monitor for a session.

        Args:
            session_id: Agent session identifier.

        Returns:
            TokenGrowthMonitor for the session.
        """
        if session_id not in self._growth_monitors:
            self._growth_monitors[session_id] = TokenGrowthMonitor(session_id=session_id)

        return self._growth_monitors[session_id]

    def check_all_sessions(self) -> list[str]:
        """Check all sessions for intervention needs.

        Returns:
            List of session IDs requiring intervention.
        """
        interventions: list[str] = []
        for session_id, monitor in self._growth_monitors.items():
            if monitor.should_compact():
                interventions.append(session_id)
                logger.info(
                    "Session %s requires compaction (growth_rate=%.2fx)",
                    session_id,
                    monitor.growth_rate,
                )

        return interventions

    def get_summary(self) -> dict[str, Any]:
        """Get budget and monitoring summary."""
        return {
            "task_budgets": {
                task_id: {
                    "budget": b.budget_tokens,
                    "used": b.used_tokens,
                    "remaining": b.remaining_tokens,
                    "utilization_pct": round(b.utilization_pct(), 1),
                }
                for task_id, b in self._task_budgets.items()
            },
            "growth_monitors": {
                session_id: monitor.get_summary() for session_id, monitor in self._growth_monitors.items()
            },
        }
