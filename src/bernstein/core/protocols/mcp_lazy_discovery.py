"""MCP-005: Lazy MCP server discovery and on-demand startup.

Only starts MCP servers when an agent actually needs their tools, rather
than starting all configured servers eagerly on orchestrator boot.

Servers are tracked in three states:
  - REGISTERED: config known but server not started.
  - STARTING: start requested, subprocess launching.
  - RUNNING: server process alive and ready.
  - FAILED: start attempted and failed.

Usage::

    from bernstein.core.mcp_lazy_discovery import LazyMCPDiscovery

    discovery = LazyMCPDiscovery()
    discovery.register(config)
    # Server stays dormant until:
    discovery.ensure_running("github")   # starts on first call
    discovery.ensure_running("github")   # no-op, already running
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bernstein.core.mcp_manager import MCPManager, MCPServerConfig

logger = logging.getLogger(__name__)


class ServerState(StrEnum):
    """Lifecycle state for a lazily-managed MCP server."""

    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


@dataclass
class LazyServerEntry:
    """Tracks a lazily-managed MCP server.

    Attributes:
        config: Server configuration.
        state: Current lifecycle state.
        started_at: Monotonic timestamp when last start was attempted.
        failure_reason: Human-readable reason for failure, if any.
        start_count: Number of times ``ensure_running`` was called.
    """

    config: MCPServerConfig
    state: ServerState = ServerState.REGISTERED
    started_at: float = 0.0
    failure_reason: str = ""
    start_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "name": self.config.name,
            "state": self.state.value,
            "started_at": self.started_at,
            "failure_reason": self.failure_reason,
            "start_count": self.start_count,
        }


class LazyMCPDiscovery:
    """Lazy MCP server discovery and on-demand startup.

    Wraps an :class:`MCPManager` and defers server startup until an agent
    actually requests a server's tools via :meth:`ensure_running`.

    Args:
        manager: The underlying MCPManager that handles actual process
            lifecycle. If None, a new empty manager is created.
    """

    def __init__(self, manager: MCPManager | None = None) -> None:
        self._manager = manager or MCPManager()
        self._entries: dict[str, LazyServerEntry] = {}

    @property
    def manager(self) -> MCPManager:
        """The underlying MCPManager."""
        return self._manager

    def register(self, config: MCPServerConfig) -> None:
        """Register a server config without starting it.

        Args:
            config: Server configuration to register.
        """
        if config.name in self._entries:
            logger.debug("MCP server '%s' already registered, updating config", config.name)
        self._entries[config.name] = LazyServerEntry(config=config)
        self._manager.add_config(config)
        logger.debug("Lazily registered MCP server '%s'", config.name)

    def register_many(self, configs: list[MCPServerConfig]) -> None:
        """Register multiple server configs without starting them.

        Args:
            configs: Server configurations to register.
        """
        for config in configs:
            self.register(config)

    def ensure_running(self, name: str) -> bool:
        """Start a server if not already running.

        Returns True if the server is alive after this call, False if the
        server could not be started or is unknown.

        Args:
            name: Server name to start.

        Returns:
            True if the server is running.
        """
        entry = self._entries.get(name)
        if entry is None:
            logger.warning("Cannot start unknown MCP server '%s'", name)
            return False

        entry.start_count += 1

        if entry.state == ServerState.RUNNING and self._manager.is_alive(name):
            return True

        entry.state = ServerState.STARTING
        entry.started_at = time.monotonic()
        try:
            self._manager._start_server(entry.config)  # pyright: ignore[reportPrivateUsage]
            if self._manager.is_alive(name):
                entry.state = ServerState.RUNNING
                entry.failure_reason = ""
                logger.info("Lazily started MCP server '%s'", name)
                return True
            else:
                entry.state = ServerState.FAILED
                entry.failure_reason = "Server not alive after start"
                return False
        except Exception as exc:
            entry.state = ServerState.FAILED
            entry.failure_reason = str(exc)
            logger.warning("Failed to lazily start MCP server '%s': %s", name, exc)
            return False

    def ensure_running_many(self, names: list[str]) -> dict[str, bool]:
        """Start multiple servers, returning per-server success.

        Args:
            names: Server names to start.

        Returns:
            Dict mapping server name to whether it is running.
        """
        return {name: self.ensure_running(name) for name in names}

    def get_state(self, name: str) -> ServerState | None:
        """Return the current state for a server, or None if unknown.

        Args:
            name: Server name.

        Returns:
            The server's lifecycle state.
        """
        entry = self._entries.get(name)
        return entry.state if entry is not None else None

    def list_entries(self) -> list[LazyServerEntry]:
        """Return all tracked server entries."""
        return list(self._entries.values())

    def get_running_names(self) -> list[str]:
        """Return names of servers currently in RUNNING state."""
        return [
            name
            for name, entry in self._entries.items()
            if entry.state == ServerState.RUNNING and self._manager.is_alive(name)
        ]

    def stop_idle(self, idle_seconds: float = 300.0) -> list[str]:
        """Stop servers that have been idle longer than the threshold.

        Args:
            idle_seconds: Seconds of inactivity before stopping.

        Returns:
            Names of servers that were stopped.
        """
        stopped: list[str] = []
        now = time.monotonic()
        for name, entry in self._entries.items():
            if entry.state != ServerState.RUNNING:
                continue
            if entry.started_at > 0 and (now - entry.started_at) > idle_seconds:
                self._manager._stop_server(name)  # pyright: ignore[reportPrivateUsage]
                entry.state = ServerState.REGISTERED
                stopped.append(name)
                logger.info("Stopped idle MCP server '%s'", name)
        return stopped

    def to_dict(self) -> dict[str, Any]:
        """Serialize all entries to a JSON-compatible dict."""
        return {name: entry.to_dict() for name, entry in self._entries.items()}
