"""{{ cookiecutter.adapter_name }} CLI adapter for Bernstein."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import CLIAdapter, SpawnResult

logger = logging.getLogger(__name__)


class {{ cookiecutter.adapter_class }}(CLIAdapter):
    """{{ cookiecutter.adapter_name }} CLI adapter.

    {{ cookiecutter.description }}

    Args:
        workdir: Project working directory.
        session_id: Unique agent session identifier.
        **kwargs: Additional adapter configuration.
    """

    name = "{{ cookiecutter.adapter_name }}"

    def __init__(
        self,
        workdir: Path,
        session_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(workdir, session_id, **kwargs)
        self._cli_path = kwargs.get("cli_path", "{{ cookiecutter.adapter_name }}")

    def spawn(
        self,
        task_description: str,
        files: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout: int | None = None,
    ) -> SpawnResult:
        """Spawn {{ cookiecutter.adapter_name }} agent for a task.

        Args:
            task_description: Task description to execute.
            files: Optional list of files to include in context.
            model: Optional model override.
            effort: Optional effort level override.
            timeout: Optional timeout in seconds.

        Returns:
            SpawnResult with agent PID and session info.
        """
        # Build command
        cmd = [self._cli_path, "run"]

        # Add task description
        cmd.extend(["--description", task_description])

        # Add files if provided
        if files:
            for f in files:
                cmd.extend(["--file", f])

        # Add model if provided
        if model:
            cmd.extend(["--model", model])

        # Add effort if provided
        if effort:
            cmd.extend(["--effort", effort])

        # Set timeout
        if timeout is None:
            timeout = self._default_timeout

        logger.info("Spawning %s agent for task", self.name)
        logger.debug("Command: %s", " ".join(cmd))

        # Spawn process
        # Note: In production, use proper process management
        # This is a template - adapt to your adapter's actual CLI
        try:
            result = subprocess.run(
                cmd,
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

            return SpawnResult(
                success=result.returncode == 0,
                pid=None,  # Template - replace with actual PID tracking
                output=result.stdout,
                error=result.stderr,
            )
        except subprocess.TimeoutExpired:
            logger.warning("%s agent timed out after %ds", self.name, timeout)
            return SpawnResult(
                success=False,
                pid=None,
                output="",
                error=f"Timeout after {timeout}s",
            )
        except Exception as exc:
            logger.error("%s agent spawn failed: %s", self.name, exc)
            return SpawnResult(
                success=False,
                pid=None,
                output="",
                error=str(exc),
            )

    def detect_tier(self) -> Any | None:
        """Detect the pricing tier for this adapter.

        Returns:
            Tier information or None if unavailable.
        """
        # Template - implement based on your adapter's tier detection
        # Return something like:
        # return TierInfo(
        #     tier=Tier.STANDARD,
        #     is_active=True,
        #     rate_limit=RateLimit(requests_per_minute=60),
        # )
        return None
