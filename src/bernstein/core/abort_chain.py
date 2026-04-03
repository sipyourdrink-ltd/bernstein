"""Hierarchical abort chain for agent tool execution (T442).

Implements a three-level abort system:
    1. **SESSION** — tears down the entire agent session.
    2. **SIBLING** — aborts other tools executing concurrently with the
       triggered tool, but allows the session to continue.
    3. **TOOL** — aborts only the current tool invocation, leaving the
       agent process and sibling tools unaffected.

Each level can propagate or contain failures independently so operators
can stop work at the right granularity without leaving orphaned tools or
inconsistent agent state.
"""

from __future__ import annotations

import logging
import os
import signal as _signal
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AbortLevel(Enum):
    """Scope of an abort operation (T442).

    Attributes:
        TOOL: Abort only the current tool invocation. The agent continues.
        SIBLING: Abort all tools running concurrently with the triggered tool,
            but let the agent session continue.
        SESSION: Tear down the entire agent session, including all tools.
    """

    TOOL = "tool"
    SIBLING = "sibling"
    SESSION = "session"


class AbortPropagation(Enum):
    """Whether an abort at one level propagates to higher levels (T442).

    Attributes:
        CONTAIN: The abort stays at the current level.  No propagation.
        PROPAGATE: The abort escalates to the next higher level.
    """

    CONTAIN = "contain"
    PROPAGATE = "propagate"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AbortSignal:
    """Record of an abort event in the chain.

    Attributes:
        level: Abort scope (TOOL, SIBLING, or SESSION).
        reason: Human-readable reason or error summary.
        detail: Optional long-form detail string.
        tool_name: Name of the tool that triggered the abort (if any).
        propagated: True if this abort was escalated to a higher level.
    """

    level: AbortLevel
    reason: str
    detail: str = ""
    tool_name: str | None = None
    propagated: bool = False

    def escalate(self) -> AbortSignal:
        """Return an escalated version of this signal at the next level.

        TOOL → SIBLING, SIBLING → SESSION, SESSION → unchanged.

        Returns:
            New AbortSignal with escalated level and propagated flag set.
        """
        next_level = {
            AbortLevel.TOOL: AbortLevel.SIBLING,
            AbortLevel.SIBLING: AbortLevel.SESSION,
            AbortLevel.SESSION: AbortLevel.SESSION,
        }
        escalated = next_level[self.level]
        if escalated == self.level:
            return self
        return AbortSignal(
            level=escalated,
            reason=f"escalated from {self.level.value}: {self.reason}",
            detail=self.detail,
            tool_name=self.tool_name,
            propagated=True,
        )


@dataclass(frozen=True)
class AbortChainPolicy:
    """Configurable policy for abort propagation (T442).

    Attributes:
        tool_propagation: Whether TOOL-level aborts propagate to SIBLING.
        sibling_propagation: Whether SIBLING-level aborts propagate to SESSION.
        session_aborts_immediately: When True, SESSION abort sends SIGTERM
            followed by SIGKILL after a grace period.
        session_grace_ms: Grace period before SIGKILL after SESSION abort.
    """

    tool_propagation: AbortPropagation = AbortPropagation.CONTAIN
    sibling_propagation: AbortPropagation = AbortPropagation.PROPAGATE
    session_aborts_immediately: bool = True
    session_grace_ms: int = 2_000


# ---------------------------------------------------------------------------
# AbortChain — core state machine
# ---------------------------------------------------------------------------


