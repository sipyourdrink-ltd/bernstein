"""Per-agent token usage tracker — lightweight thread-safe token accounting."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class AgentTokenUsage:
    """Token usage snapshot for a single agent."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed (input + output)."""
        return self.input_tokens + self.output_tokens


class AgentTokenTracker:
    """Thread-safe per-agent token usage tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, AgentTokenUsage] = {}

    def record(self, agent_id: str, usage: AgentTokenUsage) -> None:
        """Record token usage for an agent (replaces previous snapshot)."""
        with self._lock:
            self._agents[agent_id] = AgentTokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
            )

    def accumulate(self, agent_id: str, delta: AgentTokenUsage) -> None:
        """Accumulate token usage deltas for an agent."""
        with self._lock:
            if agent_id not in self._agents:
                self._agents[agent_id] = AgentTokenUsage()
            current = self._agents[agent_id]
            current.input_tokens += delta.input_tokens
            current.output_tokens += delta.output_tokens
            current.cache_read_tokens += delta.cache_read_tokens
            current.cache_creation_tokens += delta.cache_creation_tokens

    def get(self, agent_id: str) -> AgentTokenUsage | None:
        """Get token usage for an agent, or None if not tracked."""
        with self._lock:
            entry = self._agents.get(agent_id)
            if entry is None:
                return None
            return AgentTokenUsage(
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
            )

    def snapshot(self) -> dict[str, AgentTokenUsage]:
        """Return a snapshot of all agent token usage."""
        with self._lock:
            return {
                aid: AgentTokenUsage(
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_creation_tokens=u.cache_creation_tokens,
                )
                for aid, u in self._agents.items()
            }

    def total_usage(self) -> AgentTokenUsage:
        """Return aggregate token usage across all agents."""
        with self._lock:
            total = AgentTokenUsage()
            for u in self._agents.values():
                total.input_tokens += u.input_tokens
                total.output_tokens += u.output_tokens
                total.cache_read_tokens += u.cache_read_tokens
                total.cache_creation_tokens += u.cache_creation_tokens
            return total

    def reset(self, agent_id: str) -> None:
        """Reset token usage for a single agent."""
        with self._lock:
            self._agents.pop(agent_id, None)

    def clear(self) -> None:
        """Clear all token usage data."""
        with self._lock:
            self._agents.clear()


_global_tracker = AgentTokenTracker()


def get_token_tracker() -> AgentTokenTracker:
    """Get the global per-agent token tracker singleton."""
    return _global_tracker
