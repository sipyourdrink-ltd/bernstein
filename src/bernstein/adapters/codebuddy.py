"""CodeBuddy CLI adapter (Tencent Cloud Code Assistant).

`CodeBuddy <https://www.codebuddy.ai/docs/cli/headless>`_ is Tencent
Cloud's terminal coding agent (binary ``codebuddy``, shorthand ``cbc``).
Its headless surface mirrors the Claude Code family:

    codebuddy -p "<prompt>" --model <id> --output-format stream-json \
        --dangerously-skip-permissions

``-p`` runs non-interactively and prints the final result;
``--dangerously-skip-permissions`` (short ``-y``) is required in
non-interactive mode so tool executions that need authorization (file
read/write, command execution, network) proceed without a prompt;
``--output-format stream-json`` emits newline-delimited JSON events.

Auth is by ``CODEBUDDY_API_KEY``; CodeBuddy also honours the underlying
provider keys (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``) for BYO-model
routing.

Last verified against https://www.codebuddy.ai/docs/cli/headless and
https://www.codebuddy.ai/docs/cli/common-workflows on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class CodebuddyAdapter(CLIAdapter):
    """Spawn and monitor Tencent CodeBuddy CLI sessions.

    CodeBuddy is a Claude-Code-shaped CLI: ``-p`` for headless print mode
    and ``--dangerously-skip-permissions`` to run unattended. Bernstein
    drives each session fresh and reads the stream-json result from the
    captured log.
    """

    registry_name = "codebuddy"

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
        """Launch a one-shot CodeBuddy headless session.

        Args:
            prompt: Task prompt passed via ``-p``.
            workdir: Working directory for the agent process.
            model_config: Model selection forwarded via ``--model``.
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused; CodeBuddy
                has its own MCP wiring).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by CodeBuddy).
            budget_multiplier: Scope-budget multiplier (unused by CodeBuddy).
            system_addendum: Protocol-critical instructions (unused by CodeBuddy).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``codebuddy`` binary is missing from PATH or
                the current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "codebuddy",
            "-p",
            prompt,
        ]
        if model_config.model:
            cmd.extend(["--model", model_config.model])
        cmd.extend(
            [
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",  # required for non-interactive runs
            ]
        )

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

        env = build_filtered_env(["CODEBUDDY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
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
                msg = "codebuddy not found in PATH. Install: see https://www.codebuddy.ai/docs/cli"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing codebuddy: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "CodeBuddy"
