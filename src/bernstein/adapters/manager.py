from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.platform_compat import kill_process_group, process_alive


class ManagerAdapter(CLIAdapter):
    """Spawns the internal Python ManagerAgent."""

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
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        env = build_filtered_env(["ANTHROPIC_API_KEY"])

        # Extract the task ID. The ManagerAgent __main__ expects --task-id
        # We know tasks are passed in the prompt, let's grab the first task id.
        import re

        task_match = re.search(r"\(id=([^\)]+)\)", prompt)
        task_id = task_match.group(1) if task_match else "task-000"

        cmd = [
            sys.executable,
            "-m",
            "bernstein.core.orchestration.manager",
            "--port",
            "8052",
            "--task-id",
            task_id,
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role="manager",
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        preexec_fn = self._get_preexec_fn()
        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                wrapped_cmd,
                cwd=workdir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=preexec_fn,
            )

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def is_alive(self, pid: int) -> bool:
        return process_alive(pid)

    def kill(self, pid: int) -> None:
        kill_process_group(pid, sig=15)

    def name(self) -> str:
        return "Internal Manager"
