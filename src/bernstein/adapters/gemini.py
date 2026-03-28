"""Google Gemini CLI adapter."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit


class GeminiAdapter(CLIAdapter):
    """Spawn and monitor Google Gemini CLI sessions."""

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

        cmd = [
            "gemini",
            "--model", model_config.model,
            "--sandbox", "none",
            "--prompt", prompt,
        ]

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
                raise RuntimeError(
                    "gemini not found in PATH. Install it with: npm install -g @google/gemini-cli"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing gemini: {exc}") from exc

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
        return "Gemini"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Gemini API tier based on environment configuration.

        Checks GOOGLE_API_KEY and GOOGLE_CLOUD_PROJECT to determine tier:
        - With GCP project = Enterprise tier
        - With paid API key = Pro tier
        - Default = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

        if not api_key:
            return None

        # Determine tier from environment
        if gcp_project:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=100000,
            )
        elif api_key.startswith("AIza"):
            # Standard API key format
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=15,
                tokens_per_minute=1500,
            )

        return ApiTierInfo(
            provider=ProviderType.GEMINI,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
