"""Heartbeat protocol v2 with bi-directional health (road-055).

Implements a ping/pong heartbeat protocol between the orchestrator and
agents.  The orchestrator sends typed pings; agents reply with pongs
that include status, progress, context usage, and ETA.  The manager
tracks per-agent health and detects degraded or unresponsive agents.

Usage::

    mgr = HeartbeatV2Manager(timeout_s=10)
    ping = mgr.create_ping(PingType.STATUS_REQUEST, "agent-1")
    # ... send ping to agent, receive pong ...
    mgr.record_pong(AgentPong(
        ping_id=ping.ping_id,
        agent_id="agent-1",
        status="running",
        progress_pct=42.0,
        context_tokens=12000,
        estimated_remaining_s=30.0,
        timestamp=time.time(),
    ))
    health = mgr.get_health("agent-1")
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ping / Pong types
# ---------------------------------------------------------------------------


class PingType(Enum):
    """Type of information requested in a ping."""

    STATUS_REQUEST = "status_request"
    PROGRESS_REQUEST = "progress_request"
    CONTEXT_SIZE_REQUEST = "context_size_request"
    ETA_REQUEST = "eta_request"


@dataclass(frozen=True)
class OrchestratorPing:
    """An outbound health-check ping from the orchestrator.

    Attributes:
        ping_id: Unique identifier for this ping.
        ping_type: The kind of information requested.
        agent_id: Target agent identifier.
        timestamp: UNIX timestamp when the ping was created.
        timeout_s: Seconds the agent has to respond before the ping
            is considered timed-out.
    """

    ping_id: str
    ping_type: PingType
    agent_id: str
    timestamp: float
    timeout_s: float = 10.0


@dataclass(frozen=True)
class AgentPong:
    """A response from an agent to an orchestrator ping.

    Attributes:
        ping_id: The ping this pong is answering.
        agent_id: Agent that sent the pong.
        status: Free-form status string (e.g. ``"running"``).
        progress_pct: Task progress as a percentage (0-100).
        context_tokens: Number of context tokens currently used.
        estimated_remaining_s: Estimated seconds until task completion.
        timestamp: UNIX timestamp when the pong was sent.
    """

    ping_id: str
    agent_id: str
    status: str
    progress_pct: float
    context_tokens: int
    estimated_remaining_s: float
    timestamp: float


# ---------------------------------------------------------------------------
# Health state
# ---------------------------------------------------------------------------


class HealthState(Enum):
    """Observed health of an agent."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNRESPONSIVE = "unresponsive"


@dataclass(frozen=True)
class AgentHealth:
    """Current health snapshot for a single agent.

    Attributes:
        agent_id: Agent identifier.
        state: Derived health state.
        last_ping_at: Timestamp of the most recent ping sent.
        last_pong_at: Timestamp of the most recent pong received,
            or ``None`` if no pong has been received yet.
        response_time_ms: Round-trip time of the most recent pong in
            milliseconds, or ``None`` if unavailable.
        consecutive_failures: Number of consecutive ping timeouts.
    """

    agent_id: str
    state: HealthState
    last_ping_at: float
    last_pong_at: float | None
    response_time_ms: float | None
    consecutive_failures: int


# ---------------------------------------------------------------------------
# Internal mutable tracking record
# ---------------------------------------------------------------------------


