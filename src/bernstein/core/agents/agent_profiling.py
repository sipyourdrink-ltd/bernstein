"""Agent performance profiling: spawn time, token rate, completion time (agent-018).

Provides dataclasses and utilities to compute and display per-agent
performance profiles, enabling comparison of spawn latency, throughput,
and completion times across agents in an orchestration run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentProfile:
    """Immutable performance profile for a single agent session.

    Attributes:
        agent_id: Agent session identifier.
        role: Role the agent was assigned.
        model: Model used by the agent.
        spawn_latency_s: Wall-clock seconds from spawn request to first output.
        tokens_per_minute: Sustained token throughput (tokens / minute).
        time_to_first_output_s: Seconds until the agent produced its first output.
        total_completion_s: Total wall-clock seconds from spawn to session end.
        task_count: Number of tasks the agent worked on.
    """

    agent_id: str
    role: str
    model: str
    spawn_latency_s: float
    tokens_per_minute: float
    time_to_first_output_s: float
    total_completion_s: float
    task_count: int

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "model": self.model,
            "spawn_latency_s": round(self.spawn_latency_s, 3),
            "tokens_per_minute": round(self.tokens_per_minute, 1),
            "time_to_first_output_s": round(self.time_to_first_output_s, 3),
            "total_completion_s": round(self.total_completion_s, 3),
            "task_count": self.task_count,
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_profile(
    agent_id: str,
    role: str,
    model: str,
    spawn_ts: float,
    first_output_ts: float,
    end_ts: float,
    total_tokens: int,
    task_count: int,
) -> AgentProfile:
    """Compute an agent performance profile from raw timestamps and token counts.

    Args:
        agent_id: Agent session identifier.
        role: Role the agent was assigned.
        model: Model used by the agent.
        spawn_ts: Unix timestamp when the agent spawn was requested.
        first_output_ts: Unix timestamp of the agent's first output.
        end_ts: Unix timestamp when the agent session ended.
        total_tokens: Total tokens consumed (input + output).
        task_count: Number of tasks the agent worked on.

    Returns:
        A frozen AgentProfile with computed metrics.
    """
    spawn_latency_s = max(0.0, first_output_ts - spawn_ts)
    time_to_first_output_s = spawn_latency_s  # same measure
    total_completion_s = max(0.0, end_ts - spawn_ts)

    # Compute tokens per minute; avoid division by zero.
    tokens_per_minute = total_tokens / total_completion_s * 60.0 if total_completion_s > 0.0 else 0.0

    return AgentProfile(
        agent_id=agent_id,
        role=role,
        model=model,
        spawn_latency_s=spawn_latency_s,
        tokens_per_minute=tokens_per_minute,
        time_to_first_output_s=time_to_first_output_s,
        total_completion_s=total_completion_s,
        task_count=task_count,
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def format_profile_table(profiles: list[AgentProfile]) -> str:
    """Render a list of agent profiles as a Rich table string.

    Args:
        profiles: Agent profiles to display.

    Returns:
        A formatted string containing the table.
    """
    table = Table(title="Agent Performance Profiles")
    table.add_column("Agent ID", style="cyan", no_wrap=True)
    table.add_column("Role", style="green")
    table.add_column("Model", style="magenta")
    table.add_column("Spawn Latency (s)", justify="right")
    table.add_column("Tokens/min", justify="right")
    table.add_column("TTFO (s)", justify="right")
    table.add_column("Total (s)", justify="right")
    table.add_column("Tasks", justify="right")

    for p in profiles:
        table.add_row(
            p.agent_id,
            p.role,
            p.model,
            f"{p.spawn_latency_s:.2f}",
            f"{p.tokens_per_minute:.1f}",
            f"{p.time_to_first_output_s:.2f}",
            f"{p.total_completion_s:.2f}",
            str(p.task_count),
        )

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    console.print(table)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_profiles(profiles: list[AgentProfile]) -> dict[str, Any]:
    """Compute summary statistics across a list of agent profiles.

    Args:
        profiles: Agent profiles to aggregate.

    Returns:
        A dict with summary stats.  Returns zeroed stats for an empty list.
    """
    n = len(profiles)
    if n == 0:
        return {
            "count": 0,
            "avg_spawn_latency_s": 0.0,
            "avg_tokens_per_minute": 0.0,
            "avg_time_to_first_output_s": 0.0,
            "avg_total_completion_s": 0.0,
            "total_tasks": 0,
            "min_tokens_per_minute": 0.0,
            "max_tokens_per_minute": 0.0,
        }

    total_spawn = sum(p.spawn_latency_s for p in profiles)
    total_tpm = sum(p.tokens_per_minute for p in profiles)
    total_ttfo = sum(p.time_to_first_output_s for p in profiles)
    total_completion = sum(p.total_completion_s for p in profiles)
    total_tasks = sum(p.task_count for p in profiles)

    return {
        "count": n,
        "avg_spawn_latency_s": round(total_spawn / n, 3),
        "avg_tokens_per_minute": round(total_tpm / n, 1),
        "avg_time_to_first_output_s": round(total_ttfo / n, 3),
        "avg_total_completion_s": round(total_completion / n, 3),
        "total_tasks": total_tasks,
        "min_tokens_per_minute": round(min(p.tokens_per_minute for p in profiles), 1),
        "max_tokens_per_minute": round(max(p.tokens_per_minute for p in profiles), 1),
    }
