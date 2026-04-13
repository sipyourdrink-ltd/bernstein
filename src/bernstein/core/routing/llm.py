"""Async native LLM client for Bernstein manager and external models."""

from __future__ import annotations

import asyncio as _asyncio
import logging
import time
import urllib.request as _urllib_request
from typing import TYPE_CHECKING

from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class LLMSettings(BaseSettings):
    """Configuration for external LLM providers."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openrouter_api_key_paid: str | None = None
    openrouter_api_key_free: str | None = None

    oxen_api_key: str | None = None
    oxen_base_url: str = "https://hub.oxen.ai/api"

    togetherai_user_key: str | None = None

    g4f_api_key: str | None = None
    g4f_base_url: str = "https://g4f.space/v1"

    openai_api_key: str | None = None
    openai_base_url: str | None = None

    tavily_api_key: str | None = None


def _resolve_provider_config(provider: str, settings: LLMSettings) -> tuple[str, str]:
    """Resolve a provider name to (base_url, api_key).

    Args:
        provider: Provider name.
        settings: LLM settings with API keys.

    Returns:
        Tuple of (base_url, api_key).

    Raises:
        ValueError: If the provider is unknown or unconfigured.
    """
    if provider == "openrouter":
        if not settings.openrouter_api_key_paid:
            raise ValueError("Missing OPENROUTER_API_KEY_PAID")
        return "https://openrouter.ai/api/v1", settings.openrouter_api_key_paid

    if provider == "openrouter_free":
        api_key = settings.openrouter_api_key_free or settings.openrouter_api_key_paid
        if not api_key:
            raise ValueError("Missing Free OpenRouter API key")
        return "https://openrouter.ai/api/v1", api_key

    if provider == "oxen":
        if not settings.oxen_api_key:
            raise ValueError("Missing OXEN_API_KEY")
        return settings.oxen_base_url, settings.oxen_api_key

    if provider == "together":
        if not settings.togetherai_user_key:
            raise ValueError("Missing TOGETHERAI_USER_KEY")
        return "https://api.together.xyz/v1", settings.togetherai_user_key

    if provider == "g4f":
        if not settings.g4f_api_key:
            raise ValueError("Missing G4F_API_KEY")
        return settings.g4f_base_url, settings.g4f_api_key

    if settings.openai_api_key:
        return settings.openai_base_url or "https://api.openai.com/v1", settings.openai_api_key

    raise ValueError(f"Unknown or unconfigured provider: {provider}")


def get_client(provider: str) -> AsyncOpenAI:
    """Return a configured AsyncOpenAI client for the given provider."""
    settings = LLMSettings()
    base_url, api_key = _resolve_provider_config(provider, settings)
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


# Known CLI binaries and their prompt/model flag conventions.
_CLI_FLAGS: dict[str, tuple[str, str, list[str]]] = {
    "gemini": ("-p", "-m", []),
    "qwen": ("-y", "--model", []),
    "codex": ("--prompt", "--model", []),
    "goose": ("--prompt", "--model", []),
    "aider": ("--message", "--model", []),
    "claude": ("--print -p", "--model", ["--output-format", "text", "--max-turns", "1"]),
}


async def _call_cli_provider(prompt: str, model: str, provider: str) -> str:
    """Run the LLM call via a CLI subprocess.

    Handles the Claude-specific path as well as generic CLI adapters.

    Args:
        prompt: Full prompt string.
        model: Model name.
        provider: CLI provider name (binary or adapter).

    Returns:
        The text response.

    Raises:
        RuntimeError: If the CLI call fails.
    """
    import shlex

    cli_binary = provider
    if provider in _CLI_FLAGS:
        prompt_flag, model_flag, extra = _CLI_FLAGS[provider]
    else:
        prompt_flag, model_flag, extra = "-p", "-m", []
        try:
            from bernstein.adapters.registry import get_adapter

            get_adapter(provider)
            logger.debug("Using registered adapter '%s' as internal LLM CLI", provider)
        except Exception:
            pass

    logger.debug("Calling %s CLI: model=%s", cli_binary, model)
    try:
        cmd: list[str] = [cli_binary]
        cmd.extend(shlex.split(prompt_flag))
        cmd.append(prompt)
        cmd.extend(shlex.split(model_flag))
        cmd.append(model)
        cmd.extend(extra)
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await _asyncio.wait_for(proc.communicate(), timeout=120)
        _stdout = stdout_bytes.decode() if stdout_bytes else ""
        _stderr = stderr_bytes.decode() if stderr_bytes else ""
        if proc.returncode != 0:
            raise RuntimeError(f"{cli_binary} CLI exited {proc.returncode}: {_stderr[:200]}")
        return _stdout.strip()
    except TimeoutError as exc:
        raise RuntimeError(f"{cli_binary} CLI timed out after 120s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"{cli_binary} CLI not found — install it first") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error("%s CLI call failed: %s", cli_binary, exc)
        raise RuntimeError(f"{cli_binary} CLI call failed: {exc}") from exc


async def _call_api_provider(
    prompt: str,
    model: str,
    provider: str,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    """Run the LLM call via the OpenAI-compatible API client.

    Args:
        prompt: Full prompt string.
        model: Model name.
        provider: API provider name.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.

    Returns:
        The text response.

    Raises:
        RuntimeError: If the API call fails.
    """
    client = get_client(provider)
    logger.debug("Calling LLM API using provider=%s, model=%s", provider, model)

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not response.choices:
            return ""
        content = response.choices[0].message.content
        return content if content is not None else ""
    except Exception as exc:
        logger.error("Native LLM call failed provider=%s, error=%s", provider, exc)
        raise RuntimeError(f"Native LLM call failed: {exc}") from exc


async def call_llm(
    prompt: str,
    model: str,
    provider: str = "openrouter_free",
    *,
    max_tokens: int = 4000,
    temperature: float = 0.7,
) -> str:
    """Invoke the LLM cleanly via a native async client.

    Args:
        prompt: Full prompt string.
        model: Model name.
        provider: 'openrouter', 'openrouter_free', 'oxen', 'together', 'g4f', ...
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        The text response from the LLM.

    Raises:
        RuntimeError: If the API call fails.
    """
    from bernstein.core.deterministic import get_active_store

    _store = get_active_store()
    if _store is not None:
        _replay = _store.get_replay(prompt, model)
        if _replay is not None:
            logger.debug("DeterministicStore: replaying cached response for model=%s", model)
            return _replay

    # API-based providers go through the OpenAI-compatible client.
    _API_PROVIDERS = {"openrouter", "openrouter_free", "oxen", "together", "g4f"}

    if provider in _API_PROVIDERS:
        result = await _call_api_provider(prompt, model, provider, max_tokens=max_tokens, temperature=temperature)
    else:
        result = await _call_cli_provider(prompt, model, provider)

    if _store is not None:
        _store.record(prompt, model, result)
    return result


async def tavily_search(query: str, max_results: int = 5) -> str:
    """Perform a web search using Tavily API.

    Args:
        query: Search query.
        max_results: Max results to return.

    Returns:
        Formatted markdown string of search results.
    """
    import httpx

    settings = LLMSettings()
    if not settings.tavily_api_key:
        logger.warning("Tavily API key missing. Cannot perform web search.")
        return ""

    logger.info("Performing Tavily web search for: %r", query)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        if not results:
            return "(No relevant web results found.)"

        formatted: list[str] = []  # type: ignore[reportUnknownVariableType]
        for r in results:
            title = r.get("title", "Untitled")
            content = r.get("content", "")
            url = r.get("url", "")
            formatted.append(f"**{title}**\n{content}\nSource: {url}")  # type: ignore[reportUnknownMemberType]

        return "\n\n".join(formatted)

    except Exception as exc:
        logger.error("Tavily search failed: %s", exc)
        return f"(Web search failed: {exc})"


# ---------------------------------------------------------------------------
# API preconnect pool warming (T581)
# ---------------------------------------------------------------------------


async def preconnect_api(
    base_url: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Warm the HTTP connection pool with a HEAD request (T581).

    Fires a fire-and-forget HEAD request to *base_url* to establish the
    TCP connection before the first real API call.  Skips local/proxy
    providers (localhost, 127.x, 0.0.0.0).

    Args:
        base_url: API base URL to warm.
        timeout: Request timeout in seconds.

    Returns:
        True if the preconnect succeeded, False otherwise.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or ""
    # Skip local/proxy providers
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.startswith("192.168."):
        logger.debug("Skipping preconnect for local provider: %s", base_url)
        return False

    try:
        loop = _asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: _urllib_request.urlopen(
                _urllib_request.Request(base_url, method="HEAD"),
                timeout=timeout,
            ),
        )
        logger.debug("API preconnect succeeded: %s", base_url)
        return True
    except Exception as exc:
        logger.debug("API preconnect failed (non-fatal): %s — %s", base_url, exc)
        return False


# ---------------------------------------------------------------------------
# OAuth refresh on 401/403 errors (T568)
# ---------------------------------------------------------------------------


class LLMOAuthRefreshHandler:
    """Handles OAuth token refresh for LLM providers on 401/403 errors."""

    def __init__(self):
        self.refresh_callbacks: dict[str, Callable[[], str | None]] = {}
        self.last_refresh_attempt: dict[str, float] = {}
        self.refresh_cooldown = 60  # seconds between refresh attempts

    def register_provider_refresh(self, provider: str, refresh_callback: Callable[[], str | None]) -> None:
        """Register OAuth refresh callback for an LLM provider."""
        self.refresh_callbacks[provider] = refresh_callback
        logger.info(f"Registered OAuth refresh for LLM provider: {provider}")

    def handle_llm_auth_error(self, provider: str, error_code: int, error_message: str) -> bool:
        """Handle 401/403 errors from LLM providers."""
        if error_code not in (401, 403):
            return False

        current_time = time.time()
        last_attempt = self.last_refresh_attempt.get(provider, 0)

        # Check cooldown
        if current_time - last_attempt < self.refresh_cooldown:
            logger.debug(f"OAuth refresh cooldown active for LLM provider: {provider}")
            return False

        refresh_callback = self.refresh_callbacks.get(provider)
        if not refresh_callback:
            logger.warning(f"No OAuth refresh registered for LLM provider: {provider}")
            return False

        logger.info(f"Attempting OAuth refresh for LLM provider: {provider}")
        self.last_refresh_attempt[provider] = current_time

        try:
            new_token = refresh_callback()
            if new_token:
                logger.info(f"OAuth refresh successful for LLM provider: {provider}")
                return True
            else:
                logger.warning(f"OAuth refresh failed for {provider}: no token returned")
                return False
        except Exception as e:
            logger.error(f"OAuth refresh error for {provider}: {e}")
            return False


# Global LLM OAuth refresh handler
_llm_oauth_handler = LLMOAuthRefreshHandler()


def handle_llm_auth_error(provider: str, error_code: int, error_message: str) -> bool:
    """Handle LLM provider authentication errors with OAuth refresh (T568)."""
    return _llm_oauth_handler.handle_llm_auth_error(provider, error_code, error_message)


def register_llm_oauth_refresh(provider: str, refresh_callback: Callable[[], str | None]) -> None:
    """Register OAuth refresh callback for an LLM provider."""
    _llm_oauth_handler.register_provider_refresh(provider, refresh_callback)


def retry_with_oauth_refresh(provider: str, error_code: int, retry_count: int) -> bool:
    """Determine if an LLM request should be retried after OAuth refresh."""
    if error_code not in (401, 403):
        return False

    if retry_count >= 2:  # Max 2 retries after refresh
        return False

    current_time = time.time()
    last_attempt = _llm_oauth_handler.last_refresh_attempt.get(provider, 0)

    # Only retry if we recently attempted a refresh
    return current_time - last_attempt < 30  # 30 seconds window
