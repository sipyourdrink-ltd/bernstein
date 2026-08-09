"""Kimchi CLI adapter (#3100)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit


class KimchiAdapter(CLIAdapter):
    """Spawn and monitor Kimchi CLI sessions (#3100).

    Kimchi runs open-weight / hosted models driven over the ACP event channel:
    ``kimchi --mode acp --prompt "<task>" [--model <model>] [--session <path>] [--yolo]``
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
        session_path: Path | str | None = None,
        dangerous_mode: bool = False,
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "kimchi",
            "--mode",
            "acp",
            "--prompt",
            prompt,
        ]

        if model_config.model:
            cmd += ["--model", model_config.model]

        if session_path:
            cmd += ["--session", str(session_path)]

        if dangerous_mode:
            cmd.append("--yolo")

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

        env = build_filtered_env(["KIMCHI_API_KEY", "KIMCHI_OLLAMA_HOST", "OLLAMA_HOST"])
        env["KIMCHI_TELEMETRY_ENABLED"] = "0"

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
                raise RuntimeError("kimchi not found in PATH. Install from brew install getkimchi/tap/kimchi") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing kimchi: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Kimchi"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Kimchi tier from environment or local config."""
        api_key = os.environ.get("KIMCHI_API_KEY", "")
        config_file = Path.home() / ".config" / "kimchi" / "config.json"

        if not api_key and not config_file.exists():
            return None

        tier = ApiTier.PRO
        rate_limit = RateLimit(
            requests_per_minute=60,
            tokens_per_minute=30_000,
        )

        return ApiTierInfo(
            provider=ProviderType.KIMCHI,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
