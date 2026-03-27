"""Qwen CLI adapter for OpenAI compatible models."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import TYPE_CHECKING

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.llm import LLMSettings
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig



class QwenAdapter(CLIAdapter):
    """Spawn and monitor Qwen CLI sessions.

    Qwen CLI is used as a generic OpenAI-compatible coding agent wrapper.
    It passes the provider's base_url and api_key directly to Qwen CLI.
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        settings = LLMSettings()

        # Determine base url and api key based on provider
        provider = getattr(model_config, "provider", None) or "default"

        api_key = ""
        base_url = ""

        if provider == "openrouter":
            api_key = settings.openrouter_api_key_paid or ""
            base_url = "https://openrouter.ai/api/v1"
        elif provider == "openrouter_free":
            api_key = settings.openrouter_api_key_free or settings.openrouter_api_key_paid or ""
            base_url = "https://openrouter.ai/api/v1"
        elif provider == "oxen":
            api_key = settings.oxen_api_key or ""
            base_url = settings.oxen_base_url
        elif provider == "together":
            api_key = settings.togetherai_user_key or ""
            base_url = "https://api.together.xyz/v1"
        elif provider == "g4f":
            api_key = settings.g4f_api_key or ""
            base_url = settings.g4f_base_url
        else:
            api_key = settings.openai_api_key or ""
            base_url = settings.openai_base_url or ""

        env = os.environ.copy()
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url

        cmd = [
            "qwen",
            "-y", # YOLO mode (auto-approve)
        ]

        model_name = model_config.model
        if provider == "default":
            # Map internal abstract models to native Qwen OAuth models
            if model_name == "opus":
                model_name = "qwen-max"
            elif model_name == "sonnet":
                model_name = "coder-model"

        cmd.extend(["--model", model_name])

        if provider != "default":
            cmd.extend(["--auth-type", "openai"])

        if settings.tavily_api_key:
            cmd.extend([
                "--tavily-api-key", settings.tavily_api_key,
                "--web-search-default", "tavily"
            ])

        # Pass the prompt as a positional argument (one-shot mode) instead of deprecated -p
        cmd.append(prompt)

        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

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

        # Check OpenRouter first
        if settings.openrouter_api_key_paid:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.PRO,
                rate_limit=RateLimit(
                    requests_per_minute=200,
                    tokens_per_minute=20000,
                ),
                is_active=True,
            )

        if settings.openrouter_api_key_free:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.FREE,
                rate_limit=RateLimit(
                    requests_per_minute=20,
                    tokens_per_minute=2000,
                ),
                is_active=True,
            )

        # Check Together.ai
        if settings.togetherai_user_key:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.PLUS,
                rate_limit=RateLimit(
                    requests_per_minute=60,
                    tokens_per_minute=6000,
                ),
                is_active=True,
            )

        # Check Oxen
        if settings.oxen_api_key:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.PRO,
                rate_limit=RateLimit(
                    requests_per_minute=100,
                    tokens_per_minute=10000,
                ),
                is_active=True,
            )

        # Check G4F
        if settings.g4f_api_key:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.FREE,
                rate_limit=RateLimit(
                    requests_per_minute=10,
                    tokens_per_minute=1000,
                ),
                is_active=True,
            )

        # Default OpenAI
        if settings.openai_api_key:
            return ApiTierInfo(
                provider=ProviderType.QWEN,
                tier=ApiTier.PLUS,
                rate_limit=RateLimit(
                    requests_per_minute=60,
                    tokens_per_minute=5000,
                ),
                is_active=True,
            )

        return None
