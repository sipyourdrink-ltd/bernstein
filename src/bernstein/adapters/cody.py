"""Sourcegraph Cody CLI adapter.

Cody (https://sourcegraph.com/cody) is Sourcegraph's AI coding assistant.
This adapter drives the standalone Cody CLI (``cody``) in non-interactive
chat mode.

Installation: ``npm install -g @sourcegraph/cody``
Authentication: ``SRC_ACCESS_TOKEN`` and ``SRC_ENDPOINT`` environment
variables, or ``cody auth login``.
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

# Map Bernstein logical model names to Cody model identifiers.
# Cody uses Sourcegraph model IDs in the form ``provider::version::model``.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic::2025-05-14::claude-opus-4-6",
    "sonnet": "anthropic::2025-05-14::claude-sonnet-4-6",
    "haiku": "anthropic::2024-10-22::claude-haiku-4-5-20251001",
    "gpt-5.4": "openai::2026-03-05::gpt-5.4",
    "gpt-5.4-mini": "openai::2026-03-05::gpt-5.4-mini",
    "gemini-pro": "google::v1::gemini-3.1-pro",
}


class CodyAdapter(CLIAdapter):
    """Spawn and monitor Sourcegraph Cody CLI sessions.

    Cody is invoked via ``cody chat -m <prompt>`` in non-interactive mode.
    Authentication is read from ``SRC_ACCESS_TOKEN`` and ``SRC_ENDPOINT``
    environment variables, or from credentials saved by ``cody auth login``.
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
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        """Launch a Cody agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings chosen by the orchestrator.
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP config (not supported by Cody CLI).
            timeout_seconds: Hard kill timeout in seconds.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If ``cody`` is not found in PATH.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if not os.environ.get("SRC_ACCESS_TOKEN"):
            logger.warning(
                "CodyAdapter: SRC_ACCESS_TOKEN not set — spawn may fail. "
                "Run 'cody auth login' or set SRC_ACCESS_TOKEN and SRC_ENDPOINT."
            )
        if mcp_config:
            logger.debug("CodyAdapter ignoring runtime MCP config for session %s", session_id)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "cody",
            "chat",
            "-m",
            prompt,
        ]
        if model_id:
            cmd.extend(["--model", model_id])

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        env = build_filtered_env(
            [
                "SRC_ACCESS_TOKEN",
                "SRC_ENDPOINT",
                "SRC_HEADER_AUTHORIZATION",
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
                raise RuntimeError("cody not found in PATH. Install with: npm install -g @sourcegraph/cody") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing cody: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "Cody"
