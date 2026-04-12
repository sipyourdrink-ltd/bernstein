"""Spawn failure analysis and retry recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.adapters.base import RateLimitError, SpawnError
from bernstein.core.container import ContainerError
from bernstein.core.worktree import WorktreeError

if TYPE_CHECKING:
    from bernstein.core.models import Task


@dataclass(frozen=True)
class SpawnFailureAnalysis:
    """Analysis of one spawn failure."""

    error_type: str
    is_transient: bool
    recommended_delay_s: float
    recommended_action: str
    detail: str


class SpawnAnalyzer:
    """Analyze spawn failures and recommend retry behavior."""

    def analyze(self, error: Exception, task: Task) -> SpawnFailureAnalysis:
        """Classify a spawn error and recommend recovery."""
        lowered = str(error).lower()
        if isinstance(error, RateLimitError):
            return SpawnFailureAnalysis("rate_limit", True, 60.0, "wait", str(error))
        if isinstance(error, SpawnError) and "adapter not found" in lowered:
            return SpawnFailureAnalysis("adapter_missing", False, 0.0, "skip", str(error))
        if isinstance(error, WorktreeError):
            return SpawnFailureAnalysis("worktree_error", True, 10.0, "wait", str(error))
        if isinstance(error, ContainerError):
            return SpawnFailureAnalysis("container_error", False, 0.0, "reconfigure", str(error))
        if "network" in lowered or "dns" in lowered or "connection" in lowered:
            return SpawnFailureAnalysis("network_error", True, 30.0, "wait", str(error))
        return SpawnFailureAnalysis("unknown", True, 30.0, "wait", f"{task.role}: {error}")

    def should_retry(
        self,
        failure_history: list[SpawnFailureAnalysis],
        max_retries: int = 3,
    ) -> tuple[bool, float]:
        """Return whether the batch should retry and the recommended delay."""
        if not failure_history:
            return (True, 0.0)
        if any(not failure.is_transient for failure in failure_history):
            return (False, 0.0)

        delay = max(failure.recommended_delay_s for failure in failure_history)
        repeated = {
            error_type: sum(1 for failure in failure_history if failure.error_type == error_type)
            for error_type in {failure.error_type for failure in failure_history}
        }
        if any(count >= max_retries for count in repeated.values()):
            return (False, delay)

        return (len(failure_history) < max_retries, delay)
