"""Qwen CLI adapter for OpenAI compatible models."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.llm import LLMSettings
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

if TYPE_CHECKING:
    from pathlib import Path


# Maps provider key → (tier, requests_per_minute, tokens_per_minute)
_PROVIDER_TIERS: dict[str, tuple[ApiTier, int, int]] = {
    "openrouter": (ApiTier.PRO, 200, 20000),
    "openrouter_free": (ApiTier.FREE, 20, 2000),
    "together": (ApiTier.PLUS, 60, 6000),
    "oxen": (ApiTier.PRO, 100, 10000),
    "g4f": (ApiTier.FREE, 10, 1000),
    "default": (ApiTier.PLUS, 60, 5000),
}


class QwenAdapter(CLIAdapter):
    """Spawn and monitor Qwen CLI sessions.

    Qwen CLI is used as a generic OpenAI-compatible coding agent wrapper.
    It passes the provider's base_url and api_key directly to Qwen CLI.
    """

    def _detect_provider(self, settings: LLMSettings) -> str:
        """Select provider based on which API keys are configured."""
        if settings.openrouter_api_key_paid:
            return "openrouter"
        if settings.openrouter_api_key_free:
            return "openrouter_free"
        if settings.togetherai_user_key:
            return "together"
        if settings.oxen_api_key:
            return "oxen"
        if settings.g4f_api_key:
            return "g4f"
        return "default"

    def _resolve_provider_config(self, provider: str, settings: LLMSettings) -> tuple[str, str]:
        """Return (api_key, base_url) for the given provider."""
        if provider == "openrouter":
            return settings.openrouter_api_key_paid or "", "https://openrouter.ai/api/v1"
        if provider == "openrouter_free":
            key = settings.openrouter_api_key_free or settings.openrouter_api_key_paid or ""
            return key, "https://openrouter.ai/api/v1"
        if provider == "oxen":
            return settings.oxen_api_key or "", settings.oxen_base_url
        if provider == "together":
            return settings.togetherai_user_key or "", "https://api.together.xyz/v1"
        if provider == "g4f":
            return settings.g4f_api_key or "", settings.g4f_base_url
        # default / openai
        return settings.openai_api_key or "", settings.openai_base_url or ""

    def _build_command(self, model_name: str, provider: str, settings: LLMSettings) -> list[str]:
        """Build the qwen CLI command list (without the final prompt argument)."""
        cmd: list[str] = ["qwen", "-y"]

        # Always map abstract model names (opus/sonnet/haiku) to native Qwen
        # models — these are Bernstein-internal names, not valid Qwen model IDs.
        if model_name in ("opus", "sonnet"):
            model_name = "qwen3.6-plus"
        elif model_name == "haiku":
            model_name = "qwen3-coder-plus"

        cmd.extend(["--model", model_name])

        if provider != "default":
            cmd.extend(["--auth-type", "openai"])

        if settings.tavily_api_key:
            cmd.extend(
                [
                    "--tavily-api-key",
                    settings.tavily_api_key,
                    "--web-search-default",
                    "tavily",
                ]
            )

        return cmd

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

        settings = LLMSettings()
        provider = self._detect_provider(settings)
        api_key, base_url = self._resolve_provider_config(provider, settings)

        env = build_filtered_env(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url

        # Pass the prompt as a positional argument (one-shot mode) instead of deprecated -p
        cmd = self._build_command(model_config.model, provider, settings)
        cmd.append(prompt)

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

        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("qwen not found in PATH. Install it with: npm install -g qwen-code") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing qwen: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Qwen CLI"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Qwen/OpenAI-compatible API tier based on environment configuration.

        Checks provider-specific environment variables to determine tier:
        - OpenRouter paid = Pro tier
        - OpenRouter free = Free tier
        - Together.ai = Plus tier
        - Default OpenAI = based on key format

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        settings = LLMSettings()
        provider = self._detect_provider(settings)

        if provider == "default" and not settings.openai_api_key:
            return None

        tier, rpm, tpm = _PROVIDER_TIERS.get(provider, (ApiTier.PLUS, 60, 5000))
        return ApiTierInfo(
            provider=ProviderType.QWEN,
            tier=tier,
            rate_limit=RateLimit(requests_per_minute=rpm, tokens_per_minute=tpm),
            is_active=True,
        )
