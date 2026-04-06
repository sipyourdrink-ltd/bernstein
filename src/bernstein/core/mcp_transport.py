"""MCP transport abstraction layer (MCP-004).

Defines a protocol for MCP transports (stdio, SSE, StreamableHTTP) and a
factory for registering/creating them.  New transports register via
:func:`register_transport` and are instantiated via :func:`create_transport`.

Each transport encapsulates connectivity details (process management, HTTP
connections) while exposing a uniform ``connect`` / ``health_check`` /
``disconnect`` interface that :class:`~bernstein.core.mcp_manager.MCPManager`
consumes.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class McpTransport(Protocol):
    """Protocol that all MCP transports must implement.

    Transports handle the low-level connectivity to an MCP server --
    spawning subprocesses, opening HTTP connections, etc.
    """

    @property
    def transport_type(self) -> str:
        """Return the transport type identifier (e.g. 'stdio', 'sse')."""
        ...

    @property
    def is_connected(self) -> bool:
        """Return True if the transport is currently connected."""
        ...

    def connect(self, config: TransportConfig) -> None:
        """Establish the transport connection.

        Args:
            config: Transport-specific configuration.

        Raises:
            TransportError: If the connection cannot be established.
        """
        ...

    def health_check(self) -> bool:
        """Probe whether the connection is still healthy.

        Returns:
            True if the transport is healthy, False otherwise.
        """
        ...

    def disconnect(self) -> None:
        """Tear down the transport connection.

        Safe to call multiple times.
        """
        ...


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportConfig:
    """Configuration bag passed to transports at connect time.

    Attributes:
        command: Command parts for stdio transport.
        url: URL for SSE / StreamableHTTP transport.
        env: Extra environment variables for subprocess transports.
        headers: HTTP headers for network transports.
        timeout: Connection/health-check timeout in seconds.
    """

    command: list[str] = field(default_factory=list[str])
    url: str = ""
    env: dict[str, str] = field(default_factory=dict[str, str])
    headers: dict[str, str] = field(default_factory=dict[str, str])
    timeout: float = 10.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """Raised when a transport operation fails."""


# ---------------------------------------------------------------------------
# Concrete transports
# ---------------------------------------------------------------------------


def _merge_env(extra: dict[str, str]) -> dict[str, str]:
    """Merge extra env vars with current process environment."""
    import os

    env = dict(os.environ)
    env.update(extra)
    return env


class StdioTransport:
    """MCP transport over stdio subprocess.

    Spawns the MCP server as a child process and communicates via
    stdin/stdout.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def transport_type(self) -> str:
        return "stdio"

    @property
    def is_connected(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        """The underlying subprocess, if connected."""
        return self._process

    def connect(self, config: TransportConfig) -> None:
        """Spawn the MCP server subprocess.

        Args:
            config: Must have a non-empty ``command``.

        Raises:
            TransportError: If the command is empty or the process fails to start.
        """
        if not config.command:
            raise TransportError("StdioTransport requires a non-empty command")
        try:
            self._process = subprocess.Popen(
                config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_merge_env(config.env) if config.env else None,
                start_new_session=True,
            )
            logger.info(
                "StdioTransport connected: pid=%d cmd=%s",
                self._process.pid,
                " ".join(config.command),
            )
        except Exception as exc:
            raise TransportError(f"Failed to spawn process: {exc}") from exc

    def health_check(self) -> bool:
        """Check if subprocess is still running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def disconnect(self) -> None:
        """Terminate the subprocess."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2)
        except Exception as exc:
            logger.warning("Error disconnecting StdioTransport: %s", exc)
        finally:
            self._process = None


class SseTransport:
    """MCP transport over Server-Sent Events (SSE).

    Validates the URL and marks itself as connected.  Health checks
    attempt an HTTP HEAD against the URL.
    """

    def __init__(self) -> None:
        self._url: str = ""
        self._connected: bool = False
        self._timeout: float = 10.0

    @property
    def transport_type(self) -> str:
        return "sse"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def url(self) -> str:
        """The SSE endpoint URL."""
        return self._url

    def connect(self, config: TransportConfig) -> None:
        """Validate and store the SSE URL.

        Args:
            config: Must have a non-empty ``url``.

        Raises:
            TransportError: If the URL is empty.
        """
        if not config.url:
            raise TransportError("SseTransport requires a non-empty url")
        self._url = config.url
        self._timeout = config.timeout
        self._connected = True
        logger.info("SseTransport connected to %s", self._url)

    def health_check(self) -> bool:
        """Attempt HTTP HEAD against the SSE endpoint."""
        if not self._connected or not self._url:
            return False
        try:
            import urllib.request

            req = urllib.request.Request(self._url, method="HEAD")
            with urllib.request.urlopen(req, timeout=self._timeout):
                return True
        except Exception:
            return False

    def disconnect(self) -> None:
        """Mark the SSE transport as disconnected."""
        self._connected = False
        self._url = ""


class StreamableHttpTransport:
    """MCP transport over Streamable HTTP (bidirectional HTTP streaming).

    Similar to SSE but uses POST for sending and streaming responses.
    """

    def __init__(self) -> None:
        self._url: str = ""
        self._connected: bool = False
        self._timeout: float = 10.0
        self._headers: dict[str, str] = {}

    @property
    def transport_type(self) -> str:
        return "streamable_http"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def url(self) -> str:
        """The HTTP endpoint URL."""
        return self._url

    def connect(self, config: TransportConfig) -> None:
        """Validate and store the HTTP endpoint URL.

        Args:
            config: Must have a non-empty ``url``.

        Raises:
            TransportError: If the URL is empty.
        """
        if not config.url:
            raise TransportError("StreamableHttpTransport requires a non-empty url")
        self._url = config.url
        self._timeout = config.timeout
        self._headers = dict(config.headers)
        self._connected = True
        logger.info("StreamableHttpTransport connected to %s", self._url)

    def health_check(self) -> bool:
        """Attempt HTTP HEAD against the endpoint."""
        if not self._connected or not self._url:
            return False
        try:
            import urllib.request

            req = urllib.request.Request(self._url, method="HEAD", headers=self._headers)
            with urllib.request.urlopen(req, timeout=self._timeout):
                return True
        except Exception:
            return False

    def disconnect(self) -> None:
        """Mark the transport as disconnected."""
        self._connected = False
        self._url = ""
        self._headers = {}


# ---------------------------------------------------------------------------
# Transport factory
# ---------------------------------------------------------------------------

# type alias for factory callables
type TransportFactory = type[McpTransport] | Any

_TRANSPORT_REGISTRY: dict[str, type] = {
    "stdio": StdioTransport,
    "sse": SseTransport,
    "streamable_http": StreamableHttpTransport,
}


def register_transport(name: str, factory: type) -> None:
    """Register a new transport type.

    Args:
        name: Transport identifier (e.g. ``"grpc"``).
        factory: A class whose instances satisfy :class:`McpTransport`.

    Raises:
        ValueError: If *name* is already registered.
    """
    if name in _TRANSPORT_REGISTRY:
        raise ValueError(f"Transport {name!r} is already registered")
    _TRANSPORT_REGISTRY[name] = factory
    logger.info("Registered MCP transport: %s", name)


def create_transport(name: str) -> McpTransport:
    """Instantiate a transport by name.

    Args:
        name: Registered transport identifier.

    Returns:
        A new transport instance.

    Raises:
        KeyError: If *name* is not registered.
    """
    if name not in _TRANSPORT_REGISTRY:
        registered = ", ".join(sorted(_TRANSPORT_REGISTRY))
        raise KeyError(f"Unknown transport {name!r}. Registered: {registered}")
    instance: McpTransport = _TRANSPORT_REGISTRY[name]()
    return instance


def list_transports() -> list[str]:
    """Return sorted list of registered transport names."""
    return sorted(_TRANSPORT_REGISTRY)


def reset_transport_registry() -> None:
    """Reset the registry to built-in transports only.

    Intended for tests.
    """
    _TRANSPORT_REGISTRY.clear()
    _TRANSPORT_REGISTRY.update(
        {
            "stdio": StdioTransport,
            "sse": SseTransport,
            "streamable_http": StreamableHttpTransport,
        }
    )
