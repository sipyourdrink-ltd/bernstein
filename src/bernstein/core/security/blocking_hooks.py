"""Blocking hook support for pre-action events.

Pre-action events (``pre_merge``, ``pre_spawn``, ``pre_approve``) can
return allow/deny decisions.  A 5-second timeout is enforced -- if a
blocking hook does not respond in time the action is denied by default.

Usage::

    from bernstein.core.blocking_hooks import BlockingHookRunner

    runner = BlockingHookRunner()
    runner.register("pre_merge", my_hook_fn)
    result = runner.run("pre_merge", payload)
    if result.allowed:
        do_merge()
    else:
        log.warning("Merge blocked: %s", result.reason)
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Protocol

from bernstein.core.hook_events import BLOCKING_EVENTS, BlockingHookPayload, HookEvent

logger = logging.getLogger(__name__)

# Default timeout for blocking hooks (seconds).
BLOCKING_HOOK_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockingHookResult:
    """Outcome of a blocking hook evaluation.

    Attributes:
        allowed: Whether the action is permitted.
        reason: Human-readable explanation when denied.
        hook_name: Which hook produced this result.
        duration_s: Wall-clock seconds the hook took to evaluate.
    """

    allowed: bool
    reason: str = ""
    hook_name: str = ""
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Hook callable protocol
# ---------------------------------------------------------------------------


class BlockingHookFn(Protocol):
    """Callable signature for blocking hooks.

    Must return a ``BlockingHookResult``.
    """

    def __call__(self, payload: BlockingHookPayload) -> BlockingHookResult: ...


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BlockingHookRunner:
    """Execute blocking hooks with timeout enforcement.

    Multiple hooks can be registered per event.  If *any* hook denies,
    the action is blocked.  Hooks are run sequentially in registration
    order.

    Args:
        timeout_s: Maximum seconds a single hook may take before being
            treated as a deny.
    """

    def __init__(self, timeout_s: float = BLOCKING_HOOK_TIMEOUT_S) -> None:
        self._hooks: dict[str, list[BlockingHookFn]] = {}
        self._timeout_s = timeout_s
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="BlockingHook",
        )

    @property
    def timeout_s(self) -> float:
        """Return the configured timeout in seconds."""
        return self._timeout_s

    def register(self, event_name: str, hook_fn: BlockingHookFn) -> None:
        """Register a blocking hook for an event.

        Args:
            event_name: The event name (e.g. ``"pre_merge"``).
            hook_fn: Callable that evaluates the payload and returns a result.
        """
        self._hooks.setdefault(event_name, []).append(hook_fn)

    def registered_events(self) -> list[str]:
        """Return all event names that have at least one registered hook."""
        return [k for k, v in self._hooks.items() if v]

    def run(
        self,
        event_name: str,
        payload: BlockingHookPayload,
    ) -> BlockingHookResult:
        """Run all blocking hooks for *event_name* and aggregate results.

        The first hook to deny stops evaluation (short-circuit).
        If no hooks are registered, the action is allowed by default.

        Args:
            event_name: The blocking event name.
            payload: The payload describing the action to be gated.

        Returns:
            Aggregated result.  ``allowed=True`` only if every hook allows.
        """
        hooks = self._hooks.get(event_name, [])
        if not hooks:
            return BlockingHookResult(allowed=True, hook_name="(none)")

        total_duration = 0.0
        for hook_fn in hooks:
            result = self._run_single(event_name, hook_fn, payload)
            total_duration += result.duration_s
            if not result.allowed:
                logger.info(
                    "Blocking hook denied %s: %s (took %.3fs)",
                    event_name,
                    result.reason,
                    result.duration_s,
                )
                return result

        return BlockingHookResult(
            allowed=True,
            hook_name="(all)",
            duration_s=total_duration,
        )

    def _run_single(
        self,
        event_name: str,
        hook_fn: BlockingHookFn,
        payload: BlockingHookPayload,
    ) -> BlockingHookResult:
        """Execute a single blocking hook with timeout enforcement.

        Args:
            event_name: For logging/diagnostics.
            hook_fn: The hook to call.
            payload: Payload forwarded to the hook.

        Returns:
            The hook result, or a deny result on timeout/error.
        """
        hook_name = getattr(hook_fn, "__name__", repr(hook_fn))
        start = time.monotonic()

        future: Future[BlockingHookResult] = self._executor.submit(hook_fn, payload)
        try:
            result = future.result(timeout=self._timeout_s)
            elapsed = time.monotonic() - start
            return BlockingHookResult(
                allowed=result.allowed,
                reason=result.reason,
                hook_name=hook_name,
                duration_s=elapsed,
            )
        except FutureTimeoutError:
            future.cancel()
            elapsed = time.monotonic() - start
            logger.warning(
                "Blocking hook %r timed out after %.1fs for event %s -- denying",
                hook_name,
                elapsed,
                event_name,
            )
            return BlockingHookResult(
                allowed=False,
                reason=f"Hook {hook_name!r} timed out after {self._timeout_s}s",
                hook_name=hook_name,
                duration_s=elapsed,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "Blocking hook %r raised %s for event %s -- denying",
                hook_name,
                exc,
                event_name,
            )
            return BlockingHookResult(
                allowed=False,
                reason=f"Hook {hook_name!r} raised: {exc}",
                hook_name=hook_name,
                duration_s=elapsed,
            )

    def shutdown(self) -> None:
        """Shut down the thread pool.  Safe to call multiple times."""
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def validate_blocking_event(event: HookEvent) -> None:
    """Raise ``ValueError`` if *event* is not a blocking event.

    Args:
        event: The hook event to check.

    Raises:
        ValueError: If the event is not in ``BLOCKING_EVENTS``.
    """
    if event not in BLOCKING_EVENTS:
        msg = f"{event.value!r} is not a blocking event; valid: {[e.value for e in BLOCKING_EVENTS]}"
        raise ValueError(msg)


def make_blocking_payload(
    event: HookEvent,
    action: str,
    context: dict[str, Any] | None = None,
) -> BlockingHookPayload:
    """Build a ``BlockingHookPayload`` for the given event.

    Args:
        event: Must be one of the ``BLOCKING_EVENTS``.
        action: Human-readable label for the action being gated.
        context: Optional extra context dict.

    Returns:
        A fully-populated payload.

    Raises:
        ValueError: If *event* is not a blocking event.
    """
    validate_blocking_event(event)
    return BlockingHookPayload(
        event=event,
        action=action,
        context=context or {},
    )
