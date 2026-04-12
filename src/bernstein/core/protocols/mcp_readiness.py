"""MCP server readiness probe before agent spawn (AGENT-005).

After starting an MCP server subprocess, the orchestrator should verify that
the server is actually ready to accept connections before spawning an agent
that depends on it.  Without this check, agents can fail at startup because
their MCP servers are still initializing.

This module provides a configurable readiness probe with timeout and clear
error reporting.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import subprocess

    from bernstein.core.mcp_manager import MCPManager

logger = logging.getLogger(__name__)

#: Default timeout for readiness probes (seconds).
DEFAULT_READINESS_TIMEOUT: float = 10.0

#: How often to poll during the readiness wait (seconds).
DEFAULT_POLL_INTERVAL: float = 0.5


class MCPReadinessError(Exception):
    """Raised when an MCP server fails its readiness probe.

    Attributes:
        server_name: Name of the MCP server that failed.
        reason: Human-readable explanation of the failure.
    """

    def __init__(self, server_name: str, reason: str) -> None:
        self.server_name = server_name
        self.reason = reason
        super().__init__(f"MCP server '{server_name}' not ready: {reason}")


@dataclass(frozen=True)
class ReadinessResult:
    """Result of an MCP server readiness check.

    Attributes:
        server_name: Name of the checked server.
        ready: Whether the server is ready.
        elapsed_s: Time spent waiting for readiness.
        reason: Explanation (empty string when ready).
    """

    server_name: str
    ready: bool
    elapsed_s: float
    reason: str = ""


def probe_stdio_server(
    process: subprocess.Popen[bytes] | Any,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> bool:
    """Probe a stdio MCP server subprocess for readiness.

    A stdio server is considered ready if:
    1. The process has not exited (poll() returns None).
    2. The process has been alive for at least ``poll_interval`` seconds.

    For more sophisticated probes (e.g. sending an initialization message
    over stdin/stdout), subclass or replace this function.

    Args:
        process: The subprocess.Popen object for the server.
        timeout: Maximum seconds to wait for readiness.
        poll_interval: Seconds between poll attempts.

    Returns:
        True if the server appears ready, False if it crashed or timed out.
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            return False
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        # After at least one poll interval, if the process is still alive,
        # consider it ready.
        if process.poll() is None:
            return True

    return process.poll() is None


def probe_sse_server(
    url: str,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> bool:
    """Probe an SSE MCP server URL for readiness.

    Attempts a simple HTTP GET to the server URL.  The server is considered
    ready if it responds with any 2xx status code.

    Args:
        url: The SSE server URL.
        timeout: Maximum seconds to wait for readiness.
        poll_interval: Seconds between retry attempts.

    Returns:
        True if the server responded with 2xx, False otherwise.
    """
    if not url:
        return False

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            import httpx

            resp = httpx.get(url, timeout=min(2.0, timeout))
            if 200 <= resp.status_code < 300:
                return True
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval, remaining))

    return False


def validate_mcp_readiness(
    mcp_manager: MCPManager,
    *,
    server_names: list[str] | None = None,
    timeout: float = DEFAULT_READINESS_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    fail_on_error: bool = True,
) -> list[ReadinessResult]:
    """Validate readiness of MCP servers before agent spawn.

    Probes each specified MCP server (or all managed servers) and returns
    per-server results.  When ``fail_on_error`` is True, raises
    ``MCPReadinessError`` on the first failure.

    Args:
        mcp_manager: The MCPManager instance managing MCP servers.
        server_names: Subset of server names to check.  None = all.
        timeout: Per-server readiness timeout in seconds.
        poll_interval: Seconds between poll attempts.
        fail_on_error: Raise MCPReadinessError on first failure.

    Returns:
        List of ReadinessResult, one per server checked.

    Raises:
        MCPReadinessError: When a server fails its probe and ``fail_on_error``
            is True.
    """
    names = server_names if server_names is not None else mcp_manager.server_names
    results: list[ReadinessResult] = []

    for name in names:
        start = time.monotonic()

        if not mcp_manager.is_alive(name):
            elapsed = time.monotonic() - start
            reason = f"Server '{name}' is not alive (process exited or never started)"
            result = ReadinessResult(
                server_name=name,
                ready=False,
                elapsed_s=round(elapsed, 3),
                reason=reason,
            )
            results.append(result)
            logger.warning("MCP readiness probe failed: %s", reason)
            if fail_on_error:
                raise MCPReadinessError(name, reason)
            continue

        config = mcp_manager.get_server_info(name)
        if config is None:
            elapsed = time.monotonic() - start
            reason = f"No configuration found for server '{name}'"
            result = ReadinessResult(
                server_name=name,
                ready=False,
                elapsed_s=round(elapsed, 3),
                reason=reason,
            )
            results.append(result)
            if fail_on_error:
                raise MCPReadinessError(name, reason)
            continue

        # Probe based on transport type
        # For stdio, mcp_manager.is_alive() already checks process liveness,
        # which is sufficient.  For SSE, we do an HTTP probe.
        ready = True
        reason = ""

        if config.transport == "sse" and config.url:
            ready = probe_sse_server(
                config.url,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            if not ready:
                reason = f"SSE server at {config.url} did not respond within {timeout}s"

        elapsed = time.monotonic() - start
        result = ReadinessResult(
            server_name=name,
            ready=ready,
            elapsed_s=round(elapsed, 3),
            reason=reason,
        )
        results.append(result)

        if ready:
            logger.info(
                "MCP server '%s' ready (%.1fs)",
                name,
                elapsed,
            )
        else:
            logger.warning("MCP readiness probe failed for '%s': %s", name, reason)
            if fail_on_error:
                raise MCPReadinessError(name, reason)

    return results