@dataclass
class _AgentRecord:
    """Mutable internal bookkeeping for one agent."""

    agent_id: str
    last_ping_at: float = 0.0
    last_pong_at: float | None = None
    response_time_ms: float | None = None
    consecutive_failures: int = 0
    pending_pings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class HeartbeatV2Manager:
    """Manages bi-directional heartbeats between orchestrator and agents.

    Not thread-safe -- designed for single-threaded async orchestrator loop.

    Args:
        timeout_s: Default ping timeout in seconds.
        degraded_threshold: Number of consecutive failures before an agent
            is considered degraded.
        dead_threshold: Number of consecutive failures before an agent
            is considered unresponsive.
    """

    def __init__(
        self,
        timeout_s: float = 10.0,
        degraded_threshold: int = 2,
        dead_threshold: int = 5,
    ) -> None:
        self._timeout_s = timeout_s
        self._degraded_threshold = degraded_threshold
        self._dead_threshold = dead_threshold
        self._records: dict[str, _AgentRecord] = {}

    # -- helpers -------------------------------------------------------------

    def _get_record(self, agent_id: str) -> _AgentRecord:
        """Get or create the internal record for *agent_id*."""
        if agent_id not in self._records:
            self._records[agent_id] = _AgentRecord(agent_id=agent_id)
        return self._records[agent_id]

    def _derive_state(self, record: _AgentRecord) -> HealthState:
        """Derive :class:`HealthState` from consecutive failure count."""
        if record.consecutive_failures >= self._dead_threshold:
            return HealthState.UNRESPONSIVE
        if record.consecutive_failures >= self._degraded_threshold:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    # -- public API ----------------------------------------------------------

    def create_ping(
        self,
        ping_type: PingType,
        agent_id: str,
        *,
        timeout_s: float | None = None,
    ) -> OrchestratorPing:
        """Create and register a new ping for *agent_id*.

        Args:
            ping_type: Kind of information being requested.
            agent_id: Target agent.
            timeout_s: Override the default timeout for this ping.

        Returns:
            A frozen :class:`OrchestratorPing` ready to be sent.
        """
        now = time.time()
        effective_timeout = timeout_s if timeout_s is not None else self._timeout_s
        ping = OrchestratorPing(
            ping_id=uuid.uuid4().hex,
            ping_type=ping_type,
            agent_id=agent_id,
            timestamp=now,
            timeout_s=effective_timeout,
        )
        record = self._get_record(agent_id)
        record.last_ping_at = now
        record.pending_pings[ping.ping_id] = now
        logger.debug(
            "Ping %s (%s) -> agent %s (timeout=%.1fs)",
            ping.ping_id,
            ping_type.value,
            agent_id,
            effective_timeout,
        )
        return ping

    def record_pong(self, pong: AgentPong) -> None:
        """Record an inbound pong from an agent.

        Clears the matching pending ping and resets the consecutive
        failure counter.

        Args:
            pong: The pong received from the agent.
        """
        record = self._get_record(pong.agent_id)
        ping_ts = record.pending_pings.pop(pong.ping_id, None)
        if ping_ts is not None:
            rtt_ms = (pong.timestamp - ping_ts) * 1000
            record.response_time_ms = rtt_ms
        record.last_pong_at = pong.timestamp
        record.consecutive_failures = 0
        logger.debug(
            "Pong from agent %s (ping=%s, rtt=%.1fms, progress=%.1f%%)",
            pong.agent_id,
            pong.ping_id,
            record.response_time_ms or 0.0,
            pong.progress_pct,
        )

    def record_timeout(self, agent_id: str, ping_id: str) -> None:
        """Record that a ping timed out without a pong.

        Increments the consecutive failure counter for the agent.

        Args:
            agent_id: Agent that failed to respond.
            ping_id: The ping that timed out.
        """
        record = self._get_record(agent_id)
        record.pending_pings.pop(ping_id, None)
        record.consecutive_failures += 1
        state = self._derive_state(record)
        logger.warning(
            "Ping %s timed out for agent %s (failures=%d, state=%s)",
            ping_id,
            agent_id,
            record.consecutive_failures,
            state.value,
        )

    def get_health(self, agent_id: str) -> AgentHealth:
        """Return a health snapshot for *agent_id*.

        If no record exists yet, the agent is assumed ``HEALTHY`` with
        zero failures.

        Args:
            agent_id: Agent to query.

        Returns:
            Frozen :class:`AgentHealth` snapshot.
        """
        record = self._get_record(agent_id)
        return AgentHealth(
            agent_id=agent_id,
            state=self._derive_state(record),
            last_ping_at=record.last_ping_at,
            last_pong_at=record.last_pong_at,
            response_time_ms=record.response_time_ms,
            consecutive_failures=record.consecutive_failures,
        )

    def get_all_health(self) -> dict[str, AgentHealth]:
        """Return health snapshots for all tracked agents.

        Returns:
            Dict mapping agent id to :class:`AgentHealth`.
        """
        return {aid: self.get_health(aid) for aid in self._records}

    def get_degraded_agents(self) -> list[AgentHealth]:
        """Return agents that are degraded or unresponsive.

        Returns:
            List of :class:`AgentHealth` for agents not in the
            ``HEALTHY`` state.
        """
        return [health for health in self.get_all_health().values() if health.state is not HealthState.HEALTHY]
