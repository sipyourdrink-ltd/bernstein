"""Continue.dev CLI adapter.

Continue (https://www.continue.dev/) is an open-source AI coding assistant
for VS Code and JetBrains. This adapter invokes Continue's headless CLI mode
for non-interactive task execution.

Installation: npm install -g @continuedev/continue-cli
Authentication: ``~/.continue/config.yaml`` or ``CONTINUE_API_KEY`` env var.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Map Bernstein logical model names to Continue provider/model identifiers.
# These follow Continue's ``provider/model`` format used in config.yaml.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic/claude-opus-4-5",
    "sonnet": "anthropic/claude-sonnet-4-5",
    "haiku": "anthropic/claude-haiku-4-5",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4.1": "openai/gpt-4.1",
    "gemini-pro": "google/gemini-2.0-flash",
}


class ContinueDevAdapter(CLIAdapter):
    """Spawn and monitor Continue.dev CLI sessions.

    Uses Continue's headless mode (``continue run --no-interactive``) to
    execute coding tasks in batch. Continue reads its model and context
    configuration from ``~/.continue/config.yaml``.

    The ``CONTINUE_API_KEY`` environment variable is forwarded when present;
    per-provider API keys (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, etc.)
    are also forwarded so Continue can authenticate automatically.
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
        """Launch a Continue.dev agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings chosen by the orchestrator.
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP config (ignored; Continue manages MCP via config.yaml).
            timeout_seconds: Hard kill timeout in seconds.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If the Continue CLI binary is not found.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if mcp_config:
            logger.debug("ContinueDevAdapter ignoring runtime MCP config for session %s", session_id)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "continue",
            "run",
            "--no-interactive",
            "--message",
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
                "CONTINUE_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
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
                    "continue not found in PATH. Install with: npm install -g @continuedev/continue-cli"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing continue: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "Continue.dev"
