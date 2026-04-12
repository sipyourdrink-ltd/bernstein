"""MCP config validation on startup (MCP-002).

Validates all MCP server configurations during orchestrator startup:
- stdio: command exists in PATH
- sse / streamable_http: required env vars are set, URL is reachable
- All transports: required env vars are present

Each check produces a clear, actionable error message per failure.

Usage::

    from bernstein.core.protocols.mcp_config_validator import (
        validate_mcp_configs,
        McpConfigError,
    )

    errors = validate_mcp_configs(configs)
    if errors:
        for err in errors:
            print(f"[{err.server_name}] {err.message}")
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.protocols.mcp_manager import MCPServerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpConfigError:
    """One validation failure for an MCP server config.

    Attributes:
        server_name: Name of the MCP server with the issue.
        check: Short label for the check that failed.
        message: Human-readable, actionable error description.
    """

    server_name: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.server_name}] {self.check}: {self.message}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_command_exists(config: MCPServerConfig) -> McpConfigError | None:
    """Check that the command for a stdio server exists in PATH.

    Args:
        config: Server config to validate.

    Returns:
        Error if the command is not found, None otherwise.
    """
    if config.transport != "stdio":
        return None
    if not config.command:
        return McpConfigError(
            server_name=config.name,
            check="command_missing",
            message="No command specified for stdio transport",
        )
    executable = config.command[0]
    if shutil.which(executable) is None:
        return McpConfigError(
            server_name=config.name,
            check="command_not_found",
            message=f"Command {executable!r} not found in PATH",
        )
    return None


def check_env_vars(config: MCPServerConfig) -> list[McpConfigError]:
    """Check that all env vars referenced in the config are available.

    Env vars whose values look like ``${VAR_NAME}`` are treated as
    references to environment variables that must be set.

    Args:
        config: Server config to validate.

    Returns:
        List of errors for missing env vars.
    """
    errors: list[McpConfigError] = []
    for var_name, var_value in config.env.items():
        # If the value is a reference like "${GITHUB_TOKEN}", check the env
        if var_value.startswith("${") and var_value.endswith("}"):
            ref_name = var_value[2:-1]
            if not os.environ.get(ref_name):
                errors.append(
                    McpConfigError(
                        server_name=config.name,
                        check="env_var_missing",
                        message=f"Environment variable {ref_name!r} is not set (referenced by {var_name!r})",
                    )
                )
    return errors


def check_url_reachable(
    config: MCPServerConfig,
    *,
    timeout: float = 5.0,
) -> McpConfigError | None:
    """Check that the URL for an SSE/HTTP server is reachable.

    Performs a HEAD request with a short timeout.  Only runs for
    non-stdio transports with a URL.

    Args:
        config: Server config to validate.
        timeout: HTTP timeout in seconds.

    Returns:
        Error if the URL is unreachable, None otherwise.
    """
    if config.transport == "stdio":
        return None
    if not config.url:
        return McpConfigError(
            server_name=config.name,
            check="url_missing",
            message=f"No URL specified for {config.transport!r} transport",
        )
    try:
        import urllib.request

        req = urllib.request.Request(config.url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except Exception as exc:
        return McpConfigError(
            server_name=config.name,
            check="url_unreachable",
            message=f"URL {config.url!r} is not reachable: {exc}",
        )
    return None


# ---------------------------------------------------------------------------
# Aggregate validation
# ---------------------------------------------------------------------------


def validate_mcp_configs(
    configs: list[MCPServerConfig],
    *,
    check_urls: bool = True,
    url_timeout: float = 5.0,
) -> list[McpConfigError]:
    """Validate all MCP server configs and return any errors found.

    Runs all checks for each config:
    - Command exists in PATH (stdio only)
    - Required env vars are set
    - URL is reachable (sse/streamable_http only, skippable)

    Args:
        configs: List of server configurations to validate.
        check_urls: Whether to perform URL reachability checks.
        url_timeout: Timeout for URL checks in seconds.

    Returns:
        List of all errors found across all configs.
    """
    all_errors: list[McpConfigError] = []

    for config in configs:
        # Command check
        cmd_err = check_command_exists(config)
        if cmd_err is not None:
            all_errors.append(cmd_err)
            logger.warning("%s", cmd_err)

        # Env var check
        env_errs = check_env_vars(config)
        for err in env_errs:
            all_errors.append(err)
            logger.warning("%s", err)

        # URL reachability check
        if check_urls:
            url_err = check_url_reachable(config, timeout=url_timeout)
            if url_err is not None:
                all_errors.append(url_err)
                logger.warning("%s", url_err)

    if all_errors:
        logger.warning(
            "MCP config validation found %d error(s) across %d server(s)",
            len(all_errors),
            len(configs),
        )
    else:
        logger.info("MCP config validation passed for %d server(s)", len(configs))

    return all_errors
