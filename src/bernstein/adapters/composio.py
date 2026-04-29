"""Composio Agent Orchestrator (``ao``) CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class ComposioAdapter(CLIAdapter):
    """Spawn and monitor Composio Agent Orchestrator (``ao``) sessions.

    This adapter wraps Composio's ``@aoagents/ao`` as a single Bernstein
    agent. The wrapped orchestrator runs its own internal multi-agent
    workflow inside a tmux session — Bernstein only observes the final
    exit code and the captured log output. This is leaf-node delegation,
    not deep meta-orchestration: the cost, quality gates, and routing
    decisions made by Composio's sub-agents are not visible to
    Bernstein's accounting or policy layers.

    The CLI is invoked as ``ao spawn --prompt <text>``. ``ao spawn``
    is documented as the non-interactive entry point that spawns a
    single agent session for a free-form prompt (without requiring a
    GitHub issue). The companion ``ao start`` command is interactive
    and launches the dashboard, so it is not used here.
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
        """Launch a Composio Agent Orchestrator session.

        Args:
            prompt: Free-form instructions delivered via ``--prompt``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration (retained for
                interface compatibility; Composio routes to its own
                configured sub-agent plugins internally).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused; Composio
                manages its own plugin configuration via ``ao start``).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``ao`` binary is missing from PATH or
                cannot be executed.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["ao", "spawn", "--prompt", prompt]

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

        # Composio inherits credentials from the underlying agent plugin
        # (codex, claude-code, etc.), so we pass through the common keys.
        env = build_filtered_env(
            [
                "COMPOSIO_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "AO_PROJECT_ID",
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
                msg = "ao not found in PATH. Install: npm install -g @aoagents/ao"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing ao: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Composio Agent Orchestrator"
