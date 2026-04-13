"""Heartbeat timeout escalation ladder.

Implements a 4-tier escalation for unresponsive agents:

1. **WARN** (default 60s) — log a warning; no action taken.
2. **SIGUSR1** (default 90s) — send SIGUSR1 to nudge the agent.
3. **SIGTERM** (default 120s) — graceful termination request.
4. **SIGKILL** (default 150s) — force-kill the process.

Each tier is triggered when the heartbeat age exceeds the configured
threshold.  Thresholds are configurable per-agent or globally.  The
escalation is stateful: once an agent has been escalated to a tier, it
will not be re-escalated to the same tier.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from enum import IntEnum

from bernstein.core.defaults import AGENT

logger = logging.getLogger(__name__)


class EscalationTier(IntEnum):
    """Escalation tiers ordered by severity."""

    NONE = 0
    WARN = 1
    SIGUSR1 = 2
    SIGTERM = 3
    SIGKILL = 4


@dataclass(frozen=True)
class EscalationThresholds:
    """Configurable thresholds for each escalation tier.

    All values are in seconds. A value of 0 disables that tier.

    Attributes:
        warn_s: Seconds of heartbeat silence before logging a warning.
        sigusr1_s: Seconds before sending SIGUSR1.
        sigterm_s: Seconds before sending SIGTERM.
        sigkill_s: Seconds before sending SIGKILL.
    """

    warn_s: float = AGENT.escalation_warn_s
    sigusr1_s: float = AGENT.escalation_sigusr1_s
    sigterm_s: float = AGENT.escalation_sigterm_s
    sigkill_s: float = AGENT.escalation_sigkill_s

    def validate(self) -> list[str]:
        """Validate that thresholds are monotonically increasing.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors: list[str] = []
        active = [
            (name, val)
            for name, val in [
                ("warn_s", self.warn_s),
                ("sigusr1_s", self.sigusr1_s),
                ("sigterm_s", self.sigterm_s),
                ("sigkill_s", self.sigkill_s),
            ]
            if val > 0
        ]

        for i in range(1, len(active)):
            if active[i][1] <= active[i - 1][1]:
                errors.append(
                    f"{active[i][0]} ({active[i][1]}s) must be greater than {active[i - 1][0]} ({active[i - 1][1]}s)"
                )
        return errors


@dataclass
class AgentEscalationState:
    """Per-agent escalation tracking state.

    Attributes:
        session_id: Agent session ID.
        highest_tier: Highest escalation tier reached so far.
        tier_timestamps: Monotonic timestamp when each tier was triggered.
        pid: Process ID of the agent (needed for signal delivery).
    """

    session_id: str
    highest_tier: EscalationTier = EscalationTier.NONE
    tier_timestamps: dict[EscalationTier, float] = field(default_factory=dict)
    pid: int | None = None


@dataclass(frozen=True)
class EscalationAction:
    """An escalation action that was triggered or would be triggered.

    Attributes:
        session_id: Agent session ID.
        tier: The escalation tier.
        heartbeat_age_s: Age of the last heartbeat in seconds.
        action_taken: Whether the action was actually executed.
        detail: Human-readable description of what happened.
    """

    session_id: str
    tier: EscalationTier
    heartbeat_age_s: float
    action_taken: bool
    detail: str


