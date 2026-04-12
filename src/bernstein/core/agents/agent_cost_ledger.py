"""Per-agent cost attribution and leaderboard (COST-008).

Track cost per agent across tasks, rank agents by cost efficiency
(cost per successful task), and produce a leaderboard for reporting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AgentCostEntry:
    """Cost attribution for a single agent.

    Attributes:
        agent_id: Agent session identifier.
        role: Role the agent was assigned.
        model: Model used by the agent.
        total_cost_usd: Cumulative cost incurred.
        tasks_completed: Number of tasks completed successfully.
        tasks_failed: Number of tasks that failed.
        total_input_tokens: Total input tokens consumed.
        total_output_tokens: Total output tokens consumed.
        total_duration_s: Total wall-clock seconds the agent ran.
    """

    agent_id: str
    role: str
    model: str
    total_cost_usd: float = 0.0
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_s: float = 0.0

    @property
    def cost_per_task(self) -> float:
        """Average cost per completed task (inf if no completions)."""
        if self.tasks_completed == 0:
            return float("inf")
        return self.total_cost_usd / self.tasks_completed

    @property
    def success_rate(self) -> float:
        """Task success rate (0.0-1.0)."""
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 0.0
        return self.tasks_completed / total

    @property
    def efficiency_score(self) -> float:
        """Efficiency score: success_rate / (cost_per_task + 0.001).

        Higher is better.  The 0.001 prevents division by zero.
        """
        cpt = self.cost_per_task
        if cpt == float("inf"):
            return 0.0
        return self.success_rate / (cpt + 0.001)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "model": self.model,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_duration_s": round(self.total_duration_s, 2),
            "cost_per_task": round(self.cost_per_task, 6) if self.tasks_completed > 0 else None,
            "success_rate": round(self.success_rate, 4),
            "efficiency_score": round(self.efficiency_score, 4),
        }


@dataclass(frozen=True)
class LeaderboardEntry:
    """A single entry in the cost efficiency leaderboard.

    Attributes:
        rank: 1-based rank (1 = most efficient).
        agent_id: Agent session identifier.
        role: Agent role.
        model: Model used.
        cost_per_task: Average cost per completed task.
        success_rate: Task success rate.
        efficiency_score: Combined efficiency metric.
        total_cost_usd: Total spend.
    """

    rank: int
    agent_id: str
    role: str
    model: str
    cost_per_task: float
    success_rate: float
    efficiency_score: float
    total_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "rank": self.rank,
            "agent_id": self.agent_id,
            "role": self.role,
            "model": self.model,
            "cost_per_task": round(self.cost_per_task, 6),
            "success_rate": round(self.success_rate, 4),
            "efficiency_score": round(self.efficiency_score, 4),
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class AgentCostLedger:
    """Track per-agent cost and produce efficiency leaderboards.

    Args:
        run_id: Orchestrator run identifier.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._entries: dict[str, AgentCostEntry] = {}

    def record_cost(
        self,
        agent_id: str,
        role: str,
        model: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Record a cost observation for an agent.

        Creates the entry if it does not exist, otherwise accumulates.

        Args:
            agent_id: Agent session ID.
            role: Agent role.
            model: Model used.
            cost_usd: Cost in USD for this observation.
            input_tokens: Input tokens consumed.
            output_tokens: Output tokens consumed.
        """
        if agent_id not in self._entries:
            self._entries[agent_id] = AgentCostEntry(
                agent_id=agent_id,
                role=role,
                model=model,
            )
        entry = self._entries[agent_id]
        entry.total_cost_usd += cost_usd
        entry.total_input_tokens += input_tokens
        entry.total_output_tokens += output_tokens

    def record_task_result(
        self,
        agent_id: str,
        success: bool,
        duration_s: float = 0.0,
    ) -> None:
        """Record a task completion or failure for an agent.

        Args:
            agent_id: Agent session ID.
            success: Whether the task succeeded.
            duration_s: Wall-clock duration of the task.
        """
        entry = self._entries.get(agent_id)
        if entry is None:
            return
        if success:
            entry.tasks_completed += 1
        else:
            entry.tasks_failed += 1
        entry.total_duration_s += duration_s

    def leaderboard(self, *, min_tasks: int = 1) -> list[LeaderboardEntry]:
        """Build a cost-efficiency leaderboard.

        Agents are ranked by :attr:`AgentCostEntry.efficiency_score`
        (descending).  Only agents with at least ``min_tasks`` completed
        tasks are included.

        Args:
            min_tasks: Minimum completed tasks to qualify.

        Returns:
            Ranked list of :class:`LeaderboardEntry`.
        """
        qualifying = [e for e in self._entries.values() if e.tasks_completed >= min_tasks]
        qualifying.sort(key=lambda e: e.efficiency_score, reverse=True)

        return [
            LeaderboardEntry(
                rank=i + 1,
                agent_id=e.agent_id,
                role=e.role,
                model=e.model,
                cost_per_task=e.cost_per_task,
                success_rate=e.success_rate,
                efficiency_score=e.efficiency_score,
                total_cost_usd=e.total_cost_usd,
            )
            for i, e in enumerate(qualifying)
        ]

    def get_entry(self, agent_id: str) -> AgentCostEntry | None:
        """Return the cost entry for an agent, if it exists."""
        return self._entries.get(agent_id)

    def total_cost(self) -> float:
        """Total cost across all agents."""
        return sum(e.total_cost_usd for e in self._entries.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full ledger to a JSON-safe dict."""
        return {
            "run_id": self.run_id,
            "total_cost_usd": round(self.total_cost(), 6),
            "agents": {aid: e.to_dict() for aid, e in self._entries.items()},
        }
