"""Trae Agent CLI adapter (ByteDance).

`Trae Agent <https://github.com/bytedance/trae-agent>`_ is ByteDance's
open-source LLM agent for general software-engineering tasks. Its CLI is
the ``trae-cli`` binary; the ``run`` subcommand executes a single task
non-interactively and exits, which is the primary path for CI and
automation:

    trae-cli run "<prompt>"

The agent runs autonomously (it plans and executes tool calls without an
interactive permission gate), so no skip-permission flag is required. The
working directory is the spawn cwd; ``trae-cli run`` defaults to the
current directory, so Bernstein does not pass ``--working-dir`` and lets
the child inherit the worktree cwd.

Provider selection and API keys come from the Trae config file and env
(``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GOOGLE_API_KEY`` /
``OPENROUTER_API_KEY``), which the adapter forwards.

Last verified against https://github.com/bytedance/trae-agent and
https://deepwiki.com/bytedance/trae-agent/3.1-cli-commands on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class TraeAdapter(CLIAdapter):
    """Spawn and monitor ByteDance Trae Agent (``trae-cli``) sessions.

    ``trae-cli run`` is a one-shot autonomous task runner; Bernstein spawns
    it in the worktree cwd and reads its output from the captured log.
    """

    registry_name = "trae"

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
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch a one-shot Trae Agent task.

        Args:
            prompt: Task prompt passed positionally to ``trae-cli run``.
            workdir: Working directory; Trae treats the spawn cwd as the
                project root.
            model_config: Model selection (Trae resolves provider/model
                from its config, so it is carried for metadata only).
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused by Trae).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by Trae).
            budget_multiplier: Scope-budget multiplier (unused by Trae).
            system_addendum: Protocol-critical instructions (unused by Trae).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``trae-cli`` binary is missing from PATH or
                the current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["trae-cli", "run", prompt]

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
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "GOOGLE_API_KEY",
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
                msg = "trae-cli not found in PATH. Install: pip install trae-agent (see https://github.com/bytedance/trae-agent)"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing trae-cli: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Trae"
