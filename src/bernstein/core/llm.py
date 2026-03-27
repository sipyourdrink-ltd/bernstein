"""Async native LLM client for Bernstein manager and external models."""

from __future__ import annotations

import logging

from openai import AsyncOpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

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


def get_client(provider: str) -> AsyncOpenAI:
    """Return a configured AsyncOpenAI client for the given provider."""
    settings = LLMSettings()

    if provider == "openrouter":
        if not settings.openrouter_api_key_paid:
            raise ValueError("Missing OPENROUTER_API_KEY_PAID")
        return AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key_paid,
        )

    if provider == "openrouter_free":
        api_key = settings.openrouter_api_key_free or settings.openrouter_api_key_paid
        if not api_key:
            raise ValueError("Missing Free OpenRouter API key")
        return AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )

    if provider == "oxen":
        if not settings.oxen_api_key:
            raise ValueError("Missing OXEN_API_KEY")
        # Ensure trailing slash isn't missing if needed, OpenAI client handles base_url.
        return AsyncOpenAI(
            base_url=settings.oxen_base_url,
            api_key=settings.oxen_api_key,
        )

    if provider == "together":
        if not settings.togetherai_user_key:
            raise ValueError("Missing TOGETHERAI_USER_KEY")
        return AsyncOpenAI(
            base_url="https://api.together.xyz/v1",
            api_key=settings.togetherai_user_key,
        )

    if provider == "g4f":
        if not settings.g4f_api_key:
            raise ValueError("Missing G4F_API_KEY")
        return AsyncOpenAI(
            base_url=settings.g4f_base_url,
            api_key=settings.g4f_api_key,
        )

    # Default to generic OpenAI
    if settings.openai_api_key:
        return AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )

    raise ValueError(f"Unknown or unconfigured provider: {provider}")


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

        choice = response.choices[0]
        content = choice.message.content
        return content if content is not None else ""

    except Exception as exc:
        logger.error("Native LLM call failed provider=%s, error=%s", provider, exc)
        raise RuntimeError(f"Native LLM call failed: {exc}") from exc


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
