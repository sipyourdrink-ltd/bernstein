"""OpenCode CLI adapter."""

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

_OPENCODE_AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"


class OpenCodeAdapter(CLIAdapter):
    """Spawn and monitor OpenCode CLI sessions."""

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
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if not _has_opencode_auth():
            logger.warning(
                "OpenCodeAdapter: no OpenCode/provider auth detected — spawn may fail until "
                "`opencode auth login` or provider env vars are configured"
            )
        if mcp_config:
            logger.debug("OpenCodeAdapter ignoring runtime MCP config injection for session %s", session_id)

        cmd = [
            "opencode",
            "run",
            "-m",
            model_config.model,
            "--format",
            "json",
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

        env = build_filtered_env(
            [
                "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "OPENROUTER_API_KEY",
                "OPENROUTER_API_KEY_PAID",
                "XAI_API_KEY",
                "GITLAB_TOKEN",
            ]
        )
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
                raise RuntimeError("opencode not found in PATH. Install it from https://opencode.ai/docs/cli/") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing opencode: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="opencode")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "OpenCode"

    def detect_tier(self) -> ApiTierInfo | None:
        """Best-effort OpenCode tier detection from auth state."""
        if not _has_opencode_auth():
            return None

        return ApiTierInfo(
            provider=ProviderType.OPENCODE,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(requests_per_minute=120, tokens_per_minute=40_000),
            is_active=True,
        )


def _has_opencode_auth() -> bool:
    """Return True when OpenCode has a credentials file or provider API key."""
    if _OPENCODE_AUTH_FILE.exists():
        return True
    key_vars = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY_PAID",
        "XAI_API_KEY",
        "GITLAB_TOKEN",
    )
    return any(os.environ.get(name) for name in key_vars)
