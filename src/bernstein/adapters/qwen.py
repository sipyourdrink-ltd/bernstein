"""Qwen CLI adapter for OpenAI compatible models."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any, ClassVar

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import AdapterPluginInfo
from bernstein.core.defaults import QWEN_INSTALL_HINT
from bernstein.core.llm import LLMSettings
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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

    # Provider-string alias this adapter resolves from in
    # ``_infer_adapter_name_for_provider`` (via the registry's
    # provider-alias table). Unchanged from the old substring branch.
    provides = ("qwen",)

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

    # Model name mapping: Bernstein abstract names and aliases → real Qwen API model IDs.
    # "coder-model" is a qwen settings.json display alias, not a valid API model ID.
    _QWEN_PLUS_MODEL: ClassVar[str] = "qwen3.6-plus"
    _MODEL_MAP: ClassVar[dict[str, str]] = {
        "opus": _QWEN_PLUS_MODEL,
        "sonnet": _QWEN_PLUS_MODEL,
        "haiku": "qwen3-coder-plus",
        "coder-model": _QWEN_PLUS_MODEL,  # settings.json alias
        "default": _QWEN_PLUS_MODEL,
        "auto": _QWEN_PLUS_MODEL,
    }

    def plugin_info(self) -> AdapterPluginInfo:
        """Declare the sampling surface QwenAdapter genuinely wires.

        The ``qwen`` CLI exposes no sampling flags: ``--temperature``,
        ``--top-p``, ``--top-k`` and ``--max-tokens`` are all rejected by
        its argument parser (``Unknown argument: temperature``), which is a
        hard spawn failure rather than a silent drop. None are declared, so
        :func:`bernstein.adapters.plugin_sdk.ensure_sampling_params_supported`
        refuses such a spawn up front instead of building an argv the CLI
        will reject. Sampling for ``qwen`` is configured out-of-band via its
        settings file.
        """
        return AdapterPluginInfo(
            name="qwen",
            version="0.1.0",
            author="bernstein",
            description="Qwen CLI adapter for OpenAI compatible models",
            capabilities=(),
        )

    def _build_command(
        self,
        model_name: str,
        provider: str,
        settings: LLMSettings,
        *,
        mcp_config: dict[str, Any] | None = None,
    ) -> list[str]:
        """Build the qwen CLI command list (without the final prompt argument).

        The Tavily API key is never placed on argv (it would be visible to
        any user with ``ps`` access on the host). It is forwarded to the
        spawned process via the ``TAVILY_API_KEY`` environment variable in
        :meth:`spawn`.

        Args:
            mcp_config: Per-spawn config that may carry sampling overrides.
                None are wired onto argv - the qwen CLI exposes no sampling
                flags and rejects unknown arguments, so :meth:`plugin_info`
                declares no sampling capability and such a spawn is refused
                before it reaches here.
        """
        # ``--approval-mode yolo`` is the current auto-approve flag in
        # qwen-code. Verified accepted on 0.20.0 and 0.21.9 (an invalid value
        # reports ``Choices: "plan", "default", "auto-edit", "auto", "yolo"``),
        # but it is absent from ``qwen --help`` in every spelling, so the
        # adapter contract cannot drift-check it against the help text.
        # ``--output-format stream-json`` makes qwen-code emit line-delimited
        # JSON whose ``stats.models[<route>].tokens`` breakdown and per-call
        # ``usage`` blocks carry the provider's own token accounting. The
        # completion path recovers those counts from the session log so
        # ``bernstein cost`` shows real ``Tokens In`` / ``Tokens Out`` for
        # CLI-adapter runs instead of ``0`` / ``0`` (issue #2797). Streaming
        # is preserved: records arrive line by line rather than buffered.
        cmd: list[str] = ["qwen", "--approval-mode", "yolo", "--output-format", "stream-json"]

        # Map abstract/alias names to real Qwen API model IDs.
        # Always pass --model explicitly to avoid relying on settings.json.
        resolved = self._MODEL_MAP.get(model_name, model_name)
        cmd.extend(["--model", resolved])

        # ``--auth-type openai`` tells the CLI to use the OpenAI-compatible key and
        # base URL from the environment instead of its own login. Every named
        # provider needs it, and so does the default provider once an explicit
        # ``OPENAI_BASE_URL`` points the CLI at a custom OpenAI-compatible endpoint:
        # without the flag the CLI aborts non-interactively with "No auth type is
        # selected", producing a 0-token agent death with no transcript. A bare
        # ``OPENAI_API_KEY`` with no custom base URL keeps the CLI's own auth flow.
        if provider != "default" or settings.openai_base_url:
            cmd.extend(["--auth-type", "openai"])

        if mcp_config:
            # The qwen CLI parser rejects unknown arguments outright, so a
            # sampling flag that is not in its surface aborts the run instead
            # of being ignored. None are wired; plugin_info() declares no
            # sampling capability, so a spawn carrying one of the enforced keys
            # is refused before reaching here. ``max_tokens`` is the exception:
            # it is absent from SAMPLING_PARAM_KEYS, so nothing refuses it and
            # this warning is the only signal the value was dropped (#3586).
            for dropped_key in ("temperature", "top_p", "top_k", "max_tokens"):
                if mcp_config.get(dropped_key) is not None:
                    logger.warning(
                        "qwen adapter: %s=%r requested but not wired (qwen CLI has no matching "
                        "flag) - ensure_sampling_params_supported should have refused this spawn "
                        "before reaching here",
                        dropped_key,
                        mcp_config.get(dropped_key),
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
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        settings = LLMSettings()
        provider = self._detect_provider(settings)
        api_key, base_url = self._resolve_provider_config(provider, settings)

        env = build_filtered_env(["OPENAI_API_KEY", "OPENAI_BASE_URL", "TAVILY_API_KEY"])
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        # Forward the Tavily key via env (never argv) so it does not appear
        # in ``ps``/audit logs on shared hosts. Qwen Code reads
        # ``TAVILY_API_KEY`` from the environment when the web_search tool
        # is enabled. See qwen-code docs: web-search/configuration.
        if settings.tavily_api_key:
            env["TAVILY_API_KEY"] = settings.tavily_api_key

        # Pass the prompt as a positional argument (one-shot mode) instead of deprecated -p
        cmd = self._build_command(model_config.model, provider, settings, mcp_config=mcp_config)
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
                raise RuntimeError(f"qwen not found in PATH. Install it with: {QWEN_INSTALL_HINT}") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing qwen: {exc}") from exc

        # Pass proc through so downstream poll/wait works; see cursor.py.
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
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
