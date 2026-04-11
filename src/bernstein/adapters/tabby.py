"""Tabby self-hosted coding agent adapter.

Tabby (https://tabby.tabbyml.com/) is an open-source, self-hosted AI coding
assistant. This adapter drives Tabby's agent endpoint via the ``tabby-agent``
CLI, which supports non-interactive task completion against a running Tabby
server.

Installation: npm install -g @tabbyml/tabby-agent
Server: Run ``tabby serve --device cuda --model TabbyML/StarCoder-1B`` first.
Configuration: ``TABBY_SERVER_URL`` (default: http://127.0.0.1:8080).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Default Tabby server URL; override with TABBY_SERVER_URL env var.
_DEFAULT_TABBY_URL = "http://127.0.0.1:8080"


class TabbyAdapter(CLIAdapter):
    """Spawn and monitor Tabby agent CLI sessions.

    Requires a running Tabby server (``tabby serve``) and the ``tabby-agent``
    CLI installed. The agent connects to the server at ``TABBY_SERVER_URL``
    and executes tasks against the configured model.

    Model selection is controlled by the Tabby server configuration; the
    ``model_config.model`` field is logged for observability but not passed
    as a flag (Tabby Agent uses the server's active model).
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
    ) -> SpawnResult:
        """Launch a Tabby agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings (model name logged only).
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP config (not supported by tabby-agent).
            timeout_seconds: Hard kill timeout in seconds.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If ``tabby-agent`` is not found in PATH.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        server_url = os.environ.get("TABBY_SERVER_URL", _DEFAULT_TABBY_URL)

        if model_config.model:
            logger.info(
                "TabbyAdapter: requested model %s for session %s; "
                "model selection is controlled by the Tabby server config",
                model_config.model,
                session_id,
            )
        if mcp_config:
            logger.debug("TabbyAdapter ignoring runtime MCP config for session %s", session_id)

        cmd = [
            "tabby-agent",
            "chat",
            "--server",
            server_url,
            "--message",
            prompt,
        ]

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(
            [
                "TABBY_SERVER_URL",
                "TABBY_TOKEN",
            ]
        )

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "tabby-agent not found in PATH. Install with: npm install -g @tabbyml/tabby-agent\n"
                    "Also ensure a Tabby server is running (tabby serve --device auto --model TabbyML/StarCoder-7B)"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing tabby-agent: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "Tabby"
