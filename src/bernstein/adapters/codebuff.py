"""Codebuff CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class CodebuffAdapter(CLIAdapter):
    """Spawn and monitor Codebuff CLI sessions.

    Codebuff is an npm-distributed CLI coding agent. The CLI accepts a
    task prompt as a single positional argument and has no dedicated
    auto-approve or headless flag (see
    https://www.codebuff.com/docs/help/quick-start).
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
        """Launch a Codebuff session.

        Args:
            prompt: Task prompt passed as the single positional argument.
            workdir: Working directory for the agent process.
            model_config: Model configuration (unused; Codebuff selects
                its own model via its own config).
            session_id: Unique session identifier used for the log file.
            mcp_config: Unused. Codebuff manages its own integrations.
            timeout_seconds: Watchdog timeout in seconds.
            task_scope: Unused scope hint.
            budget_multiplier: Unused budget multiplier.
            system_addendum: Unused system-prompt addendum.

        Returns:
            :class:`SpawnResult` describing the launched process.

        Raises:
            RuntimeError: If the ``codebuff`` binary is missing or not
                executable.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["codebuff", prompt]

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

        env = build_filtered_env(["CODEBUFF_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
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
                msg = (
                    "codebuff not found in PATH. Install: npm install -g codebuff "
                    "or see https://www.codebuff.com/docs/help/quick-start"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing codebuff: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Codebuff"
