"""Tests for cascading_token_counter — API→cheap model→bytes/4 fallback chain."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.cascading_token_counter import (
    _API_MAX_CHARS,
    _count_tokens_bytes_estimate,
    _count_tokens_via_api,
    count_tokens_cascading,
)

# ---------------------------------------------------------------------------
# _count_tokens_via_api
# ---------------------------------------------------------------------------


class TestCountTokensViaApi:
    def test_returns_none_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = _count_tokens_via_api("hello world", "claude-haiku-4-5-20251001")
        assert result is None

    def test_returns_none_for_oversized_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        big_text = "x" * (_API_MAX_CHARS + 1)
        result = _count_tokens_via_api(big_text, "claude-haiku-4-5-20251001")
        assert result is None

    def test_returns_count_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import json
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"input_tokens": 42}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _count_tokens_via_api("some text", "claude-haiku-4-5-20251001")

        assert result == 42

    def test_returns_none_on_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = _count_tokens_via_api("text", "claude-haiku-4-5-20251001")
        assert result is None

    def test_returns_none_on_invalid_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _count_tokens_via_api("text", "claude-haiku-4-5-20251001")
        assert result is None


# ---------------------------------------------------------------------------
# _count_tokens_bytes_estimate
# ---------------------------------------------------------------------------


class TestCountTokensBytesEstimate:
    def test_returns_positive_for_nonempty(self) -> None:
        result = _count_tokens_bytes_estimate("hello world " * 100)
        assert result > 0

    def test_returns_zero_for_empty(self) -> None:
        # estimate_tokens_for_text returns 0 for empty
        result = _count_tokens_bytes_estimate("")
        assert result == 0

    def test_longer_text_more_tokens(self) -> None:
        short = _count_tokens_bytes_estimate("short")
        long_text = _count_tokens_bytes_estimate("short " * 1000)
        assert long_text > short

    def test_approx_bytes_per_4(self) -> None:
        # 400 ASCII chars / 4 bytes per token = 100 tokens
        text = "a" * 400
        result = _count_tokens_bytes_estimate(text)
        assert result == 100


# ---------------------------------------------------------------------------
# count_tokens_cascading — integration / cascade logic
# ---------------------------------------------------------------------------


class TestCountTokensCascading:
    def test_returns_zero_for_empty_text(self) -> None:
        result = asyncio.get_event_loop().run_until_complete(count_tokens_cascading(""))
        assert result == 0

    def test_api_tier_used_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When API returns a value, it should be used without calling cheaper tiers."""
        import json

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"input_tokens": 99}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("some text")
            )

        assert result == 99

    def test_falls_back_to_bytes_when_api_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cascade reaches bytes/4 when API and cheap model both fail."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = asyncio.get_event_loop().run_until_complete(
            count_tokens_cascading("hello world", skip_cheap_model=True)
        )
        assert result > 0

    def test_skip_api_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """skip_api=True skips the API tier entirely."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch(
            "bernstein.core.cascading_token_counter._count_tokens_via_api"
        ) as mock_api:
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("text", skip_api=True, skip_cheap_model=True)
            )
        mock_api.assert_not_called()
        assert result > 0

    def test_skip_cheap_model_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """skip_cheap_model=True skips tier 2."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch(
            "bernstein.core.cascading_token_counter._count_tokens_via_cheap_model"
        ) as mock_cheap:
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("text", skip_cheap_model=True)
            )
        mock_cheap.assert_not_called()
        assert result > 0

    def test_cheap_model_used_when_api_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 2 (cheap model) is tried when tier 1 (API) is unavailable."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        async def fake_cheap(text: str) -> int | None:
            return 77

        with patch(
            "bernstein.core.cascading_token_counter._count_tokens_via_cheap_model",
            side_effect=fake_cheap,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("hello")
            )
        assert result == 77

    def test_bytes_fallback_when_all_tiers_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes/4 estimate used when both API and cheap model fail."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        async def fake_cheap_fail(text: str) -> int | None:
            return None

        with patch(
            "bernstein.core.cascading_token_counter._count_tokens_via_cheap_model",
            side_effect=fake_cheap_fail,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("a" * 400)
            )
        assert result == 100  # 400 bytes / 4 = 100 tokens

    def test_cascade_ordering_api_before_cheap_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify cascade order: API→cheap model→bytes (order matters)."""
        import json

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"input_tokens": 55}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        call_log: list[str] = []

        async def fake_cheap(text: str) -> int | None:
            call_log.append("cheap")
            return 33

        with (
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_url,
            patch(
                "bernstein.core.cascading_token_counter._count_tokens_via_cheap_model",
                side_effect=fake_cheap,
            ),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                count_tokens_cascading("hello world")
            )

        # API tier was used, cheap model was never called
        assert result == 55
        assert "cheap" not in call_log
        mock_url.assert_called_once()
