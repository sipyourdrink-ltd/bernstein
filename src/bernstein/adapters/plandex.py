"""Plandex CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class PlandexAdapter(CLIAdapter):
    """Spawn and monitor Plandex CLI sessions.

    The CLI is invoked as
    ``plandex tell <prompt> --apply --auto-exec --skip-menu --stop`` where:

    * ``tell`` is the non-interactive entry point (the default ``plandex``
      invocation drops into a REPL).
    * ``--apply``/``-a`` auto-applies pending changes to the working tree
      and confirms context updates.
    * ``--auto-exec`` auto-executes any shell commands the plan requests,
      bypassing the per-command approval prompt.
    * ``--skip-menu`` skips the interactive "what next?" menu Plandex shows
      after each response.
    * ``--stop``/``-s`` halts after a single response so the subprocess
      exits instead of waiting for follow-up turns.

    Without these flags Plandex will block forever on its interactive
    approval gates and the spawned process will never complete.

    Plandex is plan-first: it builds an internal plan, then executes it
    incrementally with build/apply phases.  Bernstein wraps the entire
    plan-and-execute loop as a single short-lived agent.

    Note: Plandex uses a client-server architecture.  The CLI connects to
    Plandex Cloud or a self-hosted server (default ``http://localhost:8099``,
    started via ``./start_local.sh``).  When no server is reachable the
    spawned ``plandex`` process exits with a connection error; that surface
    is handled by the standard early-exit path rather than via a pre-flight
    probe in this adapter.
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
        """Launch a Plandex CLI session.

        Args:
            prompt: The task prompt passed as a positional argument to
                ``plandex tell``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration (retained for
                interface compatibility; Plandex selects models via its
                own ``set-model`` configuration and provider env vars).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by Plandex).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused;
                Plandex does not expose a system-prompt channel).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``plandex`` binary is missing from PATH
                or cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "plandex",
            "tell",
            prompt,
            "--apply",
            "--auto-exec",
            "--skip-menu",
            "--stop",
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
                "PLANDEX_API_KEY",
                "PLANDEX_ENV",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENROUTER_API_KEY",
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
                msg = "plandex not found in PATH. Install: curl -sL https://plandex.ai/install.sh | bash"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing plandex: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Plandex"
