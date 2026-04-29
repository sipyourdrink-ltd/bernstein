"""gptme CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


# Map Bernstein short model names to gptme model identifiers.
# gptme accepts provider-prefixed names (e.g. "anthropic/claude-sonnet-4-6",
# "openai/gpt-5.4"). Unknown names pass through unchanged.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic/claude-opus-4-7",
    "opus-4-6": "anthropic/claude-opus-4-6",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
}


class GptmeAdapter(CLIAdapter):
    """Spawn and monitor gptme CLI sessions.

    The CLI is invoked as ``gptme -n -m <model> <prompt>`` where ``-n`` runs
    non-interactively (implies ``--no-confirm`` and exits when the prompt is
    complete) and ``-m`` selects the model.

    gptme is a general-purpose terminal agent with code, shell, and browser
    tools. Bernstein invokes it for coding tasks; the browser tooling is left
    available but unused by the orchestrator's coding workflow.
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
        """Launch a gptme CLI session.

        Args:
            prompt: The initial prompt passed as a positional argument.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration; ``model`` is mapped
                to a provider-prefixed gptme model id.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by gptme).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``gptme`` binary is missing from PATH or
                cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = ["gptme", "-n", "-m", model_id, prompt]

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

        env = build_filtered_env(["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"])
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
                msg = "gptme not found in PATH. Install: pipx install gptme"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing gptme: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "gptme"
