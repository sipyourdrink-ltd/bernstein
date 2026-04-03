"""Goose CLI adapter for Bernstein.

Adapter for Block's Goose (https://github.com/block/goose).
Goose is an AI agent that can execute tasks autonomously.
This adapter allows Bernstein to orchestrate Goose as a worker agent.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    build_worker_cmd,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Model mapping: Bernstein logical names → Goose model IDs
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-5",
    "sonnet": "claude-sonnet-4-5",
    "haiku": "claude-haiku-3-5",
}


class GooseAdapter(CLIAdapter):
    """Goose CLI adapter for Bernstein.

    Integrates with Block's Goose CLI agent.
    GitHub: https://github.com/block/goose
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
    ) -> SpawnResult:
        """Launch a Goose agent process.

        Args:
            prompt: Task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings chosen by the orchestrator.
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP server configuration (ignored by Goose).
            timeout_seconds: Hard kill timeout in seconds.

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If the Goose binary is not found.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = ["goose", "run", "--instruction", prompt]
        if model_id:
            cmd += ["--model", model_id]

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("goose not found in PATH. Install: https://github.com/block/goose") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing goose: {exc}") from exc

        timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc, timeout_timer=timer)

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "goose"

    def get_version(self) -> str | None:
        """Return the Goose CLI version string, or None if unavailable."""
        try:
            result = subprocess.run(
                ["goose", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def is_available(self) -> bool:
        """Return True if the Goose CLI is installed and accessible."""
        try:
            result = subprocess.run(
                ["goose", "--help"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False
