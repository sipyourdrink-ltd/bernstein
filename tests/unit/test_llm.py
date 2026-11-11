"""Unit tests for bernstein.core.llm - LLMSettings and get_client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bernstein.core.llm import LLMSettings, call_llm, get_client, tavily_search

# ---------------------------------------------------------------------------
# LLMSettings - env var loading
# ---------------------------------------------------------------------------


class TestLLMSettings:
    def test_all_fields_default_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "OPENROUTER_API_KEY_PAID",
            "OPENROUTER_API_KEY_FREE",
            "OXEN_API_KEY",
            "TOGETHERAI_USER_KEY",
            "G4F_API_KEY",
            "OPENAI_API_KEY",
            "TAVILY_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.openrouter_api_key_paid is None
        assert s.openrouter_api_key_free is None
        assert s.oxen_api_key is None
        assert s.togetherai_user_key is None
        assert s.g4f_api_key is None
        assert s.openai_api_key is None
        assert s.tavily_api_key is None

    def test_reads_openrouter_keys_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY_PAID", "paid-key")
        monkeypatch.setenv("OPENROUTER_API_KEY_FREE", "free-key")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.openrouter_api_key_paid == "paid-key"
        assert s.openrouter_api_key_free == "free-key"

    def test_reads_oxen_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OXEN_API_KEY", "oxen-secret")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.oxen_api_key == "oxen-secret"

    def test_reads_togetherai_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHERAI_USER_KEY", "together-secret")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.togetherai_user_key == "together-secret"

    def test_reads_g4f_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("G4F_API_KEY", "g4f-secret")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.g4f_api_key == "g4f-secret"

    def test_reads_openai_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.openai_api_key == "sk-test"

    def test_reads_tavily_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.tavily_api_key == "tvly-secret"

    def test_default_oxen_base_url(self) -> None:
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.oxen_base_url == "https://hub.oxen.ai/api"

    def test_default_g4f_base_url(self) -> None:
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.g4f_base_url == "https://g4f.space/v1"

    def test_openai_base_url_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        s = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.openai_base_url is None


# ---------------------------------------------------------------------------
# get_client - provider routing and base_url selection
# ---------------------------------------------------------------------------


def _make_settings(**kwargs: str | None) -> LLMSettings:
    """Build an LLMSettings with all keys cleared unless specified."""
    defaults: dict[str, str | None] = {
        "openrouter_api_key_paid": None,
        "openrouter_api_key_free": None,
        "oxen_api_key": None,
        "togetherai_user_key": None,
        "g4f_api_key": None,
        "openai_api_key": None,
        "openai_base_url": None,
        "tavily_api_key": None,
    }
    defaults.update(kwargs)
    return LLMSettings.model_construct(**defaults)  # type: ignore[arg-type]


class TestGetClient:
    # --- openrouter ---

    def test_openrouter_returns_client_with_correct_base_url(self) -> None:
        settings = _make_settings(openrouter_api_key_paid="paid-key")
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("openrouter")
            mock_cls.assert_called_once_with(
                base_url="https://openrouter.ai/api/v1",
                api_key="paid-key",
            )

    def test_openrouter_raises_when_paid_key_missing(self) -> None:
        settings = _make_settings(openrouter_api_key_paid=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY_PAID"):
                get_client("openrouter")

    # --- openrouter_free ---

    def test_openrouter_free_uses_free_key_when_available(self) -> None:
        settings = _make_settings(
            openrouter_api_key_free="free-key",
            openrouter_api_key_paid="paid-key",
        )
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("openrouter_free")
            mock_cls.assert_called_once_with(
                base_url="https://openrouter.ai/api/v1",
                api_key="free-key",
            )

    def test_openrouter_free_falls_back_to_paid_key(self) -> None:
        settings = _make_settings(
            openrouter_api_key_free=None,
            openrouter_api_key_paid="paid-key",
        )
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("openrouter_free")
            mock_cls.assert_called_once_with(
                base_url="https://openrouter.ai/api/v1",
                api_key="paid-key",
            )

    def test_openrouter_free_raises_when_both_keys_missing(self) -> None:
        settings = _make_settings(
            openrouter_api_key_free=None,
            openrouter_api_key_paid=None,
        )
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="OpenRouter API key"):
                get_client("openrouter_free")

    # --- oxen ---

    def test_oxen_returns_client_with_default_base_url(self) -> None:
        settings = _make_settings(oxen_api_key="oxen-secret")
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("oxen")
            mock_cls.assert_called_once_with(
                base_url="https://hub.oxen.ai/api",
                api_key="oxen-secret",
            )

    def test_oxen_raises_when_key_missing(self) -> None:
        settings = _make_settings(oxen_api_key=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="OXEN_API_KEY"):
                get_client("oxen")

    # --- together ---

    def test_together_returns_client_with_correct_base_url(self) -> None:
        settings = _make_settings(togetherai_user_key="together-secret")
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("together")
            mock_cls.assert_called_once_with(
                base_url="https://api.together.xyz/v1",
                api_key="together-secret",
            )

    def test_together_raises_when_key_missing(self) -> None:
        settings = _make_settings(togetherai_user_key=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="TOGETHERAI_USER_KEY"):
                get_client("together")

    # --- g4f ---

    def test_g4f_returns_client_with_default_base_url(self) -> None:
        settings = _make_settings(g4f_api_key="g4f-secret")
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("g4f")
            mock_cls.assert_called_once_with(
                base_url="https://g4f.space/v1",
                api_key="g4f-secret",
            )

    def test_g4f_raises_when_key_missing(self) -> None:
        settings = _make_settings(g4f_api_key=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="G4F_API_KEY"):
                get_client("g4f")

    # --- openai (default fallback) ---

    def test_openai_default_fallback_when_unknown_provider(self) -> None:
        settings = _make_settings(openai_api_key="sk-test")
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("openai")
            mock_cls.assert_called_once_with(
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            )

    def test_openai_with_custom_base_url(self) -> None:
        settings = _make_settings(
            openai_api_key="sk-test",
            openai_base_url="https://my-proxy.example.com/v1",
        )
        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("bernstein.core.routing.llm.AsyncOpenAI") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            get_client("openai")
            mock_cls.assert_called_once_with(
                base_url="https://my-proxy.example.com/v1",
                api_key="sk-test",
            )

    def test_raises_when_unknown_provider_and_no_openai_key(self) -> None:
        settings = _make_settings(openai_api_key=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            with pytest.raises(ValueError, match="Unknown or unconfigured provider"):
                get_client("nonexistent_provider")

    # --- return type ---

    def test_get_client_returns_asyncopenai_instance(self) -> None:
        from openai import AsyncOpenAI

        settings = _make_settings(openrouter_api_key_paid="paid-key")
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            client = get_client("openrouter")
        assert isinstance(client, AsyncOpenAI)


# ---------------------------------------------------------------------------
# call_llm - async LLM invocation
# ---------------------------------------------------------------------------


def _make_completion_response(content: str | None, num_choices: int = 1) -> MagicMock:
    """Build a mock chat completion response."""
    response = MagicMock()
    if num_choices == 0:
        response.choices = []
    else:
        choice = MagicMock()
        choice.message.content = content
        response.choices = [choice]
    return response


class TestCallLLM:
    @pytest.mark.asyncio
    async def test_successful_call_returns_content_string(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_completion_response("Hello, world!"))
        with patch("bernstein.core.llm.get_client", return_value=mock_client):
            result = await call_llm("test prompt", "gpt-4")
        assert result == "Hello, world!"

    @pytest.mark.asyncio
    async def test_empty_choices_returns_empty_string(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_completion_response(None, num_choices=0))
        with patch("bernstein.core.llm.get_client", return_value=mock_client):
            result = await call_llm("test prompt", "gpt-4")
        assert result == ""

    @pytest.mark.asyncio
    async def test_none_content_returns_empty_string(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_completion_response(None))
        with patch("bernstein.core.llm.get_client", return_value=mock_client):
            result = await call_llm("test prompt", "gpt-4")
        assert result == ""

    @pytest.mark.asyncio
    async def test_api_exception_wraps_in_runtime_error(self) -> None:
        mock_client = MagicMock()
        original_error = ValueError("quota exceeded")
        mock_client.chat.completions.create = AsyncMock(side_effect=original_error)
        with patch("bernstein.core.llm.get_client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="quota exceeded"):
                await call_llm("test prompt", "gpt-4")

    @pytest.mark.asyncio
    async def test_provider_routing_calls_get_client_with_correct_provider(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_completion_response("response"))
        with patch("bernstein.core.llm.get_client", return_value=mock_client) as mock_get_client:
            await call_llm("test prompt", "gpt-4", provider="openrouter")
        mock_get_client.assert_called_once_with("openrouter")

    @pytest.mark.asyncio
    async def test_openai_provider_uses_api_client_not_cli(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=_make_completion_response("ok"))
        with (
            patch("bernstein.core.llm.get_client", return_value=mock_client) as mock_get_client,
            patch("bernstein.core.llm._call_cli_provider", new=AsyncMock(side_effect=AssertionError("CLI path"))),
        ):
            result = await call_llm("test prompt", "gpt-4", provider="openai")
        assert result == "ok"
        mock_get_client.assert_called_once_with("openai")


# ---------------------------------------------------------------------------
# call_llm - deterministic replay (issue #1832)
# ---------------------------------------------------------------------------


class _ProviderDouble:
    """Records every live provider invocation so tests can assert zero."""

    def __init__(self, response: str = "live response") -> None:
        self.calls = 0
        self._response = response

    async def __call__(self, *_args: object, **_kwargs: object) -> str:
        self.calls += 1
        return self._response


class TestCallLLMReplay:
    """``call_llm`` must be hermetic under a strict replay store."""

    @pytest.fixture(autouse=True)
    def _clear_active_store(self):
        """Ensure no store leaks across tests."""
        from bernstein.core.orchestration.deterministic import set_active_store

        set_active_store(None)
        yield
        set_active_store(None)

    def _replay_store(self, tmp_path, *, strict: bool, recorded: dict[str, str] | None = None):
        """Build a replay-mode DeterministicStore over a written recording."""
        import json

        from bernstein.core.orchestration.deterministic import DeterministicStore, _prompt_key

        run_dir = tmp_path / "runs" / "rep"
        run_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for prompt, response in (recorded or {}).items():
            key = _prompt_key(prompt, "gpt-4", provider="openrouter_free", temperature=0.7, max_tokens=4000)
            lines.append(json.dumps({"key": key, "model": "gpt-4", "response": response}))
        (run_dir / "llm_calls.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return DeterministicStore(run_dir, replay=True, strict=strict)

    @pytest.mark.asyncio
    async def test_replay_hit_returns_recorded_without_calling_provider(self, tmp_path) -> None:
        """A recorded prompt replays its response; no live provider call."""
        from bernstein.core.orchestration.deterministic import set_active_store

        store = self._replay_store(tmp_path, strict=True, recorded={"recorded prompt": "cached answer"})
        set_active_store(store)
        double = _ProviderDouble()

        with patch("bernstein.core.llm._call_api_provider", new=double):
            result = await call_llm("recorded prompt", "gpt-4", provider="openrouter_free")

        assert result == "cached answer"
        assert double.calls == 0

    @pytest.mark.asyncio
    async def test_strict_replay_miss_raises_and_makes_no_live_call(self, tmp_path) -> None:
        """An unknown prompt under strict replay raises and never calls live."""
        from bernstein.core.orchestration.deterministic import ReplayMissError, set_active_store

        store = self._replay_store(tmp_path, strict=True, recorded={"recorded prompt": "cached answer"})
        set_active_store(store)
        double = _ProviderDouble()
        cli_double = _ProviderDouble()

        with (
            patch("bernstein.core.llm._call_api_provider", new=double),
            patch("bernstein.core.llm._call_cli_provider", new=cli_double),
            pytest.raises(ReplayMissError),
        ):
            await call_llm("an unseen prompt", "gpt-4", provider="openrouter_free")

        assert double.calls == 0
        assert cli_double.calls == 0
        assert store.strict_violations == 1

    @pytest.mark.asyncio
    async def test_non_strict_miss_warns_and_falls_through(self, tmp_path, caplog) -> None:
        """Non-strict replay logs a WARNING per miss and calls the provider."""
        import logging

        from bernstein.core.orchestration.deterministic import set_active_store

        store = self._replay_store(tmp_path, strict=False, recorded={"recorded prompt": "cached answer"})
        set_active_store(store)
        double = _ProviderDouble(response="fresh live answer")

        with patch("bernstein.core.llm._call_api_provider", new=double), caplog.at_level(logging.WARNING):
            result = await call_llm("an unseen prompt", "gpt-4", provider="openrouter_free")

        assert result == "fresh live answer"
        assert double.calls == 1
        assert store.misses == 1
        assert any("miss" in rec.message.lower() for rec in caplog.records if rec.levelno >= logging.WARNING)

    @pytest.mark.asyncio
    async def test_replay_key_includes_temperature(self, tmp_path) -> None:
        """A recording made at temp 0.7 misses when called at temp 0.0."""
        from bernstein.core.orchestration.deterministic import ReplayMissError, set_active_store

        store = self._replay_store(tmp_path, strict=True, recorded={"recorded prompt": "cached answer"})
        set_active_store(store)
        double = _ProviderDouble()

        # temp 0.7 matches the recording.
        with patch("bernstein.core.llm._call_api_provider", new=double):
            assert await call_llm("recorded prompt", "gpt-4", temperature=0.7) == "cached answer"

        # temp 0.0 is a different request -> strict miss, no live call.
        with patch("bernstein.core.llm._call_api_provider", new=double), pytest.raises(ReplayMissError):
            await call_llm("recorded prompt", "gpt-4", temperature=0.0)
        assert double.calls == 0


# ---------------------------------------------------------------------------
# tavily_search - web search via Tavily API
# ---------------------------------------------------------------------------


class TestTavilySearch:
    @pytest.mark.asyncio
    async def test_missing_api_key_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        settings = _make_settings(tavily_api_key=None)
        with patch("bernstein.core.llm.LLMSettings", return_value=settings):
            result = await tavily_search("test query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_successful_search_formats_results_as_markdown(self) -> None:
        settings = _make_settings(tavily_api_key="tvly-secret")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"title": "Article 1", "content": "Content 1", "url": "https://example.com/1"},
                {"title": "Article 2", "content": "Content 2", "url": "https://example.com/2"},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_async_client = AsyncMock()
        mock_async_client.post = AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            result = await tavily_search("test query")

        assert "**Article 1**" in result
        assert "Content 1" in result
        assert "https://example.com/1" in result
        assert "**Article 2**" in result

    @pytest.mark.asyncio
    async def test_empty_results_returns_no_results_message(self) -> None:
        settings = _make_settings(tavily_api_key="tvly-secret")
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": []}
        mock_response.raise_for_status = MagicMock()

        mock_async_client = AsyncMock()
        mock_async_client.post = AsyncMock(return_value=mock_response)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            result = await tavily_search("obscure query")

        assert result == "(No relevant web results found.)"

    @pytest.mark.asyncio
    async def test_http_error_returns_formatted_error_message(self) -> None:
        import httpx

        settings = _make_settings(tavily_api_key="tvly-secret")
        http_error = httpx.HTTPStatusError(
            "403 Forbidden",
            request=MagicMock(),
            response=MagicMock(),
        )

        mock_async_client = AsyncMock()
        mock_async_client.post = AsyncMock(side_effect=http_error)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            result = await tavily_search("test query")

        assert result.startswith("(Web search failed:")

    @pytest.mark.asyncio
    async def test_network_exception_returns_formatted_error_message(self) -> None:
        import httpx

        settings = _make_settings(tavily_api_key="tvly-secret")
        network_error = httpx.ConnectError("Connection refused")

        mock_async_client = AsyncMock()
        mock_async_client.post = AsyncMock(side_effect=network_error)
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("bernstein.core.llm.LLMSettings", return_value=settings),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            result = await tavily_search("test query")

        assert result.startswith("(Web search failed:")
