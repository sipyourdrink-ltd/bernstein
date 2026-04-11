"""Kiro CLI adapter."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

logger = logging.getLogger(__name__)


class KiroAdapter(CLIAdapter):
    """Spawn and monitor Kiro CLI sessions.

    The public CLI docs currently expose non-interactive chat mode but do not
    document a per-invocation model flag. Bernstein therefore treats model
    selection as a Kiro-side configuration concern and logs the requested model
    for observability without mutating global Kiro settings.
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
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if not (os.environ.get("KIRO_API_KEY") or (Path.home() / ".kiro").exists()):
            logger.warning("KiroAdapter: no Kiro auth detected — spawn may fail until `kiro-cli login` completes")
        if model_config.model and model_config.model.lower() != "auto":
            logger.info(
                "KiroAdapter: requested model %s for session %s; current Kiro CLI docs "
                "expose model selection via settings, not a per-run flag",
                model_config.model,
                session_id,
            )
        if mcp_config:
            logger.debug("KiroAdapter ignoring runtime MCP config injection for session %s", session_id)

        cmd = [
            "kiro-cli",
            "chat",
            "--no-interactive",
            "--trust-all-tools",
            prompt,
        ]

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

        env = build_filtered_env(["KIRO_API_KEY", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"])
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
                raise RuntimeError("kiro-cli not found in PATH. Install it from https://kiro.dev/cli") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing kiro-cli: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="kiro")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Kiro"

    def detect_tier(self) -> ApiTierInfo | None:
        """Best-effort Kiro tier detection from local auth state."""
        if not (os.environ.get("KIRO_API_KEY") or (Path.home() / ".kiro").exists()):
            return None

        return ApiTierInfo(
            provider=ProviderType.KIRO,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(requests_per_minute=60, tokens_per_minute=20_000),
            is_active=True,
        )
