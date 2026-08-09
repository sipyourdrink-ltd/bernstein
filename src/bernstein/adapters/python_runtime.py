"""Generic Python-invoked agent-runtime adapter (#2959).

Drives Python-invoked agent runtimes in a subprocess-isolated worker, capturing
input, output, and tool events on the audit chain.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
)
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit


class PythonRuntimeAdapter(PluginAdapter):
    """Generic adapter for Python-invoked agent runtimes (#2959).

    Subclasses :class:`PluginAdapter` and spawns a subprocess worker running
    :mod:`bernstein.adapters.python_runtime_runner` in isolation.
    """

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name="python_runtime",
            version="1.0.0",
            author="Bernstein",
            description="Generic Python-invoked agent-runtime adapter",
            capabilities=(
                AdapterCapability.TOOL_USE,
                AdapterCapability.MULTI_MODEL,
                AdapterCapability.STREAMING,
            ),
        )

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
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        runner_script = Path(__file__).parent / "python_runtime_runner.py"

        runtime_module = ""
        runtime_entrypoint = "chat"
        if mcp_config:
            runtime_module = str(mcp_config.get("runtime_module", ""))
            runtime_entrypoint = str(mcp_config.get("runtime_entrypoint", "chat"))

        cmd = [
            sys.executable,
            str(runner_script),
            "--prompt",
            prompt,
            "--model",
            model_config.model,
            "--workdir",
            str(workdir),
        ]

        if runtime_module:
            cmd += ["--runtime-module", runtime_module]
            cmd += ["--runtime-entrypoint", runtime_entrypoint]

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

        env = build_filtered_env(["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PYTHONPATH"])

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
                raise RuntimeError("python executable not found") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing python_runtime: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "PythonRuntime"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect API tier from env secrets."""
        has_key = any(os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"))
        if not has_key:
            return None

        return ApiTierInfo(
            provider=ProviderType.GENERIC if hasattr(ProviderType, "GENERIC") else ProviderType.OPENAI,
            tier=ApiTier.PRO,
            rate_limit=RateLimit(requests_per_minute=60, tokens_per_minute=30_000),
            is_active=True,
        )
