"""Tests for Cloudflare Workers AI provider."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bernstein.core.routing.cloudflare_ai import (
    _CF_AI_BASE,
    WORKERS_AI_MODELS,
    WorkersAIConfig,
    WorkersAIProvider,
    WorkersAIResponse,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> WorkersAIConfig:
    """Default test config."""
    return WorkersAIConfig(account_id="test-account", api_token="test-token")


@pytest.fixture
def provider(config: WorkersAIConfig) -> WorkersAIProvider:
    """Provider with default config."""
    return WorkersAIProvider(config)


def _mock_response(
    result: dict[str, Any],
    status_code: int = 200,
) -> httpx.Response:
    """Build a fake httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json={"success": True, "result": result},
        request=httpx.Request("POST", "https://fake"),
    )
    return resp


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestWorkersAIConfig:
    """WorkersAIConfig validation."""

    def test_defaults(self) -> None:
        cfg = WorkersAIConfig(account_id="a", api_token="t")
        assert cfg.model == "@cf/meta/llama-3.1-70b-instruct"
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.3
        assert cfg.timeout_seconds == 60

    def test_custom_values(self) -> None:
        cfg = WorkersAIConfig(
            account_id="a",
            api_token="t",
            model="@cf/meta/llama-3.1-8b-instruct",
            max_tokens=2048,
            temperature=0.7,
            timeout_seconds=30,
        )
        assert cfg.model == "@cf/meta/llama-3.1-8b-instruct"
        assert cfg.max_tokens == 2048

    def test_frozen(self) -> None:
        cfg = WorkersAIConfig(account_id="a", api_token="t")
        with pytest.raises(AttributeError):
            cfg.model = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# complete() tests
# ---------------------------------------------------------------------------


class TestComplete:
    """Tests for WorkersAIProvider.complete()."""

    @pytest.mark.asyncio
    async def test_success(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response(
            {
                "response": "Hello world",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete("test prompt")

        assert result.text == "Hello world"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.model == "@cf/meta/llama-3.1-70b-instruct"
        assert result.is_free is True

    @pytest.mark.asyncio
    async def test_with_system_prompt(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": "ok"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            await provider.complete("user msg", system="system msg")

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        messages = payload["messages"]
        assert len(messages) == 2
        assert messages[0] == {"role": "system", "content": "system msg"}
        assert messages[1] == {"role": "user", "content": "user msg"}

    @pytest.mark.asyncio
    async def test_custom_max_tokens(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": "ok"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            await provider.complete("test", max_tokens=1024)

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["max_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_auth_header(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": "ok"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            await provider.complete("test")

        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_url_construction(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": "ok"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            await provider.complete("test")

        call_args = mock_client.post.call_args
        url = call_args.args[0] if call_args.args else call_args[0][0]
        expected = f"{_CF_AI_BASE}/test-account/ai/run/@cf/meta/llama-3.1-70b-instruct"
        assert url == expected

    @pytest.mark.asyncio
    async def test_api_error(self, provider: WorkersAIProvider) -> None:
        error_resp = httpx.Response(
            status_code=500,
            json={"success": False, "errors": [{"message": "server error"}]},
            request=httpx.Request("POST", "https://fake"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=error_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await provider.complete("test")

    @pytest.mark.asyncio
    async def test_timeout(self, provider: WorkersAIProvider) -> None:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.TimeoutException):
                await provider.complete("test")

    @pytest.mark.asyncio
    async def test_missing_usage_fields(self, provider: WorkersAIProvider) -> None:
        """Response without usage data should default to 0 tokens."""
        mock_resp = _mock_response({"response": "hello"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete("test")

        assert result.input_tokens == 0
        assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# structured() tests
# ---------------------------------------------------------------------------


class TestStructured:
    """Tests for WorkersAIProvider.structured()."""

    @pytest.mark.asyncio
    async def test_clean_json(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": '{"tasks": ["a", "b"]}'})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        schema = {"type": "object", "properties": {"tasks": {"type": "array"}}}
        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            result = await provider.structured("plan", schema)

        assert result == {"tasks": ["a", "b"]}

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self, provider: WorkersAIProvider) -> None:
        wrapped = '```json\n{"tasks": ["x"]}\n```'
        mock_resp = _mock_response({"response": wrapped})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        schema = {"type": "object"}
        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            result = await provider.structured("plan", schema)

        assert result == {"tasks": ["x"]}

    @pytest.mark.asyncio
    async def test_invalid_json(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": "not json at all"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(json.JSONDecodeError):
                await provider.structured("plan", {"type": "object"})

    @pytest.mark.asyncio
    async def test_schema_included_in_prompt(self, provider: WorkersAIProvider) -> None:
        mock_resp = _mock_response({"response": '{"ok": true}'})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with patch("bernstein.core.routing.cloudflare_ai.httpx.AsyncClient", return_value=mock_client):
            await provider.structured("do it", schema)

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        user_msg = payload["messages"][-1]["content"]
        assert "Respond with valid JSON matching this schema:" in user_msg
        assert '"type": "object"' in user_msg


# ---------------------------------------------------------------------------
# estimate_cost() tests
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """Tests for cost estimation."""

    def test_free_model(self, provider: WorkersAIProvider) -> None:
        cost = provider.estimate_cost(input_tokens=1000, output_tokens=500)
        assert cost == 0.0

    def test_paid_model(self) -> None:
        cfg = WorkersAIConfig(
            account_id="a",
            api_token="t",
            model="@cf/some/paid-model",
        )
        p = WorkersAIProvider(cfg)
        cost = p.estimate_cost(input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(0.03)

    def test_zero_tokens(self, provider: WorkersAIProvider) -> None:
        assert provider.estimate_cost(0, 0) == 0.0


# ---------------------------------------------------------------------------
# available_models() tests
# ---------------------------------------------------------------------------


class TestAvailableModels:
    """Tests for model listing."""

    def test_returns_dict(self) -> None:
        models = WorkersAIProvider.available_models()
        assert isinstance(models, dict)
        assert len(models) == len(WORKERS_AI_MODELS)

    def test_returns_copy(self) -> None:
        models = WorkersAIProvider.available_models()
        models["@cf/fake/model"] = {"free": True}
        assert "@cf/fake/model" not in WORKERS_AI_MODELS

    def test_all_models_have_required_keys(self) -> None:
        for name, info in WorkersAIProvider.available_models().items():
            assert "free" in info, f"{name} missing 'free' key"
            assert "context" in info, f"{name} missing 'context' key"
            assert "speed" in info, f"{name} missing 'speed' key"


# ---------------------------------------------------------------------------
# WorkersAIResponse tests
# ---------------------------------------------------------------------------


class TestWorkersAIResponse:
    """Tests for response dataclass."""

    def test_defaults(self) -> None:
        r = WorkersAIResponse(text="hi", model="m")
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.is_free is True

    def test_frozen(self) -> None:
        r = WorkersAIResponse(text="hi", model="m")
        with pytest.raises(AttributeError):
            r.text = "bye"  # type: ignore[misc]
