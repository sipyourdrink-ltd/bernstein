"""Qoder CLI adapter (Alibaba Qoder).

`Qoder <https://qoder.com/en/cli>`_ is Alibaba's agentic coding platform.
Its command-line client ships as the ``qodercli`` binary and exposes a
headless surface via ``-p``:

    qodercli -p "<prompt>"

In headless mode the CLI runs prompt-type commands (file editing, command
execution, creating commits) without the interactive TUI, so the process
exits when the task is done. TUI-only commands are unavailable headless.

Auth for non-interactive terminals is by a personal access token obtained
from the Qoder Integrations page; the adapter forwards ``QODER_API_KEY``
and ``DASHSCOPE_API_KEY`` (Alibaba Cloud Model Studio) so a routed run can
authenticate without an interactive login. Qoder resolves the model from
its own ``/model`` configuration, so no ``--model`` flag is passed.

Last verified against https://docs.qoder.com/en/cli/command and
https://qoder.com/en/cli on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class QoderAdapter(CLIAdapter):
    """Spawn and monitor Alibaba Qoder CLI (``qodercli``) sessions.

    Qoder's headless surface is a single ``-p`` prompt flag; the process
    runs the prompt-type command non-interactively and exits. Bernstein
    spawns each session fresh and reads its output from the captured log.
    """

    registry_name = "qoder"

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
        """Launch a one-shot Qoder headless session.

        Args:
            prompt: Task prompt passed via ``-p``.
            workdir: Working directory for the agent process (Qoder treats
                the spawn cwd as the project root).
            model_config: Model selection (Qoder resolves the model from
                its own config, so it is carried for metadata only).
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused; Qoder has
                its own MCP wiring).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by Qoder).
            budget_multiplier: Scope-budget multiplier (unused by Qoder).
            system_addendum: Protocol-critical instructions (unused by Qoder).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``qodercli`` binary is missing from PATH or
                the current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["qodercli", "-p", prompt]

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

        env = build_filtered_env(["QODER_API_KEY", "DASHSCOPE_API_KEY"])
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
                msg = "qodercli not found in PATH. Install: see https://qoder.com/en/cli"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing qodercli: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Qoder"
