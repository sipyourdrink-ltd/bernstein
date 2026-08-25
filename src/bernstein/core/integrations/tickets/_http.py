"""Tiny HTTP helper shared by ticket providers.

Each provider module (Linear GraphQL, GitHub Issues REST, Jira REST)
needs the same fetch capabilities:
- Prefer ``httpx`` when available, fall back to ``urllib.request``.
- URL scheme validation via ``ensure_http_url``.
- Honor ``Retry-After`` on 429 (rate limits) and 503 (service unavailable)
  with jittered exponential backoff.
- Circuit breaker integration via ``ProviderCircuitBreakerRegistry`` to fast-fail
  providers experiencing extended outages.
- Typed exception mapping:
  - 401/403 -> :class:`TicketAuthError`
  - 429 (exhausted) -> :class:`TicketRateLimitError`
  - OPEN circuit -> :class:`TicketCircuitOpenError`
  - other 4xx/5xx -> :class:`TicketParseError`
"""

from __future__ import annotations

import json
import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, cast

from bernstein.core.integrations.tickets import (
    TicketAuthError,
    TicketCircuitOpenError,
    TicketParseError,
    TicketRateLimitError,
)
from bernstein.core.observability.provider_circuit_breaker import (
    ProviderCircuitBreaker,
    ProviderCircuitBreakerRegistry,
)
from bernstein.core.security.url_allowlist import UrlSchemeError, ensure_http_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_MAX_BACKOFF_S = 30.0

# Process-wide circuit breaker registry for ticket providers
_DEFAULT_REGISTRY = ProviderCircuitBreakerRegistry()


def _parse_retry_after(header_val: str | None) -> float | None:
    """Parse a Retry-After header into delay seconds, or return None."""
    if not header_val:
        return None
    val = header_val.strip()
    try:
        seconds = float(val)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        delay = dt.timestamp() - time.time()
        return max(0.0, delay)
    except Exception:
        return None


