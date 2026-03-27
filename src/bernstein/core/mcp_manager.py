"""MCP server lifecycle manager.

Manages the lifecycle of MCP servers — starting them as subprocesses (stdio
transport) or connecting via SSE, maintaining health status, and providing
per-task MCP configuration to spawned agents.

Configuration comes from the ``mcp_servers`` section of bernstein.yaml and
is complemented by the auto-detection logic in :mod:`bernstein.core.mcp_registry`.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server.

    Attributes:
        name: Human-readable identifier (used as mcpServers key).
        command: Command parts for stdio transport (e.g. ["npx", "-y", "pkg"]).
        url: URL for SSE transport.
        transport: Transport type — "stdio" or "sse".
        env: Extra environment variables for the server process.
    """

    name: str
    command: list[str] = field(default_factory=list[str])
    url: str = ""
    transport: Literal["stdio", "sse"] = "stdio"
    env: dict[str, str] = field(default_factory=dict[str, str])

    def to_mcp_config_entry(self) -> dict[str, Any]:
        """Build the mcpServers dict entry for this server.

        Returns:
            Dict suitable for inclusion in the ``mcpServers`` structure
            consumed by Claude Code's ``--mcp-config`` flag.
        """
        if self.transport == "sse":
            entry: dict[str, Any] = {"url": self.url}
        else:
            if not self.command:
                return {}
            entry = {"command": self.command[0], "args": list(self.command[1:])}
        if self.env:
            entry["env"] = dict(self.env)
        return entry


def parse_server_configs(raw: dict[str, dict[str, Any]]) -> list[MCPServerConfig]:
    """Parse raw YAML/dict MCP server definitions into typed configs.

    Accepts the ``mcp_servers`` mapping from bernstein.yaml::

        mcp_servers:
          github:
            command: ["npx", "-y", "@anthropic/github-mcp"]
            env:
              GITHUB_TOKEN: "${GITHUB_TOKEN}"
          custom-api:
            url: "http://localhost:9090/sse"
            transport: sse

    Args:
        raw: Mapping of server name to config dict.

    Returns:
        List of parsed MCPServerConfig instances.
    """
    configs: list[MCPServerConfig] = []
    for name, defn in raw.items():
        # Determine transport from explicit key or infer from url presence
        transport_raw = defn.get("transport", "")
        if transport_raw == "sse" or (not transport_raw and defn.get("url")):
            transport: Literal["stdio", "sse"] = "sse"
        else:
            transport = "stdio"

        command_raw: Any = defn.get("command", [])
        if isinstance(command_raw, str):
            command = [command_raw]
        elif isinstance(command_raw, list):
            command = [str(c) for c in cast("list[Any]", command_raw)]
        else:
            command = []

        args_raw: Any = defn.get("args", [])
        if isinstance(args_raw, list):
            command.extend(str(a) for a in cast("list[Any]", args_raw))

        env_raw: Any = defn.get("env", {})
        env: dict[str, str] = {}
        if isinstance(env_raw, dict):
            env = {str(k): str(v) for k, v in cast("dict[Any, Any]", env_raw).items()}

        url = str(defn.get("url", ""))

        configs.append(
            MCPServerConfig(
                name=name,
                command=command,
                url=url,
                transport=transport,
                env=env,
            )
        )
    return configs


@dataclass
class _ServerState:
    """Internal tracking for a running MCP server process.

    Attributes:
        config: The server config this state tracks.
        process: The subprocess.Popen for stdio servers, None for SSE.
        started_at: Monotonic timestamp when the server was started.
        alive: Whether the server is considered alive.
    """

    config: MCPServerConfig
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    alive: bool = False


