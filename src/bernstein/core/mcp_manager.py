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


# ---------------------------------------------------------------------------
# MCP capability snapshot persistence (T553)
# ---------------------------------------------------------------------------


@dataclass
class MCPCapabilitySnapshot:
    """Point-in-time snapshot of MCP server capabilities and health.

    Attributes:
        captured_at: Unix timestamp when the snapshot was taken.
        server_name: Name of the MCP server.
        alive: Whether the server was alive at capture time.
        capabilities: List of capability tags reported by the server.
        transport: Transport type (``"stdio"`` or ``"sse"``).
        uptime_seconds: Seconds since the server was started.
        oauth_expiry: Unix timestamp when OAuth token expires, if applicable.
        scopes: OAuth scopes granted to this server.
    """

    captured_at: float
    server_name: str
    alive: bool
    capabilities: list[str] = field(default_factory=list[str])
    transport: str = "stdio"
    uptime_seconds: float = 0.0
    oauth_expiry: float | None = None
    scopes: list[str] = field(default_factory=list[str])

    def is_oauth_expiring_soon(self, threshold_seconds: float = 300.0) -> bool:
        """Return True if the OAuth token expires within *threshold_seconds*."""
        if self.oauth_expiry is None:
            return False
        return (self.oauth_expiry - time.time()) < threshold_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "captured_at": self.captured_at,
            "server_name": self.server_name,
            "alive": self.alive,
            "capabilities": self.capabilities,
            "transport": self.transport,
            "uptime_seconds": self.uptime_seconds,
            "oauth_expiry": self.oauth_expiry,
            "scopes": self.scopes,
            "oauth_expiring_soon": self.is_oauth_expiring_soon(),
        }


# ---------------------------------------------------------------------------
# MCP server health history timeline (T556)
# ---------------------------------------------------------------------------


@dataclass
class MCPHealthEvent:
    """A single health status change event for an MCP server.

    Attributes:
        ts: Unix timestamp of the event.
        server_name: Name of the MCP server.
        alive: Health status at this point.
        reason: Human-readable reason for the status change.
    """

    ts: float
    server_name: str
    alive: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "ts": self.ts,
            "server_name": self.server_name,
            "alive": self.alive,
            "reason": self.reason,
        }


