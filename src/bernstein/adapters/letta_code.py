"""Letta Code CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class LettaCodeAdapter(CLIAdapter):
    """Spawn and monitor Letta Code CLI sessions.

    The CLI is invoked as ``letta --yolo -p <prompt>`` where ``-p`` runs
    a one-off prompt in headless mode (per
    ``docs.letta.com/letta-code/quickstart`` and ``cli-reference``) and
    ``--yolo`` bypasses most permission prompts so the agent does not
    block waiting on TTY input. The binary ships as ``letta`` from the
    npm package ``@letta-ai/letta-code``; if the documented headless
    flag changes upstream, ``-p`` is the only contract Letta currently
    publishes for non-interactive runs.

    Letta Code's defining feature is *cross-task memory* persisted via
    Letta Cloud (``LETTA_API_KEY``) -- the agent maintains long-lived
    state across separate invocations. Bernstein wraps Letta Code as a
    leaf-node, one-shot agent: each task spawns a fresh ``letta -p``
    process and exits when the prompt completes. Bernstein does not
    coordinate Letta's cross-task memory, agent IDs, or memory blocks;
    that machinery still operates in Letta's own backend, but it is
    opaque to Bernstein's orchestrator. If you want Bernstein-level
    state to survive across tasks, use Bernstein's ``.sdd/`` files,
    not Letta's memory.
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
        """Launch a Letta Code CLI session.

        Args:
            prompt: The headless prompt supplied via ``-p``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration (retained for
                interface compatibility; Letta Code resolves the model
                via ``/connect`` config or ``--model``, not via the
                Bernstein scope mapping).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by Letta Code).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``letta`` binary is missing from PATH
                or cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["letta", "--yolo", "-p", prompt]

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
                "LETTA_API_KEY",
                "LETTA_BASE_URL",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
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
                msg = "letta not found in PATH. Install: npm install -g @letta-ai/letta-code"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing letta: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Letta Code"
