"""Sourcegraph Cody CLI adapter.

Cody (https://sourcegraph.com/cody) is Sourcegraph's AI coding assistant.
This adapter drives Cody via the Sourcegraph CLI (``sg cody``), which supports
non-interactive chat sessions suitable for batch task execution.

Installation: https://github.com/sourcegraph/sourcegraph/tree/main/dev/sg
Authentication: ``SRC_ACCESS_TOKEN`` and ``SRC_ENDPOINT`` environment variables.
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
    "opus": "anthropic::2024-10-22::claude-opus-4-5",
    "sonnet": "anthropic::2024-10-22::claude-sonnet-4-5",
    "haiku": "anthropic::2024-10-22::claude-haiku-4-5",
    "gpt-4o": "openai::2024-11-20::gpt-4o",
    "gpt-4.1": "openai::2025-04-14::gpt-4.1",
    "gemini-pro": "google::v1::gemini-2.0-flash-exp",
}

# Default Sourcegraph endpoint — override with SRC_ENDPOINT for self-hosted
_DEFAULT_SRC_ENDPOINT = "https://sourcegraph.com"


class CodyAdapter(CLIAdapter):
    """Spawn and monitor Sourcegraph Cody CLI sessions.

    Cody is invoked via the ``sg cody chat`` sub-command in non-interactive
    mode. Authentication is read from ``SRC_ACCESS_TOKEN`` and
    ``SRC_ENDPOINT`` environment variables (or ``~/.config/sg/sg.config.yaml``
    after running ``sg auth login``).
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
            RuntimeError: If ``sg`` is not found in PATH.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if not os.environ.get("SRC_ACCESS_TOKEN"):
            logger.warning(
                "CodyAdapter: SRC_ACCESS_TOKEN not set — spawn may fail. "
                "Run 'sg auth login' or set SRC_ACCESS_TOKEN and SRC_ENDPOINT."
            )
        if mcp_config:
            logger.debug("CodyAdapter ignoring runtime MCP config for session %s", session_id)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "sg",
            "cody",
            "chat",
            "--stdin",
            # Sourcegraph's sg cody chat reads the prompt from stdin when --stdin is set.
            # Alternatively: --message flag on newer sg versions.
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
                    stdin=subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "sg not found in PATH. Install the Sourcegraph CLI: "
                    "https://github.com/sourcegraph/sourcegraph/tree/main/dev/sg"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing sg: {exc}") from exc

        # Write the prompt to stdin and close the pipe so the process can start.
        if proc.stdin:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "Cody"
