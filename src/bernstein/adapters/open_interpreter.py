"""Open Interpreter CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class OpenInterpreterAdapter(CLIAdapter):
    """Spawn and monitor Open Interpreter CLI sessions.

    The CLI is invoked as ``interpreter -y --model <model> "<prompt>"``.
    Open Interpreter takes the prompt as a positional argument (joined from
    ``sys.argv[1:]`` when the first token does not start with ``-``).

    The ``-y`` flag (long form ``--auto_run``) is mandatory for headless
    operation: without it, the interpreter pauses and asks the user to
    confirm every code execution, so the spawned subprocess hangs forever
    waiting on stdin. With ``-y`` the agent auto-approves its own code
    execution and runs to completion.

    Open Interpreter executes shell commands and arbitrary code on the
    host machine, which is the whole point of the tool. Bernstein already
    runs each spawned agent inside its own git worktree, so filesystem
    isolation is handled at the orchestrator layer; the adapter does not
    add a sandbox of its own.
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
        """Launch an Open Interpreter CLI session.

        Args:
            prompt: The initial prompt supplied as a positional argument.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration. ``model`` is
                forwarded via ``--model`` so LiteLLM (which Open Interpreter
                uses internally) can resolve the provider.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``interpreter`` binary is missing from
                PATH or cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["interpreter", "-y", "--model", model_config.model, prompt]

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

        env = build_filtered_env(["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
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
                msg = "interpreter not found in PATH. Install: pip install open-interpreter"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing interpreter: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Open Interpreter"
