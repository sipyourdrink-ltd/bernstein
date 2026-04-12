"""SEC-013: API key rotation support with agent hot-update.

Extends the existing key_rotation module with support for detecting expiring
keys, issuing new tokens, and updating running agents without restart.

Usage::

    from bernstein.core.key_rotation_support import (
        KeyExpiryDetector,
        AgentKeyUpdater,
        RotationOrchestrator,
    )

    detector = KeyExpiryDetector(warning_threshold_seconds=86400)
    expiring = detector.check_keys(managed_keys)

    updater = AgentKeyUpdater()
    updater.update_agent(agent_id, env_var, new_value)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum

from bernstein.core.key_rotation import (
    KeyRotationManager,
    KeyState,
    ManagedKey,
)

logger = logging.getLogger(__name__)


class ExpiryStatus(StrEnum):
    """Status of a key relative to its rotation schedule."""

    HEALTHY = "healthy"
    WARNING = "warning"  # Approaching rotation deadline
    EXPIRED = "expired"  # Past rotation deadline
    REVOKED = "revoked"


@dataclass(frozen=True)
class KeyExpiryInfo:
    """Information about a key's expiry status.

    Attributes:
        key: The managed key.
        status: Current expiry status.
        seconds_until_rotation: Seconds until the key is due for rotation.
            Negative means overdue.
        message: Human-readable status message.
    """

    key: ManagedKey
    status: ExpiryStatus
    seconds_until_rotation: float
    message: str


@dataclass(frozen=True)
class AgentUpdateResult:
    """Result of updating an agent's API key.

    Attributes:
        agent_id: The agent that was updated.
        env_var: Environment variable that was updated.
        success: Whether the update succeeded.
        message: Status message.
    """

    agent_id: str
    env_var: str
    success: bool
    message: str


class KeyExpiryDetector:
    """Detects keys approaching or past their rotation deadline.

    Args:
        warning_threshold_seconds: How far in advance to warn about
            upcoming rotation (default 24 hours).
        rotation_interval_seconds: Rotation interval to check against.
    """

    def __init__(
        self,
        warning_threshold_seconds: float = 86400.0,
        rotation_interval_seconds: float = 2592000.0,
    ) -> None:
        self._warning_threshold = warning_threshold_seconds
        self._rotation_interval = rotation_interval_seconds

    def check_key(self, key: ManagedKey) -> KeyExpiryInfo:
        """Check a single key's expiry status.

        Args:
            key: The managed key to check.

        Returns:
            Expiry information for the key.
        """
        if key.state == KeyState.REVOKED:
            return KeyExpiryInfo(
                key=key,
                status=ExpiryStatus.REVOKED,
                seconds_until_rotation=0,
                message=f"Key {key.key_id} is revoked: {key.revoke_reason}",
            )

        if key.state == KeyState.EXPIRED:
            return KeyExpiryInfo(
                key=key,
                status=ExpiryStatus.EXPIRED,
                seconds_until_rotation=0,
                message=f"Key {key.key_id} has been rotated out",
            )

        now = time.time()
        last_rotation = key.rotated_at or key.created_at
        age = now - last_rotation
        remaining = self._rotation_interval - age

        if remaining <= 0:
            return KeyExpiryInfo(
                key=key,
                status=ExpiryStatus.EXPIRED,
                seconds_until_rotation=remaining,
                message=f"Key {key.key_id} is {-remaining:.0f}s past rotation deadline",
            )

        if remaining <= self._warning_threshold:
            return KeyExpiryInfo(
                key=key,
                status=ExpiryStatus.WARNING,
                seconds_until_rotation=remaining,
                message=f"Key {key.key_id} expires in {remaining:.0f}s",
            )

        return KeyExpiryInfo(
            key=key,
            status=ExpiryStatus.HEALTHY,
            seconds_until_rotation=remaining,
            message=f"Key {key.key_id} is healthy ({remaining:.0f}s until rotation)",
        )

    def check_keys(self, keys: list[ManagedKey]) -> list[KeyExpiryInfo]:
        """Check multiple keys and return their expiry status.

        Args:
            keys: List of managed keys to check.

        Returns:
            List of expiry info for each key.
        """
        return [self.check_key(k) for k in keys]

    def get_expiring(self, keys: list[ManagedKey]) -> list[KeyExpiryInfo]:
        """Return only keys that are WARNING or EXPIRED.

        Args:
            keys: List of managed keys to check.

        Returns:
            Filtered list of keys needing attention.
        """
        return [info for info in self.check_keys(keys) if info.status in {ExpiryStatus.WARNING, ExpiryStatus.EXPIRED}]


class AgentKeyUpdater:
    """Updates running agents' API keys without requiring a restart.

    Maintains a registry of agent sessions and their environment variables.
    When a key is rotated, the updater pushes the new value to all affected
    agents via environment variable update.
    """

    def __init__(self) -> None:
        self._agent_env: dict[str, dict[str, str]] = {}
        self._update_log: list[AgentUpdateResult] = []

    @property
    def update_log(self) -> list[AgentUpdateResult]:
        """Return the log of update results."""
        return list(self._update_log)

    def register_agent(self, agent_id: str, env_vars: dict[str, str]) -> None:
        """Register an agent and its environment variables.

        Args:
            agent_id: The agent identifier.
            env_vars: The agent's environment variables.
        """
        self._agent_env[agent_id] = dict(env_vars)

    def unregister_agent(self, agent_id: str) -> None:
        """Remove an agent from the registry.

        Args:
            agent_id: The agent to remove.
        """
        self._agent_env.pop(agent_id, None)

    def update_agent(
        self,
        agent_id: str,
        env_var: str,
        new_value: str,
    ) -> AgentUpdateResult:
        """Update a specific environment variable for an agent.

        Also updates the current process environment so child processes
        pick up the change.

        Args:
            agent_id: The agent to update.
            env_var: Environment variable name.
            new_value: New value to set.

        Returns:
            Result of the update operation.
        """
        if agent_id not in self._agent_env:
            result = AgentUpdateResult(
                agent_id=agent_id,
                env_var=env_var,
                success=False,
                message=f"Agent {agent_id} not registered",
            )
            self._update_log.append(result)
            return result

        self._agent_env[agent_id][env_var] = new_value
        os.environ[env_var] = new_value

        result = AgentUpdateResult(
            agent_id=agent_id,
            env_var=env_var,
            success=True,
            message=f"Updated {env_var} for agent {agent_id}",
        )
        self._update_log.append(result)
        logger.info("Updated key %s for agent %s", env_var, agent_id)
        return result

    def update_all_agents(
        self,
        env_var: str,
        new_value: str,
    ) -> list[AgentUpdateResult]:
        """Update an environment variable across all registered agents.

        Args:
            env_var: Environment variable name.
            new_value: New value to set.

        Returns:
            List of update results for each agent.
        """
        results: list[AgentUpdateResult] = []
        for agent_id in list(self._agent_env):
            if env_var in self._agent_env[agent_id]:
                results.append(self.update_agent(agent_id, env_var, new_value))
        return results

    def get_agent_env(self, agent_id: str) -> dict[str, str] | None:
        """Return a copy of an agent's environment variables.

        Args:
            agent_id: The agent to query.

        Returns:
            Copy of the agent's env vars, or None if not registered.
        """
        env = self._agent_env.get(agent_id)
        return dict(env) if env is not None else None


class RotationOrchestrator:
    """Orchestrates key rotation with automatic agent updates.

    Combines KeyExpiryDetector, KeyRotationManager, and AgentKeyUpdater
    to provide a complete rotation workflow.

    Args:
        rotation_manager: The key rotation manager.
        key_updater: The agent key updater.
        warning_threshold: Seconds before rotation to start warning.
    """

    def __init__(
        self,
        rotation_manager: KeyRotationManager,
        key_updater: AgentKeyUpdater,
        warning_threshold: float = 86400.0,
    ) -> None:
        self._manager = rotation_manager
        self._updater = key_updater
        self._detector = KeyExpiryDetector(
            warning_threshold_seconds=warning_threshold,
            rotation_interval_seconds=float(rotation_manager.config.interval_seconds),
        )

    def check_and_rotate(self) -> list[AgentUpdateResult]:
        """Check for expiring keys, rotate them, and update all agents.

        Returns:
            List of agent update results.
        """
        all_results: list[AgentUpdateResult] = []
        expiring = self._detector.get_expiring(self._manager.get_active_keys())

        for info in expiring:
            if info.status == ExpiryStatus.EXPIRED:
                logger.warning("Key %s is past rotation deadline, rotating now", info.key.key_id)
            else:
                logger.info("Key %s approaching rotation, rotating early", info.key.key_id)

            try:
                new_key = self._manager.rotate_key(info.key)
                # Get the new value from the environment (rotate_key sets it)
                new_value = os.environ.get(new_key.env_var, "")
                if new_value:
                    results = self._updater.update_all_agents(new_key.env_var, new_value)
                    all_results.extend(results)
            except Exception as exc:
                logger.error("Failed to rotate key %s: %s", info.key.key_id, exc)

        return all_results
