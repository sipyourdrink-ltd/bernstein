"""Continue.dev CLI adapter.

Continue (https://www.continue.dev/) is an open-source AI coding assistant
for VS Code and JetBrains. This adapter invokes the Continue CLI (``cn``)
in headless mode for non-interactive task execution.

Installation: ``npm install -g @continuedev/cli`` (binary is ``cn``).
Authentication: ``~/.continue/config.yaml`` or provider API keys in the env.
Model selection is driven by the Continue config; ``cn`` has no CLI flag for
per-invocation model override, so ``model_config.model`` is logged only.
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


class ContinueDevAdapter(CLIAdapter):
    """Spawn and monitor Continue.dev CLI sessions.

    Uses the Continue CLI binary ``cn`` with the ``-p`` (headless prompt) flag
    to run coding tasks non-interactively. Model and context are configured
    through ``~/.continue/config.yaml``.

    Provider API keys (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, etc.) are
    forwarded so Continue can authenticate against its configured model
    providers.
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
        """Launch a Continue.dev agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings (model name logged only;
                Continue selects the model from its config file).
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP config (ignored; Continue manages MCP
                through config.yaml).
            timeout_seconds: Hard kill timeout in seconds.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If the ``cn`` binary is not found in PATH.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if mcp_config:
            logger.debug("ContinueDevAdapter ignoring runtime MCP config for session %s", session_id)
        if model_config.model:
            logger.info(
                "ContinueDevAdapter: requested model %s for session %s; "
                "Continue CLI selects the model from ~/.continue/config.yaml",
                model_config.model,
                session_id,
            )

        cmd = [
            "cn",
            "-p",
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
                    "cn not found in PATH. Install with: npm install -g @continuedev/cli"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing cn: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "Continue.dev"
