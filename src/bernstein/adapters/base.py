"""Base adapter for CLI coding agents."""
from __future__ import annotations

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

    @abstractmethod
    def is_alive(self, pid: int) -> bool:
        """Check if the agent process is still running."""
        ...

    @abstractmethod
    def kill(self, pid: int) -> None:
        """Terminate the agent process."""
        ...

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
