"""Base adapter for CLI coding agents."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ApiTierInfo, ModelConfig


@dataclass
class SpawnResult:
    """Result of spawning an agent process."""

    pid: int
    log_path: Path
    proc: object | None = None  # subprocess.Popen, kept for poll()-based alive check


def build_worker_cmd(
    cmd: list[str],
    *,
    role: str,
    session_id: str,
    pid_dir: Path,
    model: str = "",
) -> list[str]:
    """Wrap a CLI command with bernstein-worker for process visibility.

    The worker sets the process title to "bernstein: <role> [<session>]"
    and writes a PID metadata file for ``bernstein ps``.

    Args:
        cmd: The original CLI command to wrap.
        role: Agent role (qa, backend, etc.).
        session_id: Unique session identifier.
        pid_dir: Directory for PID metadata JSON files.
        model: Model name for metadata display.

    Returns:
        Wrapped command list.
    """
    return [
        sys.executable,
        "-m",
        "bernstein.core.worker",
        "--role",
        role,
        "--session",
        session_id,
        "--pid-dir",
        str(pid_dir),
        "--model",
        model,
        "--",
        *cmd,
    ]


class CLIAdapter(ABC):
    """Interface for launching and monitoring CLI coding agents.

    Implement this for each supported CLI (Claude Code, Codex, Gemini, etc.).
    """

    @abstractmethod
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        """Launch an agent process with the given prompt."""
        ...

    def is_alive(self, pid: int) -> bool:
        """Check if the agent process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, pid: int) -> None:
        """Terminate the agent process."""
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)

    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI adapter."""
        ...

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect the current API tier and remaining quota.

        Returns:
            ApiTierInfo if tier detection is supported and successful, None otherwise.
            Subclasses should override this to return provider-specific tier info.
        """
        return None
