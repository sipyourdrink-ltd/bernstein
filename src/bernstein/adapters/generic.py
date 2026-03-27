"""Generic CLI adapter for arbitrary coding agent CLIs."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ModelConfig


class GenericAdapter(CLIAdapter):
    """Spawn and monitor an arbitrary CLI coding agent.

    The CLI command and argument patterns are provided at construction time,
    making this adapter work with any command-line agent.

    Args:
        cli_command: The base command to invoke (e.g. "aider", "cursor").
        prompt_flag: Flag to pass the prompt (e.g. "--message", "-p").
        model_flag: Flag to pass the model name (e.g. "--model"). None to omit.
        extra_args: Additional fixed arguments to include in every invocation.
        display_name: Human-readable name for this adapter.
    """

    def __init__(
        self,
        *,
        cli_command: str,
        prompt_flag: str = "--prompt",
        model_flag: str | None = "--model",
        extra_args: list[str] | None = None,
        display_name: str = "Generic CLI",
    ) -> None:
        self._cli_command = cli_command
        self._prompt_flag = prompt_flag
        self._model_flag = model_flag
        self._extra_args = extra_args or []
        self._display_name = display_name

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

        cmd = [self._cli_command]
        if self._model_flag is not None:
            cmd.extend([self._model_flag, model_config.model])
        cmd.extend(self._extra_args)
        cmd.extend([self._prompt_flag, prompt])

        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
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
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass

    def name(self) -> str:
        return self._display_name
