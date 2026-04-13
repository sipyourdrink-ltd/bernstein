"""Memory leak detection for long-running agent sessions."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemorySample:
    """A single memory usage sample."""

    timestamp: float
    rss_bytes: int


@dataclass
class AgentMemoryHistory:
    """History of memory usage for a single agent."""

    session_id: str
    pid: int
    samples: list[MemorySample] = field(default_factory=list[MemorySample])
    max_samples: int = 20

    def add_sample(self, rss_bytes: int) -> None:
        """Add a new memory sample and maintain window size."""
        self.samples.append(MemorySample(time.time(), rss_bytes))
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)

    def is_leaking(self, threshold_mb: float = 200.0, min_samples: int = 5) -> bool:
        """Detect if memory is monotonically increasing and exceeds growth threshold.

        Args:
            threshold_mb: Growth in MB that triggers an alert.
            min_samples: Minimum samples required before detection.

        Returns:
            True if a leak is detected.
        """
        if len(self.samples) < min_samples:
            return False

        # Check for monotonic increase in last min_samples
        recent = self.samples[-min_samples:]
        is_increasing = all(recent[i].rss_bytes < recent[i + 1].rss_bytes for i in range(len(recent) - 1))

        if not is_increasing:
            return False

        # Check total growth in the window
        growth_bytes = self.samples[-1].rss_bytes - self.samples[0].rss_bytes
        growth_mb = growth_bytes / (1024 * 1024)

        return growth_mb >= threshold_mb


def get_rss_bytes(pid: int) -> int | None:
    """Get Resident Set Size (RSS) for a process in bytes.

    Uses 'ps' command as a portable fallback when psutil is unavailable.
    """
    try:
        # ps -o rss= returns RSS in KB
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        rss_kb = int(result.stdout.strip())
        return rss_kb * 1024
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


class MemoryGuard:
    """Monitors active agents for memory leaks."""

    def __init__(self) -> None:
        self._histories: dict[str, AgentMemoryHistory] = {}

    def monitor_agents(self, active_agents: list[Any]) -> list[str]:
        """Update memory history for active agents and return list of leaking sessions.

        Args:
            active_agents: List of agent session objects (must have id and pid).

        Returns:
            List of session IDs where leaks were detected.
        """
        leaking: list[str] = []
        active_ids = {a.id for a in active_agents if getattr(a, "pid", None)}

        # Cleanup old histories
        self._histories = {sid: h for sid, h in self._histories.items() if sid in active_ids}

        for agent in active_agents:
            pid = getattr(agent, "pid", None)
            if pid is None:
                continue

            if agent.id not in self._histories:
                self._histories[agent.id] = AgentMemoryHistory(agent.id, pid)

            history = self._histories[agent.id]
            rss = get_rss_bytes(pid)
            if rss is not None:
                history.add_sample(rss)
                if history.is_leaking():
                    leaking.append(agent.id)
                    logger.warning("Memory leak detected in agent %s (PID %d)", agent.id, pid)

        return leaking  # type: ignore[return-value]