def http_request_json(
    *,
    method: str = "GET",
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any] | None = None,
    provider_label: str,
    auth_env_var: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    circuit_breaker: ProviderCircuitBreaker | None = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """Execute an HTTP request and decode the JSON response with retries and circuit breaker."""
    try:
        ensure_http_url(url, allow_http=True, source=f"tickets.{provider_label}")
    except UrlSchemeError as exc:
        raise TicketParseError(str(exc)) from exc

    breaker = circuit_breaker if circuit_breaker is not None else _DEFAULT_REGISTRY.get_breaker(provider_label.lower())
    if not breaker.should_allow():
        raise TicketCircuitOpenError(
            f"{provider_label} circuit breaker is OPEN; fast-failing request",
            provider=provider_label,
        )

    attempt = 0
    total_waited_s = 0.0

    while True:
        try:
            import httpx

            if method.upper() == "POST":
                resp = httpx.post(url, headers=headers, json=json_data, timeout=timeout)
            else:
                resp = httpx.get(url, headers=headers, timeout=timeout)

            # 1. Auth errors fail immediately
            if resp.status_code in (401, 403):
                raise TicketAuthError(
                    f"{provider_label} rejected the request (HTTP {resp.status_code}). "
                    f"Check the {auth_env_var} environment variable."
                )

            # 2. Rate limit (429) or Service Unavailable (503) -> Retry
            if resp.status_code in (429, 503):
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if attempt < max_retries:
                    attempt += 1
                    if retry_after is not None:
                        wait_s = min(retry_after, max_backoff_s)
                    else:
                        jitter = random.uniform(0, 0.1 * backoff_base_s)
                        wait_s = min(max_backoff_s, (backoff_base_s * (2 ** (attempt - 1))) + jitter)
                    logger.warning(
                        "%s returned HTTP %d (attempt %d/%d), retrying in %.2fs...",
                        provider_label,
                        resp.status_code,
                        attempt,
                        max_retries,
                        wait_s,
                    )
                    sleep_fn(wait_s)
                    total_waited_s += wait_s
                    continue
                else:
                    if resp.status_code == 429:
                        breaker.record_failure()
                        raise TicketRateLimitError(
                            f"{provider_label} rate limit exceeded (HTTP 429); "
                            f"retries exhausted after {total_waited_s:.1f}s (last Retry-After: {retry_after})",
                            provider=provider_label,
                            retry_after_s=retry_after,
                        )
                    breaker.record_failure()
                    raise TicketParseError(
                        f"{provider_label} API returned HTTP {resp.status_code} "
                        f"after {max_retries} retries: {resp.text[:200]}"
                    )

            # 3. Other 4xx client errors (e.g. 404) fail immediately
            if 400 <= resp.status_code < 500:
                raise TicketParseError(f"{provider_label} API returned HTTP {resp.status_code}: {resp.text[:200]}")

            # 4. Server errors (5xx)
            if resp.status_code >= 500:
                breaker.record_failure()
                raise TicketParseError(f"{provider_label} API returned HTTP {resp.status_code}: {resp.text[:200]}")

            # Success
            breaker.record_success()
            return cast(dict[str, Any], resp.json())

        except ImportError:  # pragma: no cover - fallback when httpx is not imported
            import urllib.error
            import urllib.request

            req_data = json.dumps(json_data).encode("utf-8") if json_data is not None else None
            req = urllib.request.Request(url, data=req_data, headers=headers, method=method.upper())
            try:
                # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                with urllib.request.urlopen(req, timeout=timeout) as handle:
                    raw = handle.read().decode("utf-8")
                    breaker.record_success()
                    return cast(dict[str, Any], json.loads(raw))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TicketAuthError(
                        f"{provider_label} rejected the request (HTTP {exc.code}). "
                        f"Check the {auth_env_var} environment variable."
                    ) from exc
                if exc.code in (429, 503) and attempt < max_retries:
                    retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                    attempt += 1
                    # Mirrors the httpx branch exactly: a server-supplied
                    # Retry-After is honoured but still capped, so a provider
                    # answering `Retry-After: 3600` cannot park the calling
                    # thread for an hour, and the un-hinted backoff carries the
                    # same jitter so two clients retrying together spread out.
                    if retry_after is not None:
                        wait_s = min(retry_after, max_backoff_s)
                    else:
                        jitter = random.uniform(0, 0.1 * backoff_base_s)
                        wait_s = min(max_backoff_s, (backoff_base_s * (2 ** (attempt - 1))) + jitter)
                    sleep_fn(wait_s)
                    total_waited_s += wait_s
                    continue
                if exc.code == 429:
                    breaker.record_failure()
                    raise TicketRateLimitError(
                        f"{provider_label} rate limit exceeded (HTTP 429); retries exhausted",
                        provider=provider_label,
                    ) from exc
                if exc.code >= 500:
                    breaker.record_failure()
                raise TicketParseError(f"{provider_label} API returned HTTP {exc.code}") from exc
        except (TicketAuthError, TicketParseError, TicketRateLimitError, TicketCircuitOpenError):
            raise
        except Exception as exc:
            breaker.record_failure()
            raise TicketParseError(f"{provider_label} request failed: {exc}") from exc


def http_get_json(
    *,
    url: str,
    headers: dict[str, str],
    provider_label: str,
    auth_env_var: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    circuit_breaker: ProviderCircuitBreaker | None = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """GET ``url`` and decode the JSON body with rate limit retries and circuit breaker."""
    return http_request_json(
        method="GET",
        url=url,
        headers=headers,
        provider_label=provider_label,
        auth_env_var=auth_env_var,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        max_backoff_s=max_backoff_s,
        circuit_breaker=circuit_breaker,
        sleep_fn=sleep_fn,
    )


def http_post_json(
    *,
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any],
    provider_label: str,
    auth_env_var: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    circuit_breaker: ProviderCircuitBreaker | None = None,
    sleep_fn: Any = time.sleep,
) -> dict[str, Any]:
    """POST ``url`` with JSON data and decode the response with rate limit retries and circuit breaker."""
    return http_request_json(
        method="POST",
        url=url,
        headers=headers,
        json_data=json_data,
        provider_label=provider_label,
        auth_env_var=auth_env_var,
        timeout=timeout,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        max_backoff_s=max_backoff_s,
        circuit_breaker=circuit_breaker,
        sleep_fn=sleep_fn,
    )
