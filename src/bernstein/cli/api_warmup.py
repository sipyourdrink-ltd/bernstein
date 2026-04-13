"""API preconnect warmup -- send a minimal request to warm provider connections.

Measures latency and caches results per provider/model with a TTL so that
subsequent lookups know whether a warmup call already succeeded recently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bernstein.core.llm import LLMSettings

logger = logging.getLogger(__name__)

# How long a warmup result remains valid.
_WARMUP_TTL_SECONDS: float = 5 * 60  # 5 minutes

# Minimal request timeout for warmup pings.
_WARMUP_TIMEOUT_SECONDS: float = 10.0

# Base URL -> provider mapping used by get_client().
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_PROVIDER_BASE_URLS: dict[str, str] = {
    "openrouter": _OPENROUTER_BASE_URL,
    "openrouter_free": _OPENROUTER_BASE_URL,
    "oxen": "https://hub.oxen.ai/api",
    "together": "https://api.together.xyz/v1",
    "g4f": "https://g4f.space/v1",
    "openai": "https://api.openai.com/v1",
}

# In-memory cache: provider -> (WarmupResult, timestamp).
_cache: dict[str, tuple[WarmupResult, float]] = {}


@dataclass(frozen=True)
class WarmupResult:
    """Result of a warmup probe for a provider."""

    provider: str
    latency_ms: float
    success: bool


def _is_local_or_proxy(base_url: str) -> bool:
    """Return True when *base_url* points to localhost or a Unix socket."""
    lower = base_url.lower()
    return any(
        lower.startswith(prefix) for prefix in ("http://localhost", "http://127.0.0.1", "http://0.0.0.0", "unix://")
    )


def _get_provider_base_url(provider: str, settings: LLMSettings) -> str:
    """Return the base API URL for the given provider name."""
    if provider in ("openrouter", "openrouter_free"):
        return _OPENROUTER_BASE_URL
    if provider == "oxen":
        return settings.oxen_base_url
    if provider == "together":
        return "https://api.together.xyz/v1"
    if provider == "g4f":
        return settings.g4f_base_url
    if settings.openai_api_key:
        return settings.openai_base_url or "https://api.openai.com/v1"
    return _PROVIDER_BASE_URLS.get(provider, "")


def _is_provider_configured(provider: str, settings: LLMSettings) -> bool:
    """Return True when the provider has credentials configured."""
    if provider == "openrouter":
        return bool(settings.openrouter_api_key_paid)
    if provider == "openrouter_free":
        return bool(settings.openrouter_api_key_free or settings.openrouter_api_key_paid)
    if provider == "oxen":
        return bool(settings.oxen_api_key)
    if provider == "together":
        return bool(settings.togetherai_user_key)
    if provider == "g4f":
        return bool(settings.g4f_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    return False


async def warmup_provider(
    provider: str,
    _model: str = "",
    *,
    timeout: float = _WARMUP_TIMEOUT_SECONDS,
) -> WarmupResult:
    """Send a minimal request to pre-connect and measure latency.

    Fires a small HTTP request to the provider's API endpoint -- enough to open
    a connection pool slot and record round-trip latency.  Localhost and
    Unix-socket endpoints are skipped because they are already "warm."

    Args:
        provider: Provider name as understood by Bernstein
            (openrouter, openrouter_free, oxen, together, g4f, openai).
        model: Model name (used in log messages; not sent in the minimal ping).
        timeout: HTTP timeout in seconds.

    Returns:
        WarmupResult with provider name, measured latency, and success flag.
    """
    settings = LLMSettings()

    if not _is_provider_configured(provider, settings):
        logger.debug(
            "Skipping warmup for unconfigured provider: %s",
            provider,
        )
        return WarmupResult(provider=provider, latency_ms=0.0, success=False)

    base_url = _get_provider_base_url(provider, settings)
    if _is_local_or_proxy(base_url):
        logger.debug(
            "Skipping warmup for local/proxy endpoint: %s (%s)",
            provider,
            base_url,
        )
        return WarmupResult(provider=provider, latency_ms=0.0, success=True)

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": "Bearer _ping"},
            )
            latency_ms = (time.monotonic() - start) * 1000
            success = response.status_code < 500
            result = WarmupResult(
                provider=provider,
                latency_ms=round(latency_ms, 2),
                success=success,
            )
            _cache[provider] = (result, time.monotonic())
            logger.info(
                "Warmup %s %s -- %.1fms (HTTP %d)",
                provider,
                "OK" if result.success else "FAIL",
                result.latency_ms,
                response.status_code,
            )
            return result
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
        latency_ms = (time.monotonic() - start) * 1000
        result = WarmupResult(
            provider=provider,
            latency_ms=round(latency_ms, 2),
            success=False,
        )
        _cache[provider] = (result, time.monotonic())
        logger.warning("Warmup %s failed: %s", provider, exc)
        return result


def can_skip_warmup(provider: str) -> bool:
    """Return True when the provider was already warmed within the TTL."""
    if provider not in _cache:
        return False
    record_ts = _cache[provider][1]
    return (time.monotonic() - record_ts) < _WARMUP_TTL_SECONDS


def check_warmup_status() -> dict[str, Any]:
    """Return status of all warmed providers with TTL awareness.

    Returns:
        Mapping of provider name to dict with keys:
            - ``latency_ms`` (float): measured round-trip latency
            - ``success`` (bool): whether the warmup call succeeded
            - ``is_fresh`` (bool): True if still within TTL window
            - ``ttl_remaining_seconds`` (float): seconds until cache expiry
    """
    now = time.monotonic()
    result: dict[str, Any] = {}
    for provider, (warmup, cached_at) in _cache.items():
        age = now - cached_at
        is_fresh = age < _WARMUP_TTL_SECONDS
        result[provider] = {
            "latency_ms": warmup.latency_ms,
            "success": warmup.success,
            "is_fresh": is_fresh,
            "ttl_remaining_seconds": max(0.0, _WARMUP_TTL_SECONDS - age),
        }
    return result


def clear_cache() -> None:
    """Flush the entire warmup cache.

    Primarily useful for testing and explicit cache invalidation.
    """
    _cache.clear()
