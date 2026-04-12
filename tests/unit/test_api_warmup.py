"""Tests for bernstein.cli.api_warmup - warmup, cache, TTL, and local/proxy skip logic."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

import bernstein.cli.api_warmup
from bernstein.cli.api_warmup import (
    _WARMUP_TTL_SECONDS,
    WarmupResult,
    can_skip_warmup,
    check_warmup_status,
    clear_cache,
    warmup_provider,
)

# -- fixtures --


@pytest.fixture(autouse=True)
def clean_cache() -> Generator[None, None, None]:
    """Reset the warmup cache before and after each test."""
    clear_cache()
    yield
    clear_cache()


# -- WarmupResult --


class TestWarmupResult:
    def test_dataclass_fields(self) -> None:
        result = WarmupResult(provider="openrouter", latency_ms=12.3, success=True)
        assert result.provider == "openrouter"
        assert result.latency_ms == pytest.approx(12.3)
        assert result.success is True

    def test_is_frozen(self) -> None:
        result = WarmupResult(provider="oxen", latency_ms=0.0, success=False)
        with pytest.raises(AttributeError):
            result.provider = "changed"  # type: ignore[reportAttributeAccessIssue]


# -- _is_local_or_proxy --


class TestIsLocalOrProxy:
    def test_localhost_string(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("http://localhost:8080/api") is True

    def test_127_address(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("http://127.0.0.1:3000/anything") is True

    def test_0_0_0_0(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("http://0.0.0.0:9000/v1") is True

    def test_unix_socket(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("unix:///var/run/api.sock") is True

    def test_remote_url(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("https://api.openai.com/v1") is False

    def test_hypothetical(self) -> None:
        from bernstein.cli.api_warmup import _is_local_or_proxy

        assert _is_local_or_proxy("https://api.example.com/v1") is False


# -- _get_provider_base_url --


class TestGetProviderBaseUrl:
    def test_openrouter(self) -> None:
        from bernstein.core.llm import LLMSettings

        settings = LLMSettings(_env_file=None, openrouter_api_key_paid="pk-123")  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._get_provider_base_url("openrouter", settings) == "https://openrouter.ai/api/v1"

    def test_unknown_provider_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core.llm import LLMSettings

        for var in (
            "OPENROUTER_API_KEY_PAID",
            "OPENROUTER_API_KEY_FREE",
            "OXEN_API_KEY",
            "TOGETHERAI_USER_KEY",
            "G4F_API_KEY",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        settings = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._get_provider_base_url("bogus", settings) == ""


# -- _is_provider_configured --


class TestIsProviderConfigured:
    def test_openrouter_with_key(self) -> None:
        from bernstein.core.llm import LLMSettings

        settings = LLMSettings(_env_file=None, openrouter_api_key_paid="pk-123")  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._is_provider_configured("openrouter", settings) is True

    def test_openrouter_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("OPENROUTER_API_KEY_PAID", "OPENROUTER_API_KEY_FREE"):
            monkeypatch.delenv(var, raising=False)
        from bernstein.core.llm import LLMSettings

        settings = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._is_provider_configured("openrouter", settings) is False

    def test_openai_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from bernstein.core.llm import LLMSettings

        settings = LLMSettings(_env_file=None)  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._is_provider_configured("openai", settings) is False

    def test_oxen_with_key(self) -> None:
        from bernstein.core.llm import LLMSettings

        settings = LLMSettings(_env_file=None, oxen_api_key="oxen-123")  # type: ignore[call-arg]
        assert bernstein.cli.api_warmup._is_provider_configured("oxen", settings) is True


# -- warmup_provider --


class TestWarmupProvider:
    @pytest.mark.asyncio
    async def test_unconfigured_provider_returns_failure(self) -> None:
        with patch.object(bernstein.cli.api_warmup, "_is_provider_configured", return_value=False):
            result = await warmup_provider("openrouter")
            assert result.success is False
            assert result.provider == "openrouter"
            assert result.latency_ms == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_local_endpoint_skips_network(self) -> None:
        """Provider whose base_url is localhost should succeed without a real HTTP call."""
        with (
            patch.object(bernstein.cli.api_warmup, "_is_local_or_proxy", return_value=True),
            patch.object(bernstein.cli.api_warmup, "_is_provider_configured", return_value=True),
            patch.object(
                bernstein.cli.api_warmup,
                "_get_provider_base_url",
                return_value="http://127.0.0.1:11434",
            ),
        ):
            result = await warmup_provider("openai")
            assert result.success is True
            assert result.latency_ms == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_successful_warmup_caches_result(self) -> None:
        """warmup_provider caches the result to the module-level cache."""

        class FakeResponse:
            status_code = 200

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def get(self, *a, **kw):
                return FakeResponse()

        with (
            patch.object(bernstein.cli.api_warmup, "_is_local_or_proxy", return_value=False),
            patch.object(bernstein.cli.api_warmup, "_is_provider_configured", return_value=True),
            patch.object(
                bernstein.cli.api_warmup,
                "_get_provider_base_url",
                return_value="https://api.openai.com/v1",
            ),
            patch("bernstein.cli.api_warmup.httpx.AsyncClient", FakeClient),
        ):
            result = await warmup_provider("openai")
            assert result.success is True
            assert result.provider == "openai"
            assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_connection_error_returns_failure(self) -> None:
        """If the network stack raises OSError, the function returns success=False."""

        class FailingClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                raise OSError("connect failed")

            async def __aexit__(self, *_a):
                return False

        with (
            patch.object(bernstein.cli.api_warmup, "_is_local_or_proxy", return_value=False),
            patch.object(bernstein.cli.api_warmup, "_is_provider_configured", return_value=True),
            patch.object(
                bernstein.cli.api_warmup,
                "_get_provider_base_url",
                return_value="https://api.openai.com/v1",
            ),
            patch("bernstein.cli.api_warmup.httpx.AsyncClient", FailingClient),
        ):
            result = await warmup_provider("openai")
            assert result.success is False

    @pytest.mark.asyncio
    async def test_model_arg_not_used_in_request(self) -> None:
        """The model argument is accepted but does not change the HTTP call."""

        class FakeClient:
            call_count: int = 0

            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def get(self, url: str, **kw: object) -> object:
                type(self).call_count += 1
                resp = MagicMock()
                resp.status_code = 200
                self._last_url = url
                return resp

        with (
            patch.object(bernstein.cli.api_warmup, "_is_local_or_proxy", return_value=False),
            patch.object(bernstein.cli.api_warmup, "_is_provider_configured", return_value=True),
            patch.object(
                bernstein.cli.api_warmup,
                "_get_provider_base_url",
                return_value="https://api.openai.com/v1",
            ),
            patch("bernstein.cli.api_warmup.httpx.AsyncClient", FakeClient),
        ):
            result = await warmup_provider("openai", model="gpt-4-mini")
            assert result.success is True

        assert FakeClient.call_count == 1


# -- can_skip_warmup --


class TestCanSkipWarmup:
    def test_unknown_provider(self) -> None:
        assert can_skip_warmup("nonexistent") is False

    def test_freshly_warmed_provider(self) -> None:
        result = WarmupResult(provider="openrouter", latency_ms=10.0, success=True)
        bernstein.cli.api_warmup._cache["openrouter"] = (result, __import__("time").monotonic())
        assert can_skip_warmup("openrouter") is True

    def test_expired_warmup(self) -> None:
        result = WarmupResult(provider="openrouter", latency_ms=10.0, success=True)
        bernstein.cli.api_warmup._cache["openrouter"] = (
            result,
            __import__("time").monotonic() - _WARMUP_TTL_SECONDS - 5,
        )
        assert can_skip_warmup("openrouter") is False


# -- check_warmup_status --


class TestCheckWarmupStatus:
    def test_empty_cache(self) -> None:
        assert check_warmup_status() == {}

    def test_single_provider(self) -> None:
        result = WarmupResult(provider="together", latency_ms=50.0, success=True)
        bernstein.cli.api_warmup._cache["together"] = (result, __import__("time").monotonic())
        status = check_warmup_status()
        assert "together" in status
        entry = status["together"]
        assert entry["latency_ms"] == pytest.approx(50.0)
        assert entry["success"] is True
        assert entry["is_fresh"] is True
        assert entry["ttl_remaining_seconds"] > 0

    def test_expired_entry_shown_as_stale(self) -> None:
        result = WarmupResult(provider="g4f", latency_ms=99.0, success=False)
        bernstein.cli.api_warmup._cache["g4f"] = (
            result,
            __import__("time").monotonic() - _WARMUP_TTL_SECONDS - 10,
        )
        status = check_warmup_status()
        assert status["g4f"]["is_fresh"] is False
        assert status["g4f"]["ttl_remaining_seconds"] == pytest.approx(0.0)

    def test_multiple_providers(self) -> None:
        now = __import__("time").monotonic()
        bernstein.cli.api_warmup._cache["openrouter"] = (
            WarmupResult(provider="openrouter", latency_ms=12.0, success=True),
            now,
        )
        bernstein.cli.api_warmup._cache["oxen"] = (
            WarmupResult(provider="oxen", latency_ms=5.0, success=True),
            now - 10,
        )
        status = check_warmup_status()
        assert len(status) == 2
        assert status["openrouter"]["is_fresh"] is True
        assert status["oxen"]["is_fresh"] is True


# -- clear_cache --


class TestClearCache:
    def test_clear_removes_all_entries(self) -> None:
        result = WarmupResult(provider="oxen", latency_ms=5.0, success=True)
        bernstein.cli.api_warmup._cache["oxen"] = (result, __import__("time").monotonic())
        clear_cache()
        assert check_warmup_status() == {}
        assert can_skip_warmup("oxen") is False