@dataclass
class AbortChain:
    """Hierarchical abort state for an agent session (T442).

    Thread-safe.  Tracks the current abort state and coordinates shutdown
    events across tool, sibling, and session levels.

    Usage::

        chain = AbortChain()

        # In tool execution wrapper:
        if chain.should_abort(AbortLevel.TOOL):
            handle_tool_abort(chain.pop_signal())
        elif chain.should_abort(AbortLevel.SIBLING):
            handle_sibling_abort(chain.pop_signal())
        elif chain.should_abort(AbortLevel.SESSION):
            handle_session_abort(chain.pop_signal())

        # Trigger an abort from anywhere:
        chain.trigger(AbortLevel.TOOL, "timeout", tool_name="bash")
    """

    policy: AbortChainPolicy = field(default_factory=AbortChainPolicy)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _current_level: AbortLevel | None = field(default=None, init=False, repr=False)
    _signal: AbortSignal | None = field(default=None, init=False, repr=False)
    _shutdown_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def trigger(
        self,
        level: AbortLevel,
        reason: str,
        *,
        tool_name: str | None = None,
        detail: str = "",
    ) -> AbortSignal:
        """Trigger an abort at the given level.

        Applies propagation policy: if the current level's propagation is
        ``PROPAGATE``, the abort escalates to the next level.

        Args:
            level: Abort level to trigger.
            reason: Short reason string.
            tool_name: Name of the tool that triggered the abort.
            detail: Optional long-form detail.

        Returns:
            The final AbortSignal (possibly escalated).
        """
        with self._lock:
            sig = AbortSignal(level=level, reason=reason, detail=detail, tool_name=tool_name)
            log_msg = "Abort triggered: level=%s, reason=%s"
            log_args: list[Any] = [level.value, reason]

            # Apply propagation (loop until no further escalation)
            escalation_map: dict[AbortLevel, AbortPropagation] = {
                AbortLevel.TOOL: self.policy.tool_propagation,
                AbortLevel.SIBLING: self.policy.sibling_propagation,
                AbortLevel.SESSION: AbortPropagation.PROPAGATE,  # SESSION is always terminal
            }

            effective_sig = sig
            while True:
                prop = escalation_map[effective_sig.level]
                if prop == AbortPropagation.PROPAGATE and effective_sig.level != AbortLevel.SESSION:
                    escalated = effective_sig.escalate()
                    log_msg += ", escalated to %s"
                    log_args.append(escalated.level.value)
                    effective_sig = escalated
                else:
                    break

            self._current_level = effective_sig.level
            self._signal = effective_sig

            if effective_sig.level == AbortLevel.SESSION:
                self._shutdown_event.set()

            logger.warning(log_msg, *log_args)
            return effective_sig

    def pop_signal(self) -> AbortSignal | None:
        """Pop and consume the current abort signal (thread-safe).

        Returns:
            AbortSignal if one is pending, None otherwise.
        """
        with self._lock:
            sig = self._signal
            if sig is not None:
                self._signal = None
            return sig

    def should_abort(self, level: AbortLevel) -> bool:
        """Check if the system should abort at the given level.

        Returns True when the current abort state is at *level* or higher
        (SESSION > SIBLING > TOOL).

        Args:
            level: Abort level to check.

        Returns:
            True if an abort at this level or above is pending.
        """
        level_order = {AbortLevel.TOOL: 0, AbortLevel.SIBLING: 1, AbortLevel.SESSION: 2}
        min_order = level_order[level]
        with self._lock:
            if self._current_level is None:
                return False
            return level_order[self._current_level] >= min_order

    def reset(self) -> None:
        """Clear all abort state (for reuse in a new session).

        Warning: this is a hard reset.  Only call when the session has
        fully terminated and no tool processes are running.
        """
        with self._lock:
            self._current_level = None
            self._signal = None
            self._shutdown_event = threading.Event()

    @property
    def is_session_aborted(self) -> bool:
        """Return True if a SESSION-level abort has been triggered."""
        return self._shutdown_event.is_set()

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        """Block until a SESSION-level abort is triggered.

        Args:
            timeout: Maximum seconds to wait (None = wait forever).

        Returns:
            True if shutdown was triggered, False if timed out.
        """
        return self._shutdown_event.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Session abort helper: SIGTERM → grace → SIGKILL
# ---------------------------------------------------------------------------


def abort_session_agent(
    pid: int,
    *,
    grace_ms: int = 2_000,
    reason: str = "session abort",
) -> None:
    """Gracefully terminate an agent session process (T442).

    Sends SIGTERM, waits for the grace period, then sends SIGKILL if the
    process is still alive.

    Args:
        pid: Process ID of the agent session.
        grace_ms: Grace period in milliseconds between SIGTERM and SIGKILL.
        reason: Human-readable reason logged for auditing.
    """

    logger.warning("Aborting agent session PID %d: %s", pid, reason)
    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError as exc:
        logger.debug("SIGTERM to PID %d failed: %s (process may already be gone)", pid, exc)
        return

    deadline = time.monotonic() + grace_ms / 1_000
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            logger.info("Agent session PID %d exited gracefully", pid)
            return
        time.sleep(0.05)

    # Process still alive — SIGKILL
    logger.warning("Agent PID %d did not exit within %d ms; sending SIGKILL", pid, grace_ms)
    try:
        os.kill(pid, _signal.SIGKILL)
    except OSError as exc:
        logger.debug("SIGKILL to PID %d failed: %s", pid, exc)


# ---------------------------------------------------------------------------
# Convenience: create abort chain with common policies
# ---------------------------------------------------------------------------


def default_abort_chain() -> AbortChain:
    """Create an AbortChain with the default policy (tool-contained, sibling-escalates).

    Returns:
        Configured AbortChain instance.
    """
    return AbortChain(
        policy=AbortChainPolicy(
            tool_propagation=AbortPropagation.CONTAIN,
            sibling_propagation=AbortPropagation.PROPAGATE,
            session_grace_ms=2_000,
        )
    )


def strict_abort_chain() -> AbortChain:
    """Create an AbortChain where all levels propagate upward.

    Any tool abort escalates to sibling abort, which escalates to session abort.

    Returns:
        Strictly propagating AbortChain instance.
    """
    return AbortChain(
        policy=AbortChainPolicy(
            tool_propagation=AbortPropagation.PROPAGATE,
            sibling_propagation=AbortPropagation.PROPAGATE,
            session_grace_ms=1_000,
        )
    )
