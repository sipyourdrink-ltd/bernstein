"""OpenAI Codex CLI adapter."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

logger = logging.getLogger(__name__)


class CodexAdapter(CLIAdapter):
    """Spawn and monitor OpenAI Codex CLI sessions."""

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
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = workdir / ".sdd" / "runtime" / f"{session_id}.last-message.txt"

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("CodexAdapter: OPENAI_API_KEY is not set — spawn will fail")

        cmd = [
            "codex",
            "exec",
            "--full-auto",
            "-m",
            model_config.model,
            "--json",
            "-o",
            str(output_path),
            prompt,
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL"])
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("codex not found in PATH. Install it with: npm install -g @openai/codex") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing codex: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="codex")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Codex"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Codex API tier based on environment configuration.

        Checks OPENAI_API_KEY and OPENAI_ORG_ID to determine tier:
        - With organization ID = Enterprise tier
        - With paid account (sk-proj...) = Pro tier
        - Default = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "")
        org_id = os.environ.get("OPENAI_ORG_ID", "")

        if not api_key:
            return None

        # Determine tier from environment and key format
        if org_id:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(
                requests_per_minute=500,
                tokens_per_minute=90000,
            )
        elif api_key.startswith("sk-proj"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        elif api_key.startswith("sk-"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=60,
                tokens_per_minute=5000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
