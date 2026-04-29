"""OpenHands CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class OpenHandsAdapter(CLIAdapter):
    """Spawn and monitor OpenHands CLI sessions.

    The CLI is invoked as ``openhands --headless --override-with-envs -t
    '<task>'`` where ``--headless`` runs without the interactive TUI,
    ``--override-with-envs`` opts the CLI into honouring ``LLM_API_KEY`` /
    ``LLM_MODEL`` / ``LLM_BASE_URL`` environment variables (otherwise it
    reads ``~/.openhands/agent_settings.json``), and ``-t`` supplies the
    task text. OpenHands is itself a multi-step autonomous agent that
    plans, edits, and runs commands inside the working directory; Bernstein
    wraps it as a single short-lived agent and only observes the final exit
    code via the standard worker watchdog. Persisted settings still live
    under ``~/.openhands/`` regardless of the env override.
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
        """Launch an OpenHands CLI session in headless mode.

        Args:
            prompt: The task text supplied via ``-t``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration (retained for
                interface compatibility; OpenHands resolves the model from
                ``LLM_MODEL`` or its persisted ``agent_settings.json``).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by OpenHands).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``openhands`` binary is missing from PATH
                or cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["openhands", "--headless", "--override-with-envs", "-t", prompt]

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
                "LLM_API_KEY",
                "LLM_MODEL",
                "LLM_BASE_URL",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
            ],
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
                msg = "openhands not found in PATH. Install: uv tool install openhands --python 3.12"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing openhands: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "OpenHands"