class MCPManager:
    """Manage MCP server lifecycles for a Bernstein orchestrator run.

    Starts MCP servers as subprocesses (stdio) or validates SSE endpoints,
    tracks health, and provides per-agent MCP configuration dicts.

    Args:
        configs: List of MCP server configurations to manage.
    """

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self._configs: list[MCPServerConfig] = list(configs) if configs else []
        self._servers: dict[str, _ServerState] = {}

    @property
    def configs(self) -> list[MCPServerConfig]:
        """All registered server configurations."""
        return list(self._configs)

    @property
    def server_names(self) -> list[str]:
        """Names of all managed servers."""
        return [c.name for c in self._configs]

    def add_config(self, config: MCPServerConfig) -> None:
        """Add a server config (does not start it).

        Args:
            config: Server configuration to add.
        """
        self._configs.append(config)

    def start_all(self) -> None:
        """Start all configured MCP servers.

        For stdio servers, launches the subprocess. For SSE servers, marks
        them as alive (connectivity is checked lazily via health checks).
        Servers that fail to start are logged as warnings but do not block
        other servers.
        """
        for config in self._configs:
            if config.name in self._servers:
                continue
            try:
                self._start_server(config)
            except Exception as exc:
                logger.warning("Failed to start MCP server '%s': %s", config.name, exc)

    def _start_server(self, config: MCPServerConfig) -> None:
        """Start a single MCP server.

        Args:
            config: Server to start.
        """
        state = _ServerState(config=config, started_at=time.monotonic())

        if config.transport == "stdio":
            if not config.command:
                logger.warning("MCP server '%s' has no command, skipping", config.name)
                return
            proc = subprocess.Popen(
                config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_merge_env(config.env) if config.env else None,
                start_new_session=True,
            )
            state.process = proc
            state.alive = True
            logger.info(
                "Started MCP server '%s' (pid=%d, cmd=%s)",
                config.name,
                proc.pid,
                " ".join(config.command),
            )
        else:
            # SSE transport — no subprocess to manage, mark alive optimistically
            state.alive = bool(config.url)
            if state.alive:
                logger.info("Registered SSE MCP server '%s' at %s", config.name, config.url)
            else:
                logger.warning("SSE MCP server '%s' has no URL", config.name)

        self._servers[config.name] = state

    def stop_all(self) -> None:
        """Stop all running MCP servers.

        Terminates stdio subprocesses. SSE servers are simply marked dead.
        Safe to call multiple times.
        """
        for name in list(self._servers):
            self._stop_server(name)
        self._servers.clear()

    def _stop_server(self, name: str) -> None:
        """Stop a single server by name.

        Args:
            name: Server name to stop.
        """
        state = self._servers.get(name)
        if state is None:
            return
        if state.process is not None:
            try:
                state.process.terminate()
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()
                state.process.wait(timeout=2)
            except Exception as exc:
                logger.warning("Error stopping MCP server '%s': %s", name, exc)
        state.alive = False
        logger.info("Stopped MCP server '%s'", name)

    def is_alive(self, name: str) -> bool:
        """Check if a server is alive.

        For stdio servers, polls the subprocess. For SSE servers, returns
        the last known status.

        Args:
            name: Server name to check.

        Returns:
            True if the server is considered alive.
        """
        state = self._servers.get(name)
        if state is None:
            return False
        if state.process is not None and state.process.poll() is not None:
            state.alive = False
        return state.alive

    def get_server_info(self, name: str) -> MCPServerConfig | None:
        """Look up a server configuration by name.

        Args:
            name: Server name to look up.

        Returns:
            The MCPServerConfig, or None if not found.
        """
        for config in self._configs:
            if config.name == name:
                return config
        return None

    def build_mcp_config(
        self,
        server_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Build MCP config dict for a subset (or all) of managed servers.

        Only includes servers that are currently alive.

        Args:
            server_names: Subset of server names to include. If None, includes
                all alive servers.

        Returns:
            Dict with ``{"mcpServers": {...}}`` structure, or None if empty.
        """
        mcp_servers: dict[str, Any] = {}
        targets = server_names if server_names is not None else self.server_names

        for name in targets:
            if not self.is_alive(name):
                logger.debug("MCP server '%s' not alive, skipping from config", name)
                continue
            config = self.get_server_info(name)
            if config is None:
                continue
            entry = config.to_mcp_config_entry()
            if entry:
                mcp_servers[name] = entry

        if not mcp_servers:
            return None
        return {"mcpServers": mcp_servers}

    def build_mcp_config_for_task(
        self,
        task_mcp_servers: list[str] | None,
        base_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build merged MCP config for a specific task.

        Combines the task's requested servers (from ``task.mcp_servers``)
        with any base MCP config. Task-requested servers override base config
        entries with the same name.

        Args:
            task_mcp_servers: Server names requested by the task, or None
                for all alive servers.
            base_config: Existing MCP config to merge with.

        Returns:
            Merged ``{"mcpServers": {...}}`` dict, or None if empty.
        """
        task_config = self.build_mcp_config(server_names=task_mcp_servers)

        if base_config is None and task_config is None:
            return None
        if base_config is None:
            return task_config
        if task_config is None:
            return base_config

        # Merge: task config wins on conflicts (task explicitly requested those)
        merged_servers = dict(base_config.get("mcpServers", {}))
        merged_servers.update(task_config.get("mcpServers", {}))
        return {"mcpServers": merged_servers}


def _merge_env(extra: dict[str, str]) -> dict[str, str]:
    """Merge extra env vars with the current process environment.

    Args:
        extra: Additional environment variables.

    Returns:
        Combined environment dict.
    """
    import os

    env = dict(os.environ)
    env.update(extra)
    return env
