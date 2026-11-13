"""{{ cookiecutter.adapter_name }} CLI adapter for Bernstein.

{{ cookiecutter.description }}

Declare the adapter under the ``bernstein.adapters`` entry-point group and
Bernstein discovers it at runtime:

    [project.entry-points."bernstein.adapters"]
    {{ cookiecutter.adapter_name }} = "{{ cookiecutter.adapter_name }}:{{ cookiecutter.adapter_class }}"

Then ``bernstein run --cli {{ cookiecutter.adapter_name }} plan.yaml`` routes tasks to it.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

#: The executable this adapter launches. Change it to the name of your CLI.
CLI_BINARY = "{{ cookiecutter.adapter_name }}"

#: Environment variables your CLI needs (API keys, endpoints). The agent
#: process gets a filtered environment, so a variable that is not named here
#: and is not in the base allowlist does not reach it.
SECRET_ENV: tuple[str, ...] = ()


class {{ cookiecutter.adapter_class }}(CLIAdapter):
    """Spawn and monitor {{ cookiecutter.adapter_name }} CLI sessions.

    The contract is ``spawn``: launch the CLI without waiting for it, and
    return a ``SpawnResult`` with the process id and the log file. Bernstein
    reads the log and watches the process from there.
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
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch a {{ cookiecutter.adapter_name }} agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory for the agent process.
            model_config: Model and effort settings chosen by the orchestrator.
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP server definitions. Ignored by this template.
            timeout_seconds: Hard kill timeout in seconds. 0 disables it.
            task_scope: Task scope hint. Ignored by this template.
            budget_multiplier: Budget hint. Ignored by this template.
            system_addendum: Extra system text. Ignored by this template.
            multimodal_context: Attachments. Refused unless the adapter is
                registered as multimodal-capable.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If the CLI is not found in PATH or cannot be executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self.build_command(prompt, model_config)
        logger.debug("Spawning %s for session %s", CLI_BINARY, session_id)

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=build_filtered_env(SECRET_ENV),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"{CLI_BINARY} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {CLI_BINARY}: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def build_command(self, prompt: str, model_config: ModelConfig) -> list[str]:
        """Build the argv for one task. Adapt the flags to your CLI.

        Args:
            prompt: Task description passed to the agent.
            model_config: Model and effort settings chosen by the orchestrator.

        Returns:
            The command to execute, binary first.
        """
        cmd = [CLI_BINARY, "run", "--prompt", prompt]
        if model_config.model:
            cmd.extend(["--model", model_config.model])
        return cmd

    def name(self) -> str:
        """Human-readable adapter name shown in ``bernstein ps`` and logs."""
        return "{{ cookiecutter.adapter_class }}"
