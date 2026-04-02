"""Goose CLI adapter for Bernstein.

Adapter for Block's Goose (https://github.com/block/goose).
Goose is an AI agent that can execute tasks autonomously.
This adapter allows Bernstein to orchestrate Goose as a worker agent.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GooseAdapter(CLIAdapter):
    """Goose CLI adapter for Bernstein.

    Integrates with Block's Goose CLI agent.
    GitHub: https://github.com/block/goose

    Args:
        workdir: Project working directory.
        session_id: Unique agent session identifier.
        **kwargs: Additional adapter configuration.
    """

    name = "goose"

    def __init__(
        self,
        workdir: Path,
        session_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(workdir, session_id, **kwargs)
        self._cli_path = kwargs.get("cli_path", "goose")
        self._model = kwargs.get("model", "default")

    def spawn(
        self,
        task_description: str,
        files: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout: int | None = None,
    ) -> SpawnResult:
        """Spawn Goose agent for a task.

        Args:
            task_description: Task description to execute.
            files: Optional list of files to include in context.
            model: Optional model override.
            effort: Optional effort level override.
            timeout: Optional timeout in seconds.

        Returns:
            SpawnResult with agent output.
        """
        # Build Goose command
        # Goose uses: goose run --instruction "<task>"
        cmd = [self._cli_path, "run"]

        # Add instruction
        cmd.extend(["--instruction", task_description])

        # Add model if specified
        goose_model = model or self._model
        if goose_model and goose_model != "default":
            cmd.extend(["--model", goose_model])

        # Add files if specified
        if files:
            for f in files:
                cmd.extend(["--file", f])

        # Set timeout (Goose has built-in timeout)
        if timeout is None:
            timeout = self._default_timeout

        logger.info("Spawning Goose agent for task")
        logger.debug("Command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

            success = result.returncode == 0

            if success:
                logger.info("Goose agent completed successfully")
            else:
                logger.warning("Goose agent failed: %s", result.stderr[:200] if result.stderr else "Unknown error")

            return SpawnResult(
                success=success,
                pid=None,  # Goose manages its own process
                output=result.stdout,
                error=result.stderr,
            )

        except subprocess.TimeoutExpired:
            logger.warning("Goose agent timed out after %ds", timeout)
            return SpawnResult(
                success=False,
                pid=None,
                output="",
                error=f"Timeout after {timeout}s",
            )
        except FileNotFoundError:
            logger.error("Goose CLI not found at: %s", self._cli_path)
            return SpawnResult(
                success=False,
                pid=None,
                output="",
                error=f"Goose CLI not found at: {self._cli_path}",
            )
        except Exception as exc:
            logger.error("Goose agent spawn failed: %s", exc)
            return SpawnResult(
                success=False,
                pid=None,
                output="",
                error=str(exc),
            )

    def detect_tier(self) -> Any | None:
        """Detect the pricing tier for Goose.

        Goose can use various backends (Anthropic, OpenAI, etc.).
        Tier depends on the configured backend.

        Returns:
            Tier information or None if unavailable.
        """
        # Try to get Goose configuration
        try:
            # Goose stores config in ~/.config/goose/config.yaml
            from pathlib import Path as P

            config_path = P.home() / ".config" / "goose" / "config.yaml"
            if config_path.exists():
                import yaml

                config = yaml.safe_load(config_path.read_text())

                # Check provider
                provider = config.get("provider", "unknown")

                # Map to Bernstein tier
                from bernstein.core.router import Tier

                if provider in ("anthropic", "openai"):
                    return type(
                        "TierInfo",
                        (),
                        {
                            "tier": Tier.STANDARD,
                            "is_active": True,
                            "rate_limit": None,
                        },
                    )()
                elif provider in ("ollama", "local"):
                    return type(
                        "TierInfo",
                        (),
                        {
                            "tier": Tier.FREE,
                            "is_active": True,
                            "rate_limit": None,
                        },
                    )()

        except Exception as exc:
            logger.debug("Failed to detect Goose tier: %s", exc)

        return None

    def get_version(self) -> str | None:
        """Get Goose CLI version.

        Returns:
            Version string or None if unavailable.
        """
        try:
            result = subprocess.run(
                [self._cli_path, "--version"],
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
        """Check if Goose CLI is available.

        Returns:
            True if Goose is installed and accessible.
        """
        try:
            result = subprocess.run(
                [self._cli_path, "--help"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False
