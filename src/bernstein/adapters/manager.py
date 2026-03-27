from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult


class ManagerAdapter(CLIAdapter):
    """Spawns the internal Python ManagerAgent."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()

        # Extract the task ID. The ManagerAgent __main__ expects --task-id
        # We know tasks are passed in the prompt, let's grab the first task id.
        import re
        task_match = re.search(r"\(id=([^\)]+)\)", prompt)
        task_id = task_match.group(1) if task_match else "task-000"

        cmd = [
            sys.executable,
            "-m",
            "bernstein.core.manager",
            "--port", "8052",
            "--task-id", task_id,
        ]

        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        return SpawnResult(pid=proc.pid, log_path=log_path)

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, pid: int) -> None:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)

    def name(self) -> str:
        return "Internal Manager"
