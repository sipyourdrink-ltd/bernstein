"""Staggered agent shutdown during drain (AGENT-014).

Kills agents in reverse-priority order with configurable intervals
between each kill, giving high-priority agents the most time to
finish their work.

Usage::

    shutdown = StaggeredShutdown(
        config=StaggeredShutdownConfig(interval_seconds=5.0)
    )
    report = await shutdown.execute(agents, kill_fn=my_kill)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaggeredShutdownConfig:
    """Configuration for staggered shutdown.

    Attributes:
        interval_seconds: Delay between killing each agent.
        grace_seconds: Grace period after kill signal before force-kill.
        reverse_priority: If True (default), kill lowest-priority agents first.
    """

    interval_seconds: float = 5.0
    grace_seconds: float = 10.0
    reverse_priority: bool = True


# ---------------------------------------------------------------------------
# Agent info for ordering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShutdownTarget:
    """An agent to be shut down.

    Attributes:
        session_id: Agent session identifier.
        pid: Process ID.
        role: Agent role (for logging).
        priority: Task priority (1=highest, higher number=lower priority).
    """

    session_id: str
    pid: int
    role: str = ""
    priority: int = 2


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ShutdownStep:
    """Record of shutting down a single agent.

    Attributes:
        session_id: Agent session identifier.
        pid: Process ID.
        role: Agent role.
        priority: Task priority at time of shutdown.
        killed_at: Monotonic timestamp when the kill signal was sent.
        success: True if the agent was killed successfully.
        error: Error message if the kill failed.
    """

    session_id: str
    pid: int
    role: str
    priority: int
    killed_at: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class ShutdownReport:
    """Report of a staggered shutdown operation.

    Attributes:
        steps: Per-agent shutdown records.
        total_seconds: Total wall-clock time for the shutdown.
        agents_killed: Number of agents successfully killed.
        agents_failed: Number of agents that failed to shut down.
    """

    steps: list[ShutdownStep] = field(default_factory=list[ShutdownStep])
    total_seconds: float = 0.0

    @property
    def agents_killed(self) -> int:
        """Number of agents successfully killed."""
        return sum(1 for s in self.steps if s.success)

    @property
    def agents_failed(self) -> int:
        """Number of agents that failed to shut down."""
        return sum(1 for s in self.steps if not s.success)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class StaggeredShutdown:
    """Execute staggered shutdown of agents in priority order.

    Args:
        config: Shutdown configuration.
    """

    def __init__(self, config: StaggeredShutdownConfig | None = None) -> None:
        self._config = config or StaggeredShutdownConfig()

    @property
    def config(self) -> StaggeredShutdownConfig:
        """Return the shutdown configuration."""
        return self._config

    def order_targets(self, targets: list[ShutdownTarget]) -> list[ShutdownTarget]:
        """Order targets for shutdown.

        When ``reverse_priority`` is True, lowest-priority agents (highest
        priority number) are killed first, giving critical agents more time.

        Args:
            targets: Agents to order.

        Returns:
            Ordered list of targets.
        """
        return sorted(
            targets,
            key=lambda t: t.priority,
            reverse=self._config.reverse_priority,
        )

    async def execute(
        self,
        targets: list[ShutdownTarget],
        kill_fn: Callable[[int], Awaitable[bool]] | Callable[[int], bool],
    ) -> ShutdownReport:
        """Execute staggered shutdown.

        Args:
            targets: Agents to shut down.
            kill_fn: Function that sends a kill signal to a PID.
                Returns True on success.  Can be sync or async.

        Returns:
            ShutdownReport with per-agent results.
        """
        report = ShutdownReport()
        start = time.monotonic()

        ordered = self.order_targets(targets)

        for i, target in enumerate(ordered):
            step = ShutdownStep(
                session_id=target.session_id,
                pid=target.pid,
                role=target.role,
                priority=target.priority,
                killed_at=time.monotonic(),
            )

            try:
                result = kill_fn(target.pid)
                if asyncio.iscoroutine(result):
                    step.success = bool(await result)
                else:
                    step.success = bool(result)
            except Exception as exc:
                step.success = False
                step.error = str(exc)
                logger.warning(
                    "Staggered shutdown: failed to kill %s (pid=%d): %s",
                    target.session_id,
                    target.pid,
                    exc,
                )

            report.steps.append(step)
            logger.info(
                "Staggered shutdown [%d/%d]: %s role=%s priority=%d pid=%d %s",
                i + 1,
                len(ordered),
                target.session_id,
                target.role,
                target.priority,
                target.pid,
                "OK" if step.success else f"FAILED: {step.error}",
            )

            # Wait between kills (but not after the last one)
            if i < len(ordered) - 1 and self._config.interval_seconds > 0:
                await asyncio.sleep(self._config.interval_seconds)

        report.total_seconds = time.monotonic() - start
        return report
