"""Generic CLI adapter for arbitrary coding agent CLIs."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path

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
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [self._cli_command]
        if self._model_flag is not None:
            cmd.extend([self._model_flag, model_config.model])
        cmd.extend(self._extra_args)
        cmd.extend([self._prompt_flag, prompt])

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"{self._cli_command!r} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {self._cli_command!r}: {exc}") from exc

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
        return self._display_name
