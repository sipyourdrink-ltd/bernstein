"""Graduated access control with trust-based permission expansion.

New agents start with minimal privileges (UNTRUSTED) and earn broader
access as they complete tasks without security violations.  Trust levels
gate file-write access, network access, per-task file limits, and
directory scoping.

Promotion thresholds:
- UNTRUSTED -> PROBATIONARY: 1 successful task, 0 violations
- PROBATIONARY -> TRUSTED: 3 successful tasks, 0 violations
- TRUSTED -> ELEVATED: 10 successful tasks, 0 violations

A single security violation at any level triggers an automatic demotion.
Manual promote/demote overrides are also supported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trust levels
# ---------------------------------------------------------------------------


class TrustLevel(IntEnum):
    """Agent trust level, lowest to highest privilege.

    Attributes:
        UNTRUSTED: Brand-new agent, no track record.
        PROBATIONARY: Completed at least one task successfully.
        TRUSTED: Consistent success record with no violations.
        ELEVATED: Long track record of clean execution.
    """

    UNTRUSTED = 0
    PROBATIONARY = 1
    TRUSTED = 2
    ELEVATED = 3


# ---------------------------------------------------------------------------
# Agent trust record
# ---------------------------------------------------------------------------


@dataclass
class AgentTrustRecord:
    """Mutable record tracking an agent's trust history.

    Attributes:
        agent_id: Unique identifier for the agent.
        trust_level: Current trust level.
        successful_tasks: Cumulative count of successfully completed tasks.
        failed_tasks: Cumulative count of failed tasks.
        security_violations: Cumulative count of security violations.
        first_seen: Unix timestamp when the agent was first observed.
        last_seen: Unix timestamp of the most recent activity.
    """

    agent_id: str
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    successful_tasks: int = 0
    failed_tasks: int = 0
    security_violations: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Access policy (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessPolicy:
    """Immutable policy describing what an agent at a given trust level may do.

    Attributes:
        trust_level: The trust level this policy applies to.
        can_write_files: Whether the agent may create or modify files.
        can_access_network: Whether the agent may make network requests.
        max_files_per_task: Maximum number of files the agent may touch
            in a single task.
        allowed_directories: Tuple of directory path prefixes the agent
            is confined to.  An empty tuple means unrestricted.
    """

    trust_level: TrustLevel
    can_write_files: bool
    can_access_network: bool
    max_files_per_task: int
    allowed_directories: tuple[str, ...]


# ---------------------------------------------------------------------------
# Default policies per trust level
# ---------------------------------------------------------------------------

_DEFAULT_POLICIES: dict[TrustLevel, AccessPolicy] = {
    TrustLevel.UNTRUSTED: AccessPolicy(
        trust_level=TrustLevel.UNTRUSTED,
        can_write_files=False,
        can_access_network=False,
        max_files_per_task=0,
        allowed_directories=("docs/",),
    ),
    TrustLevel.PROBATIONARY: AccessPolicy(
        trust_level=TrustLevel.PROBATIONARY,
        can_write_files=True,
        can_access_network=False,
        max_files_per_task=5,
        allowed_directories=("src/", "tests/", "docs/"),
    ),
    TrustLevel.TRUSTED: AccessPolicy(
        trust_level=TrustLevel.TRUSTED,
        can_write_files=True,
        can_access_network=True,
        max_files_per_task=20,
        allowed_directories=("src/", "tests/", "docs/", "scripts/"),
    ),
    TrustLevel.ELEVATED: AccessPolicy(
        trust_level=TrustLevel.ELEVATED,
        can_write_files=True,
        can_access_network=True,
        max_files_per_task=100,
        allowed_directories=(),
    ),
}

# ---------------------------------------------------------------------------
# Promotion thresholds
# ---------------------------------------------------------------------------

_PROMOTION_THRESHOLDS: dict[TrustLevel, tuple[int, int]] = {
    # target_level: (min_successes, max_violations)
    TrustLevel.PROBATIONARY: (1, 0),
    TrustLevel.TRUSTED: (3, 0),
    TrustLevel.ELEVATED: (10, 0),
}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GraduatedAccessManager:
    """Manage agent trust levels and access policies.

    Tracks per-agent trust records, applies automatic promotion/demotion
    rules, and returns the appropriate ``AccessPolicy`` for each agent.

    Args:
        policies: Optional override map of trust level to ``AccessPolicy``.
            When not provided the built-in defaults are used.
    """

    def __init__(
        self,
        policies: dict[TrustLevel, AccessPolicy] | None = None,
    ) -> None:
        self._records: dict[str, AgentTrustRecord] = {}
        self._policies = policies if policies is not None else dict(_DEFAULT_POLICIES)

    # -- record helpers -----------------------------------------------------

    def _get_or_create_record(self, agent_id: str) -> AgentTrustRecord:
        """Return the trust record for *agent_id*, creating one if needed.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The existing or newly created ``AgentTrustRecord``.
        """
        if agent_id not in self._records:
            now = time.time()
            self._records[agent_id] = AgentTrustRecord(
                agent_id=agent_id,
                first_seen=now,
                last_seen=now,
            )
            logger.info("New agent registered: %s (UNTRUSTED)", agent_id)
        return self._records[agent_id]

    # -- public API ---------------------------------------------------------

    def get_record(self, agent_id: str) -> AgentTrustRecord:
        """Return the trust record for an agent.

        Creates a new UNTRUSTED record if the agent has not been seen before.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The agent's ``AgentTrustRecord``.
        """
        return self._get_or_create_record(agent_id)

    def get_trust_level(self, agent_id: str) -> TrustLevel:
        """Return the current trust level for an agent.

        Creates a new UNTRUSTED record if the agent has not been seen before.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The agent's current ``TrustLevel``.
        """
        return self._get_or_create_record(agent_id).trust_level

    def record_outcome(
        self,
        agent_id: str,
        *,
        success: bool,
        security_violation: bool = False,
    ) -> TrustLevel:
        """Record a task outcome and apply automatic trust adjustments.

        A successful task increments the success counter and may trigger
        a promotion.  A security violation triggers an immediate demotion.
        A simple failure (no violation) increments the failure counter but
        does not change the trust level.

        Args:
            agent_id: Unique agent identifier.
            success: Whether the task completed successfully.
            security_violation: Whether a security violation occurred.

        Returns:
            The agent's trust level after applying any adjustments.
        """
        record = self._get_or_create_record(agent_id)
        record.last_seen = time.time()

        if security_violation:
            record.security_violations += 1
            self._auto_demote(record)
            return record.trust_level

        if success:
            record.successful_tasks += 1
            if self.should_promote(record):
                self._auto_promote(record)
        else:
            record.failed_tasks += 1

        return record.trust_level

    def get_policy(self, agent_id: str) -> AccessPolicy:
        """Return the access policy for an agent based on its trust level.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The ``AccessPolicy`` corresponding to the agent's trust level.
        """
        level = self.get_trust_level(agent_id)
        return self._policies[level]

    def promote(self, agent_id: str) -> TrustLevel:
        """Manually promote an agent by one trust level.

        No-op if the agent is already at ELEVATED.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The agent's trust level after promotion.
        """
        record = self._get_or_create_record(agent_id)
        if record.trust_level < TrustLevel.ELEVATED:
            old = record.trust_level
            record.trust_level = TrustLevel(record.trust_level + 1)
            record.last_seen = time.time()
            logger.info(
                "Agent %s manually promoted: %s -> %s",
                agent_id,
                old.name,
                record.trust_level.name,
            )
        return record.trust_level

    def demote(self, agent_id: str) -> TrustLevel:
        """Manually demote an agent by one trust level.

        No-op if the agent is already at UNTRUSTED.

        Args:
            agent_id: Unique agent identifier.

        Returns:
            The agent's trust level after demotion.
        """
        record = self._get_or_create_record(agent_id)
        if record.trust_level > TrustLevel.UNTRUSTED:
            old = record.trust_level
            record.trust_level = TrustLevel(record.trust_level - 1)
            record.last_seen = time.time()
            logger.info(
                "Agent %s manually demoted: %s -> %s",
                agent_id,
                old.name,
                record.trust_level.name,
            )
        return record.trust_level

    def should_promote(self, record: AgentTrustRecord) -> bool:
        """Check whether an agent qualifies for automatic promotion.

        Promotion requires meeting the success threshold for the *next*
        trust level while having zero security violations.

        Args:
            record: The agent's trust record to evaluate.

        Returns:
            True if the agent meets the criteria for the next trust level.
        """
        if record.trust_level >= TrustLevel.ELEVATED:
            return False

        next_level = TrustLevel(record.trust_level + 1)
        threshold = _PROMOTION_THRESHOLDS.get(next_level)
        if threshold is None:
            return False

        min_successes, max_violations = threshold
        return record.successful_tasks >= min_successes and record.security_violations <= max_violations

    # -- internals ----------------------------------------------------------

    def _auto_promote(self, record: AgentTrustRecord) -> None:
        """Promote an agent to the next trust level.

        Args:
            record: The agent's trust record.
        """
        if record.trust_level >= TrustLevel.ELEVATED:
            return
        old = record.trust_level
        record.trust_level = TrustLevel(record.trust_level + 1)
        logger.info(
            "Agent %s auto-promoted: %s -> %s",
            record.agent_id,
            old.name,
            record.trust_level.name,
        )

    def _auto_demote(self, record: AgentTrustRecord) -> None:
        """Demote an agent by one level on security violation.

        Args:
            record: The agent's trust record.
        """
        if record.trust_level <= TrustLevel.UNTRUSTED:
            return
        old = record.trust_level
        record.trust_level = TrustLevel(record.trust_level - 1)
        logger.warning(
            "Agent %s auto-demoted on violation: %s -> %s",
            record.agent_id,
            old.name,
            record.trust_level.name,
        )
