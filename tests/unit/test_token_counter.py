"""Tests for bernstein.core.token_counter — cascading token count strategy."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bernstein.core.token_counter import (
    count_tokens,
    count_tokens_via_api,
    count_tokens_via_cheap_model,
    count_tokens_via_estimation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_TEXT = "def hello_world():\n    print('Hello, world!')\n"


# ---------------------------------------------------------------------------
# count_tokens_via_estimation
# ---------------------------------------------------------------------------


class TestCountTokensViaEstimation:
    """count_tokens_via_estimation uses the bytes/token ratio from token_estimation."""

    def test_positive_int_for_nonempty_text(self) -> None:
        result = count_tokens_via_estimation(_SAMPLE_TEXT)
        assert result > 0

    def test_code_file_type_uses_four_bytes_per_token(self) -> None:
        text = "a" * 100  # 100 ASCII bytes
        result = count_tokens_via_estimation(text, file_type="code")
        # CODE_BYTES_PER_TOKEN == 4.0  → 100 / 4 == 25
        assert result == 25

    def test_json_file_type_uses_two_bytes_per_token(self) -> None:
        text = "a" * 100
        result = count_tokens_via_estimation(text, file_type="json")
        # JSON_BYTES_PER_TOKEN == 2.0  → 100 / 2 == 50
        assert result == 50

    def test_empty_text_returns_zero(self) -> None:
        assert count_tokens_via_estimation("", file_type="code") == 0


# ---------------------------------------------------------------------------
# count_tokens_via_api
# ---------------------------------------------------------------------------


class TestCountTokensViaApi:
    @pytest.mark.asyncio
    async def test_success_returns_input_tokens(self) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"input_tokens": 42}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("bernstein.core.tokens.token_counter.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await count_tokens_via_api(_SAMPLE_TEXT)

        assert result == 42

    @pytest.mark.asyncio
    async def test_raises_when_no_api_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
                await count_tokens_via_api(_SAMPLE_TEXT)

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self) -> None:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("network failure"))

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("bernstein.core.tokens.token_counter.httpx.AsyncClient", return_value=mock_client),
        ):
            with pytest.raises(Exception, match="network failure"):
                await count_tokens_via_api(_SAMPLE_TEXT)


# ---------------------------------------------------------------------------
# count_tokens_via_cheap_model
# ---------------------------------------------------------------------------


class TestCountTokensViaCheapModel:
    @pytest.mark.asyncio
    async def test_extracts_integer_from_response(self) -> None:
        with patch("bernstein.core.tokens.token_counter.call_llm", new=AsyncMock(return_value="37")) as mock_llm:
            result = await count_tokens_via_cheap_model(_SAMPLE_TEXT)
        assert result == 37
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_no_integer_in_response(self) -> None:
        with patch("bernstein.core.tokens.token_counter.call_llm", new=AsyncMock(return_value="I don't know")):
            with pytest.raises(RuntimeError, match="No integer found"):
                await count_tokens_via_cheap_model(_SAMPLE_TEXT)

    @pytest.mark.asyncio
    async def test_raises_when_call_llm_raises(self) -> None:
        with patch("bernstein.core.tokens.token_counter.call_llm", new=AsyncMock(side_effect=RuntimeError("api error"))):
            with pytest.raises(RuntimeError, match="api error"):
                await count_tokens_via_cheap_model(_SAMPLE_TEXT)


# ---------------------------------------------------------------------------
# count_tokens (cascade)
# ---------------------------------------------------------------------------


class TestCountTokensCascade:
    @pytest.mark.asyncio
    async def test_returns_positive_int_for_sample_text(self) -> None:
        """Full cascade resolves to a positive integer (uses estimation at minimum)."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "bernstein.core.token_counter.call_llm",
                new=AsyncMock(side_effect=RuntimeError("no provider")),
            ),
        ):
            result = await count_tokens(_SAMPLE_TEXT, file_type="code")

        assert isinstance(result, int)
        assert result > 0

    @pytest.mark.asyncio
    async def test_api_failure_falls_through_to_cheap_model(self) -> None:
        """When httpx raises, cascade falls to cheap model."""
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(side_effect=Exception("http error"))

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("bernstein.core.tokens.token_counter.httpx.AsyncClient", return_value=mock_http_client),
            patch("bernstein.core.tokens.token_counter.call_llm", new=AsyncMock(return_value="99")) as mock_llm,
        ):
            result = await count_tokens(_SAMPLE_TEXT)

        assert result == 99
        mock_llm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cheap_model_failure_falls_through_to_estimation(self) -> None:
        """When both API and cheap model fail, cascade falls to bytes estimation."""
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(side_effect=Exception("http error"))

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("bernstein.core.tokens.token_counter.httpx.AsyncClient", return_value=mock_http_client),
            patch(
                "bernstein.core.token_counter.call_llm",
                new=AsyncMock(side_effect=RuntimeError("llm error")),
            ),
        ):
            result = await count_tokens(_SAMPLE_TEXT, file_type="code")

        # Falls back to bytes estimation: len("def hello_world():\n    ...") / 4
        expected = count_tokens_via_estimation(_SAMPLE_TEXT, file_type="code")
        assert result == expected
        assert result > 0

    @pytest.mark.asyncio
    async def test_api_success_skips_remaining_levels(self) -> None:
        """When API returns a count, cheap model and estimation are never called."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"input_tokens": 7}

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch("bernstein.core.tokens.token_counter.httpx.AsyncClient", return_value=mock_http_client),
            patch("bernstein.core.tokens.token_counter.call_llm", new=AsyncMock()) as mock_llm,
        ):
            result = await count_tokens(_SAMPLE_TEXT)

        assert result == 7
        mock_llm.assert_not_awaited()
