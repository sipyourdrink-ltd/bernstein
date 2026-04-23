"""Cline CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class ClineAdapter(CLIAdapter):
    """Spawn and monitor Cline CLI sessions.

    Cline is a CLI coding agent invoked directly as ``cline``.  The
    ``--yolo`` flag enables auto-approval for tool invocations so sessions
    can run unattended.

    See: https://docs.cline.bot/cline-cli/overview
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
        """Spawn a Cline CLI session.

        Args:
            prompt: Task prompt passed positionally to ``cline``.
            workdir: Project working directory.
            model_config: Model and effort configuration (passed through as
                metadata only; Cline selects its own model).
            session_id: Unique session identifier used for log/pid metadata.
            mcp_config: Unused; accepted for interface compatibility.
            timeout_seconds: Watchdog timeout for the spawned process.
            task_scope: Unused; accepted for interface compatibility.
            budget_multiplier: Unused; accepted for interface compatibility.
            system_addendum: Unused; accepted for interface compatibility.

        Returns:
            SpawnResult describing the launched worker process.

        Raises:
            RuntimeError: If ``cline`` is not installed or is not executable.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["cline", "--yolo", prompt]

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
            ["CLINE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"],
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
                msg = (
                    "cline not found in PATH. Install with: npm install -g cline "
                    "(see https://docs.cline.bot/cline-cli/overview)"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing cline: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Cline"