class MCPHealthHistory:
    """Rolling health history for all managed MCP servers (T556).

    Stores the last *max_events* health events per server.

    Args:
        max_events: Maximum events to retain per server.
    """

    def __init__(self, max_events: int = 100) -> None:
        self._max_events = max_events
        self._events: dict[str, list[MCPHealthEvent]] = {}

    def record(self, server_name: str, alive: bool, reason: str = "") -> None:
        """Record a health status event.

        Args:
            server_name: Name of the server.
            alive: Current health status.
            reason: Optional reason for the change.
        """
        event = MCPHealthEvent(ts=time.time(), server_name=server_name, alive=alive, reason=reason)
        history = self._events.setdefault(server_name, [])
        history.append(event)
        if len(history) > self._max_events:
            del history[: len(history) - self._max_events]

    def get_history(self, server_name: str) -> list[MCPHealthEvent]:
        """Return health events for *server_name* in chronological order."""
        return list(self._events.get(server_name, []))

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Serialise all history to a JSON-compatible dict."""
        return {name: [e.to_dict() for e in events] for name, events in self._events.items()}


# ---------------------------------------------------------------------------
# MCP scope precedence explainer (T555)
# ---------------------------------------------------------------------------


@dataclass
class MCPScopePrecedenceEntry:
    """One entry in the MCP scope precedence chain.

    Attributes:
        source: Where this scope came from (``"task"``, ``"global"``,
            ``"server_default"``).
        scopes: Scopes granted at this level.
        server_name: MCP server this entry applies to.
    """

    source: str
    scopes: list[str]
    server_name: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {"source": self.source, "scopes": self.scopes, "server_name": self.server_name}


def explain_mcp_scope_precedence(
    server_name: str,
    task_scopes: list[str] | None,
    global_scopes: list[str] | None,
    server_default_scopes: list[str] | None,
) -> list[MCPScopePrecedenceEntry]:
    """Build the scope precedence chain for an MCP server (T555).

    Precedence (highest first): task → global → server_default.

    Args:
        server_name: Name of the MCP server.
        task_scopes: Scopes requested by the current task.
        global_scopes: Globally configured scopes.
        server_default_scopes: Default scopes from the server definition.

    Returns:
        Ordered list of :class:`MCPScopePrecedenceEntry` objects.
    """
    chain: list[MCPScopePrecedenceEntry] = []
    if task_scopes is not None:
        chain.append(MCPScopePrecedenceEntry(source="task", scopes=task_scopes, server_name=server_name))
    if global_scopes is not None:
        chain.append(MCPScopePrecedenceEntry(source="global", scopes=global_scopes, server_name=server_name))
    if server_default_scopes is not None:
        chain.append(
            MCPScopePrecedenceEntry(source="server_default", scopes=server_default_scopes, server_name=server_name)
        )
    return chain


# ---------------------------------------------------------------------------
# MCPManager extensions: snapshot + health history + OAuth expiry (T553, T554, T556)
# ---------------------------------------------------------------------------


def build_mcp_capability_snapshots(manager: MCPManager) -> list[MCPCapabilitySnapshot]:
    """Build capability snapshots for all servers in *manager* (T553).

    Args:
        manager: The :class:`MCPManager` to snapshot.

    Returns:
        List of :class:`MCPCapabilitySnapshot` objects.
    """
    snapshots: list[MCPCapabilitySnapshot] = []
    now = time.time()
    for config in manager.configs:
        state = manager._servers.get(config.name)  # type: ignore[reportPrivateUsage]
        alive = manager.is_alive(config.name)
        uptime = (now - state.started_at) if state is not None else 0.0
        snapshots.append(
            MCPCapabilitySnapshot(
                captured_at=now,
                server_name=config.name,
                alive=alive,
                transport=config.transport,
                uptime_seconds=uptime,
            )
        )
    return snapshots


def get_oauth_expiry_dashboard(snapshots: list[MCPCapabilitySnapshot]) -> list[dict[str, Any]]:
    """Build an OAuth expiry dashboard from capability snapshots (T554).

    Args:
        snapshots: List of :class:`MCPCapabilitySnapshot` objects.

    Returns:
        List of dicts with ``server_name``, ``oauth_expiry``,
        ``expiring_soon``, and ``seconds_remaining``.
    """
    dashboard: list[dict[str, Any]] = []
    now = time.time()
    for snap in snapshots:
        if snap.oauth_expiry is not None:
            seconds_remaining = snap.oauth_expiry - now
            dashboard.append(
                {
                    "server_name": snap.server_name,
                    "oauth_expiry": snap.oauth_expiry,
                    "expiring_soon": snap.is_oauth_expiring_soon(),
                    "seconds_remaining": max(0.0, seconds_remaining),
                }
            )
    return dashboard


# ---------------------------------------------------------------------------
# OAuth refresh on 401/403 errors (T568)
# ---------------------------------------------------------------------------


class OAuthRefreshError(Exception):
    """Raised when an OAuth token refresh attempt fails."""


def should_attempt_oauth_refresh(status_code: int) -> bool:
    """Return True if *status_code* warrants an OAuth token refresh (T568).

    Args:
        status_code: HTTP response status code.

    Returns:
        True for 401 (Unauthorized) and 403 (Forbidden).
    """
    return status_code in (401, 403)


def refresh_oauth_token(
    server_name: str,
    *,
    refresh_url: str,
    client_id: str,
    refresh_token: str,
    timeout: float = 10.0,
) -> str:
    """Attempt to refresh an OAuth token for an MCP server (T568).

    Performs a single bounded refresh attempt.  Raises
    :class:`OAuthRefreshError` on failure so callers can decide whether to
    retry or surface the error.

    Args:
        server_name: Name of the MCP server (for logging).
        refresh_url: Token refresh endpoint URL.
        client_id: OAuth client ID.
        refresh_token: Current refresh token.
        timeout: Request timeout in seconds.

    Returns:
        New access token string.

    Raises:
        OAuthRefreshError: If the refresh request fails.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    ).encode()

    req = urllib.request.Request(
        refresh_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json as _json

            data = _json.loads(resp.read().decode())
            access_token = data.get("access_token")
            if not access_token:
                raise OAuthRefreshError(f"No access_token in refresh response for '{server_name}'")
            logger.info("OAuth token refreshed for MCP server '%s'", server_name)
            return str(access_token)
    except urllib.error.HTTPError as exc:
        raise OAuthRefreshError(f"OAuth refresh failed for '{server_name}': HTTP {exc.code}") from exc
    except Exception as exc:
        raise OAuthRefreshError(f"OAuth refresh failed for '{server_name}': {exc}") from exc
