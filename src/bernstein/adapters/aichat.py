"""AIChat CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class AIChatAdapter(CLIAdapter):
    """Spawn and monitor AIChat (https://github.com/sigoden/aichat) sessions.

    The CLI is invoked as ``aichat -m <model> -- <prompt>`` where ``-m``
    selects the underlying LLM (e.g. ``openai:gpt-4o``, ``claude:...``)
    and the prompt is passed as a positional argument; aichat exits after
    emitting the response.

    AIChat is a thinner LLM CLI than coding-specific agents like Aider or
    Claude Code: it does not have built-in repo navigation, multi-file
    editing, or autonomous tool loops. It does support user-defined
    function/tool calling (``-f``) and has ``-c/--code`` (output code only)
    and ``-e/--execute`` (translate prompt to a shell command) modes.
    Bernstein wraps plain chat mode for tasks where a lightweight,
    one-shot model invocation suffices (e.g. quick rewrites, summaries,
    classification) and richer agentic behavior is not required.
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
        """Launch an AIChat session.

        Args:
            prompt: The prompt passed positionally after ``--``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration; ``model`` is
                forwarded to aichat via ``-m`` (e.g. ``openai:gpt-4o``).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused; aichat
                has its own function/tool registry).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by aichat).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused;
                aichat has no system-prompt flag for one-shot mode).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``aichat`` binary is missing from PATH
                or cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["aichat", "-m", model_config.model, "--", prompt]

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
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENROUTER_API_KEY",
                "GROQ_API_KEY",
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
                msg = "aichat not found in PATH. Install: cargo install aichat (or `brew install aichat`)"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing aichat: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "AIChat"
