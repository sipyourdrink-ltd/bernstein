"""{{ cookiecutter.adapter_name }} CLI adapter for Bernstein."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import SystemAddendumChannel
from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    append_system_addendum,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class {{ cookiecutter.adapter_class }}(CLIAdapter):
    """{{ cookiecutter.description }}

    Args:
        cli_path: Executable for the target CLI.
    """

    def __init__(self, *, cli_path: str = "{{ cookiecutter.adapter_name }}") -> None:
        super().__init__()
        self._cli_path = cli_path

    def name(self) -> str:
        """Return the adapter's display name."""
        return "{{ cookiecutter.adapter_name }}"

    def system_addendum_channel(self) -> SystemAddendumChannel:
        """Deliver protocol instructions by appending them to the prompt."""
        return SystemAddendumChannel.PROMPT_APPEND

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
        """Launch the CLI and return its handle without waiting for completion.

        Adapt the command flags to your CLI. MCP configuration and scope-based
        budgets require CLI-specific support; this template does not wire them.
        Launch and log-file errors propagate to the caller.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        cmd = [self._cli_path, "run", "--description", append_system_addendum(prompt, system_addendum)]
        if model_config.model:
            cmd.extend(["--model", model_config.model])
        if model_config.effort:
            cmd.extend(["--effort", model_config.effort])

        log_path = workdir / f".bernstein-{session_id}.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
                preexec_fn=self._get_preexec_fn(),
            )

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def detect_tier(self) -> Any | None:
        """Detect the pricing tier; implement this for your target CLI."""
        return None
