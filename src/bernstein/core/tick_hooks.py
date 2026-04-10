"""Tick lifecycle plugin extension points for the orchestrator.

Provides pre-tick, post-tick, pre-spawn, and post-spawn hooks that
plugins can implement to observe or react to orchestrator tick events.
Uses pluggy for hook dispatch, consistent with the existing plugin
infrastructure in ``bernstein.plugins``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import pluggy

from bernstein.plugins import hookspec

log = logging.getLogger(__name__)

__all__ = [
    "TickContext",
    "TickHookManager",
    "TickHookResult",
    "TickHookSpec",
]


@dataclass(frozen=True, slots=True)
class TickContext:
    """Immutable snapshot of orchestrator state at a given tick.

    Attributes:
        tick_number: Monotonically increasing tick counter.
        timestamp: Unix timestamp (seconds since epoch) when the tick began.
        active_agents: Number of currently running agent sessions.
        pending_tasks: Number of tasks in the ``open`` state.
        completed_tasks: Number of tasks in the ``done`` state.
        cost_usd: Cumulative estimated cost in USD so far.
    """

    tick_number: int
    timestamp: float
    active_agents: int
    pending_tasks: int
    completed_tasks: int
    cost_usd: float


@dataclass(frozen=True, slots=True)
class TickHookResult:
    """Result returned by a tick lifecycle hook invocation.

    Attributes:
        hook_name: Name of the plugin that produced this result.
        phase: Which lifecycle phase the hook ran in.
        success: Whether the hook executed without error.
        duration_ms: Wall-clock execution time in milliseconds.
        message: Human-readable status or error message.
    """

    hook_name: str
    phase: Literal["pre_tick", "post_tick", "pre_spawn", "post_spawn"]
    success: bool
    duration_ms: float
    message: str


class TickHookSpec:
    """Pluggy hook specifications for orchestrator tick lifecycle events.

    Plugins implement one or more of these methods via ``@hookimpl``
    to receive callbacks at each stage of the orchestrator's tick loop.
    """

    @hookspec
    def pre_tick(self, context: TickContext) -> TickHookResult | None:
        """Called before the orchestrator processes a tick.

        Args:
            context: Immutable snapshot of orchestrator state.

        Returns:
            Optional result describing the hook's outcome.
        """

    @hookspec
    def post_tick(self, context: TickContext) -> TickHookResult | None:
        """Called after the orchestrator finishes processing a tick.

        Args:
            context: Immutable snapshot of orchestrator state.

        Returns:
            Optional result describing the hook's outcome.
        """

    @hookspec
    def pre_spawn(
        self,
        context: TickContext,
        task_id: str,
        role: str,
    ) -> TickHookResult | None:
        """Called before an agent is spawned for a task.

        Args:
            context: Immutable snapshot of orchestrator state.
            task_id: Identifier of the task being assigned.
            role: Role of the agent about to be spawned.

        Returns:
            Optional result describing the hook's outcome.
        """

    @hookspec
    def post_spawn(
        self,
        context: TickContext,
        task_id: str,
        agent_id: str,
    ) -> TickHookResult | None:
        """Called after an agent has been spawned for a task.

        Args:
            context: Immutable snapshot of orchestrator state.
            task_id: Identifier of the task that was assigned.
            agent_id: Identifier of the newly spawned agent session.

        Returns:
            Optional result describing the hook's outcome.
        """


class TickHookManager:
    """Manages registration and invocation of tick lifecycle hooks.

    Wraps a pluggy ``PluginManager`` scoped to the ``bernstein`` project,
    registers the :class:`TickHookSpec` specifications, and provides
    convenience methods for running each hook phase with error handling.
    """

    def __init__(self) -> None:
        self._pm = pluggy.PluginManager("bernstein")
        self._pm.add_hookspecs(TickHookSpec)

    def register(self, plugin: object) -> None:
        """Register a plugin that implements one or more tick hook methods.

        Args:
            plugin: An object whose methods are decorated with ``@hookimpl``.
        """
        self._pm.register(plugin)

    def run_pre_tick(self, context: TickContext) -> list[TickHookResult]:
        """Execute all registered ``pre_tick`` hooks.

        Args:
            context: Current tick state snapshot.

        Returns:
            List of results from each hook invocation.
        """
        return self._run_hooks("pre_tick", context=context)

    def run_post_tick(self, context: TickContext) -> list[TickHookResult]:
        """Execute all registered ``post_tick`` hooks.

        Args:
            context: Current tick state snapshot.

        Returns:
            List of results from each hook invocation.
        """
        return self._run_hooks("post_tick", context=context)

    def run_pre_spawn(
        self,
        context: TickContext,
        task_id: str,
        role: str,
    ) -> list[TickHookResult]:
        """Execute all registered ``pre_spawn`` hooks.

        Args:
            context: Current tick state snapshot.
            task_id: Identifier of the task being assigned.
            role: Role of the agent about to be spawned.

        Returns:
            List of results from each hook invocation.
        """
        return self._run_hooks(
            "pre_spawn",
            context=context,
            task_id=task_id,
            role=role,
        )

    def run_post_spawn(
        self,
        context: TickContext,
        task_id: str,
        agent_id: str,
    ) -> list[TickHookResult]:
        """Execute all registered ``post_spawn`` hooks.

        Args:
            context: Current tick state snapshot.
            task_id: Identifier of the task that was assigned.
            agent_id: Identifier of the newly spawned agent session.

        Returns:
            List of results from each hook invocation.
        """
        return self._run_hooks(
            "post_spawn",
            context=context,
            task_id=task_id,
            agent_id=agent_id,
        )

    def _run_hooks(
        self,
        phase: str,
        **kwargs: object,
    ) -> list[TickHookResult]:
        """Invoke all registered hooks for *phase*, collecting results.

        Exceptions from individual hooks are caught and converted into
        failed :class:`TickHookResult` entries so that one misbehaving
        plugin cannot crash the orchestrator.

        Args:
            phase: Hook phase name (must match a method on TickHookSpec).
            **kwargs: Keyword arguments forwarded to the hook.

        Returns:
            List of collected results.
        """
        hook_caller = getattr(self._pm.hook, phase)
        results: list[TickHookResult] = []

        # pluggy returns a list of results from all implementations.
        # We time the entire batch and handle errors per-result.
        t0 = time.monotonic()
        try:
            raw_results: list[TickHookResult | None] = hook_caller(**kwargs)
        except Exception:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            log.exception("Tick hook phase %s raised an unexpected error", phase)
            results.append(
                TickHookResult(
                    hook_name="<unknown>",
                    phase=phase,  # type: ignore[arg-type]
                    success=False,
                    duration_ms=elapsed_ms,
                    message="Hook phase raised an unexpected error",
                )
            )
            return results

        elapsed_ms = (time.monotonic() - t0) * 1000.0

        for raw in raw_results:
            if raw is not None:
                results.append(raw)
            # None results (hooks that returned nothing) are silently skipped.

        if not results and raw_results:
            # All hooks returned None — still record elapsed time for tracing.
            log.debug(
                "Tick hook phase %s: %d hook(s) returned None in %.1fms",
                phase,
                len(raw_results),
                elapsed_ms,
            )

        return results
