"""Cooldown period management for agents after failures."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default cooldown period in seconds (5 minutes)
DEFAULT_COOLDOWN_SECONDS = 300


@dataclass
class AgentCooldown:
    """Cooldown state for an agent after failure."""

    agent_id: str
    cooldown_until: float  # Unix timestamp when cooldown expires
    failure_reason: str
    failure_count: int = 1

    @property
    def is_active(self) -> bool:
        """Check if cooldown is still active."""
        return time.time() < self.cooldown_until

    @property
    def remaining_seconds(self) -> float:
        """Get remaining cooldown seconds."""
        return max(0.0, self.cooldown_until - time.time())

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "agent_id": self.agent_id,
            "cooldown_until": self.cooldown_until,
            "failure_reason": self.failure_reason,
            "failure_count": self.failure_count,
            "is_active": self.is_active,
            "remaining_seconds": round(self.remaining_seconds, 1),
        }


class CooldownManager:
    """Manage cooldown periods for agents after failures.

    Args:
        cooldown_seconds: Default cooldown period in seconds.
    """

    def __init__(self, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._cooldowns: dict[str, AgentCooldown] = {}

    def record_failure(
        self,
        agent_id: str,
        failure_reason: str,
        cooldown_seconds: int | None = None,
    ) -> AgentCooldown:
        """Record a failure and set cooldown for an agent.

        Args:
            agent_id: Agent identifier.
            failure_reason: Reason for the failure.
            cooldown_seconds: Optional custom cooldown period.

        Returns:
            AgentCooldown instance.
        """
        cooldown = cooldown_seconds or self._cooldown_seconds
        cooldown_until = time.time() + cooldown

        if agent_id in self._cooldowns:
            # Increment failure count
            existing = self._cooldowns[agent_id]
            cooldown = AgentCooldown(
                agent_id=agent_id,
                cooldown_until=cooldown_until,
                failure_reason=failure_reason,
                failure_count=existing.failure_count + 1,
            )
        else:
            cooldown = AgentCooldown(
                agent_id=agent_id,
                cooldown_until=cooldown_until,
                failure_reason=failure_reason,
            )

        self._cooldowns[agent_id] = cooldown
        logger.info(
            "Agent %s cooldown set until %s (%d failures)",
            agent_id,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cooldown_until)),
            cooldown.failure_count,
        )

        return cooldown

    def is_on_cooldown(self, agent_id: str) -> bool:
        """Check if an agent is currently on cooldown.

        Args:
            agent_id: Agent identifier.

        Returns:
            True if agent is on cooldown.
        """
        if agent_id not in self._cooldowns:
            return False

        cooldown = self._cooldowns[agent_id]
        if not cooldown.is_active:
            # Cooldown expired, remove it
            del self._cooldowns[agent_id]
            return False

        return True

    def get_cooldown(self, agent_id: str) -> AgentCooldown | None:
        """Get cooldown info for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentCooldown or None if not on cooldown.
        """
        if agent_id not in self._cooldowns:
            return None

        cooldown = self._cooldowns[agent_id]
        if not cooldown.is_active:
            del self._cooldowns[agent_id]
            return None

        return cooldown

    def clear_cooldown(self, agent_id: str) -> bool:
        """Clear cooldown for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            True if cooldown was cleared.
        """
        if agent_id in self._cooldowns:
            del self._cooldowns[agent_id]
            logger.info("Agent %s cooldown cleared", agent_id)
            return True
        return False

    def get_all_cooldowns(self) -> list[AgentCooldown]:
        """Get all active cooldowns.

        Returns:
            List of active AgentCooldown instances.
        """
        # Clean up expired cooldowns
        expired = [aid for aid, cd in self._cooldowns.items() if not cd.is_active]
        for aid in expired:
            del self._cooldowns[aid]

        return list(self._cooldowns.values())

    def get_summary(self) -> dict[str, Any]:
        """Get cooldown summary.

        Returns:
            Summary dictionary.
        """
        active = self.get_all_cooldowns()
        return {
            "total_cooldowns": len(active),
            "cooldowns": [cd.to_dict() for cd in active],
            "default_cooldown_seconds": self._cooldown_seconds,
        }