class HeartbeatEscalationLadder:
    """Manages heartbeat timeout escalation for all agents.

    Args:
        thresholds: Global thresholds (can be overridden per-agent).
    """

    def __init__(self, thresholds: EscalationThresholds | None = None) -> None:
        self._thresholds = thresholds or EscalationThresholds()
        self._agents: dict[str, AgentEscalationState] = {}
        # Per-agent threshold overrides
        self._agent_thresholds: dict[str, EscalationThresholds] = {}

    @property
    def thresholds(self) -> EscalationThresholds:
        """Global escalation thresholds."""
        return self._thresholds

    def register_agent(
        self,
        session_id: str,
        pid: int | None = None,
        thresholds: EscalationThresholds | None = None,
    ) -> None:
        """Register an agent for escalation tracking.

        Args:
            session_id: Agent session ID.
            pid: Process ID (needed for signal delivery).
            thresholds: Optional per-agent threshold overrides.
        """
        self._agents[session_id] = AgentEscalationState(
            session_id=session_id,
            pid=pid,
        )
        if thresholds is not None:
            self._agent_thresholds[session_id] = thresholds

    def unregister_agent(self, session_id: str) -> None:
        """Remove an agent from escalation tracking.

        Args:
            session_id: Agent session ID to remove.
        """
        self._agents.pop(session_id, None)
        self._agent_thresholds.pop(session_id, None)

    def check_and_escalate(
        self,
        session_id: str,
        heartbeat_age_s: float,
        pid: int | None = None,
    ) -> EscalationAction | None:
        """Check an agent's heartbeat age and escalate if needed.

        This method is idempotent per tier: once an agent has been
        escalated to SIGTERM, calling this again with the same age
        will not re-send SIGTERM.

        Args:
            session_id: Agent session ID.
            heartbeat_age_s: Seconds since the last heartbeat.
            pid: Process ID override (updates stored PID if provided).

        Returns:
            The EscalationAction taken, or None if no escalation needed.
        """
        state = self._agents.get(session_id)
        if state is None:
            # Auto-register if not previously registered
            self.register_agent(session_id, pid=pid)
            state = self._agents[session_id]

        if pid is not None:
            state.pid = pid

        thresholds = self._agent_thresholds.get(session_id, self._thresholds)
        target_tier = self._compute_target_tier(heartbeat_age_s, thresholds)

        if target_tier <= state.highest_tier:
            return None  # Already at or past this tier

        # Escalate to the new tier
        state.highest_tier = target_tier
        state.tier_timestamps[target_tier] = time.monotonic()

        action_taken = self._execute_escalation(state, target_tier, heartbeat_age_s)

        return EscalationAction(
            session_id=session_id,
            tier=target_tier,
            heartbeat_age_s=heartbeat_age_s,
            action_taken=action_taken,
            detail=self._tier_detail(target_tier, heartbeat_age_s, action_taken),
        )

    def reset_agent(self, session_id: str) -> None:
        """Reset an agent's escalation state (e.g. after receiving a heartbeat).

        Args:
            session_id: Agent session ID.
        """
        state = self._agents.get(session_id)
        if state is not None:
            state.highest_tier = EscalationTier.NONE
            state.tier_timestamps.clear()

    def get_state(self, session_id: str) -> AgentEscalationState | None:
        """Get the current escalation state for an agent.

        Args:
            session_id: Agent session ID.

        Returns:
            The escalation state, or None if not tracked.
        """
        return self._agents.get(session_id)

    def _compute_target_tier(
        self,
        heartbeat_age_s: float,
        thresholds: EscalationThresholds,
    ) -> EscalationTier:
        """Determine the appropriate escalation tier for a heartbeat age.

        Args:
            heartbeat_age_s: Seconds since the last heartbeat.
            thresholds: Thresholds to check against.

        Returns:
            The highest applicable tier.
        """
        tier = EscalationTier.NONE

        if thresholds.sigkill_s > 0 and heartbeat_age_s >= thresholds.sigkill_s:
            tier = EscalationTier.SIGKILL
        elif thresholds.sigterm_s > 0 and heartbeat_age_s >= thresholds.sigterm_s:
            tier = EscalationTier.SIGTERM
        elif thresholds.sigusr1_s > 0 and heartbeat_age_s >= thresholds.sigusr1_s:
            tier = EscalationTier.SIGUSR1
        elif thresholds.warn_s > 0 and heartbeat_age_s >= thresholds.warn_s:
            tier = EscalationTier.WARN

        return tier

    def _execute_escalation(
        self,
        state: AgentEscalationState,
        tier: EscalationTier,
        heartbeat_age_s: float,
    ) -> bool:
        """Execute the escalation action for a tier.

        Args:
            state: Agent escalation state.
            tier: The tier to execute.
            heartbeat_age_s: Current heartbeat age.

        Returns:
            True if the action was successfully executed.
        """
        if tier == EscalationTier.WARN:
            logger.warning(
                "Heartbeat stale for agent %s: %.0fs (WARN threshold)",
                state.session_id,
                heartbeat_age_s,
            )
            return True

        if tier == EscalationTier.SIGUSR1:
            return self._send_signal(state, signal.SIGUSR1, "SIGUSR1", heartbeat_age_s)

        if tier == EscalationTier.SIGTERM:
            return self._send_signal(state, signal.SIGTERM, "SIGTERM", heartbeat_age_s)

        if tier == EscalationTier.SIGKILL:
            return self._send_signal(state, signal.SIGKILL, "SIGKILL", heartbeat_age_s)

        return False

    def _send_signal(
        self,
        state: AgentEscalationState,
        sig: signal.Signals,
        sig_name: str,
        heartbeat_age_s: float,
    ) -> bool:
        """Send a signal to an agent process.

        Args:
            state: Agent escalation state.
            sig: Signal to send.
            sig_name: Human-readable signal name for logging.
            heartbeat_age_s: Current heartbeat age.

        Returns:
            True if the signal was successfully sent.
        """
        if state.pid is None:
            logger.warning(
                "Cannot send %s to agent %s: no PID available (heartbeat age: %.0fs)",
                sig_name,
                state.session_id,
                heartbeat_age_s,
            )
            return False

        try:
            from bernstein.core.platform_compat import kill_process_group

            kill_process_group(state.pid, sig)
            logger.warning(
                "Sent %s to agent %s (PID %d): heartbeat stale for %.0fs",
                sig_name,
                state.session_id,
                state.pid,
                heartbeat_age_s,
            )
            return True
        except ProcessLookupError:
            logger.info(
                "Agent %s (PID %d) already exited when sending %s",
                state.session_id,
                state.pid,
                sig_name,
            )
            return False
        except PermissionError:
            logger.error(
                "Permission denied sending %s to agent %s (PID %d)",
                sig_name,
                state.session_id,
                state.pid,
            )
            return False

    def _tier_detail(
        self,
        tier: EscalationTier,
        heartbeat_age_s: float,
        action_taken: bool,
    ) -> str:
        """Build a human-readable detail string for an escalation action.

        Args:
            tier: The escalation tier.
            heartbeat_age_s: Current heartbeat age.
            action_taken: Whether the action was executed.

        Returns:
            Detail string.
        """
        status = "executed" if action_taken else "failed"
        return f"{tier.name} escalation ({status}) at {heartbeat_age_s:.0f}s heartbeat age"
