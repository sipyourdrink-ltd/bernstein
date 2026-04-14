"""Cloudflare Workers AI as internal LLM provider.

Supports free-tier models (Llama, Mistral, Gemma) for task decomposition,
planning, and manager decisions. Zero-cost planning when using free models.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CF_AI_BASE = "https://api.cloudflare.com/client/v4/accounts"

# Free models on Workers AI
WORKERS_AI_MODELS: dict[str, dict[str, Any]] = {
    "@cf/meta/llama-3.1-70b-instruct": {
        "free": True,
        "context": 131072,
        "speed": "medium",
    },
    "@cf/meta/llama-3.1-8b-instruct": {
        "free": True,
        "context": 131072,
        "speed": "fast",
    },
    "@cf/mistral/mistral-7b-instruct-v0.2": {
        "free": True,
        "context": 32768,
        "speed": "fast",
    },
    "@cf/google/gemma-7b-it": {
        "free": True,
        "context": 8192,
        "speed": "fast",
    },
    "@cf/qwen/qwen1.5-14b-chat": {
        "free": True,
        "context": 32768,
        "speed": "medium",
    },
}


@dataclass(frozen=True)
class WorkersAIConfig:
    """Configuration for Workers AI provider."""

    account_id: str
    api_token: str
    model: str = "@cf/meta/llama-3.1-70b-instruct"
    max_tokens: int = 4096
    temperature: float = 0.3
    timeout_seconds: int = 60


@dataclass(frozen=True)
class WorkersAIResponse:
    """Response from Workers AI API."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    is_free: bool = True


class WorkersAIProvider:
    """Cloudflare Workers AI as Bernstein's internal LLM.

    Usage:
        provider = WorkersAIProvider(WorkersAIConfig(
            account_id="abc123",
            api_token="cf_token",
        ))
        response = await provider.complete("Decompose this task...")
        structured = await provider.structured("Plan tasks", schema={...})
    """

    def __init__(self, config: WorkersAIConfig) -> None:
        self._config = config

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
    ) -> WorkersAIResponse:
        """Generate text completion via Workers AI REST API.

        Args:
            prompt: The user prompt to send.
            system: Optional system prompt.
            max_tokens: Override default max tokens.

        Returns:
            WorkersAIResponse with generated text and token usage.

        Raises:
            httpx.HTTPStatusError: If the API returns an error status.
        """
        url = f"{_CF_AI_BASE}/{self._config.account_id}/ai/run/{self._config.model}"
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens or self._config.max_tokens,
            "temperature": self._config.temperature,
        }

        async with httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        result = data.get("result", {})
        usage = result.get("usage", {})
        model_info = WORKERS_AI_MODELS.get(self._config.model, {})
        return WorkersAIResponse(
            text=result.get("response", ""),
            model=self._config.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            is_free=model_info.get("free", False),
        )

    async def structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str = "",
    ) -> dict[str, Any]:
        """Generate structured JSON output conforming to schema.

        Args:
            prompt: The user prompt describing what to generate.
            schema: JSON schema the response must conform to.
            system: Optional system prompt.

        Returns:
            Parsed JSON dict matching the requested schema.

        Raises:
            json.JSONDecodeError: If the model output is not valid JSON.
        """
        schema_prompt = f"{prompt}\n\nRespond with valid JSON matching this schema:\n{json.dumps(schema, indent=2)}"
        response = await self.complete(schema_prompt, system=system)
        # Parse JSON from response, handling markdown code blocks
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD. Many Workers AI models are free.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Estimated cost in USD (0.0 for free-tier models).
        """
        model_info = WORKERS_AI_MODELS.get(self._config.model, {})
        if model_info.get("free", False):
            return 0.0
        # Paid models: ~$0.01/1M input, ~$0.02/1M output
        return (input_tokens * 0.01 + output_tokens * 0.02) / 1_000_000

    @staticmethod
    def available_models() -> dict[str, dict[str, Any]]:
        """Return available Workers AI models with metadata.

        Returns:
            Dict mapping model name to metadata (free, context, speed).
        """
        return dict(WORKERS_AI_MODELS)
