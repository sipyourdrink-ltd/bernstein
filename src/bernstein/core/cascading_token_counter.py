"""Cascading token count strategy: API → cheap model → bytes/4 estimation.

Provides ``count_tokens_cascading()`` — an async function that tries progressively
cheaper (but less accurate) token counting strategies in order:

1. **API tier**: Call the Anthropic ``/v1/messages/count_tokens`` endpoint for an
   exact token count.  Requires ``ANTHROPIC_API_KEY`` in the environment.

2. **Cheap model tier**: Ask a cheap OpenRouter/OpenAI model to estimate the token
   count via a short conversation.  Less precise but still API-based.

3. **Bytes/4 estimation tier**: Pure offline heuristic from
   ``token_estimation.estimate_tokens_for_text()``.  No network I/O, no API key
   required.  Always succeeds.

The cascade short-circuits as soon as a tier returns a value.  All failures are
logged at DEBUG level so callers can observe which tier was used.

Usage::

    count = await count_tokens_cascading("some text here")
    count = await count_tokens_cascading(text, model="claude-haiku-4-5-20251001")
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: Default Anthropic model used for API-tier counting.
_API_TIER_MODEL: str = "claude-haiku-4-5-20251001"

#: Timeout for API-tier HTTP requests in seconds.
_API_TIMEOUT_S: float = 5.0

#: Max text length to send to the API for counting (chars).
#: Texts beyond this are estimated without an API call.
_API_MAX_CHARS: int = 500_000


# ---------------------------------------------------------------------------
# Tier 1 — Anthropic API count_tokens endpoint
# ---------------------------------------------------------------------------


def _count_tokens_via_api(text: str, model: str) -> int | None:
    """Call the Anthropic count_tokens beta endpoint.

    Args:
        text: Text to count tokens for (wrapped in a user message).
        model: Anthropic model name for which tokens are counted.

    Returns:
        Exact token count from the API, or ``None`` on any failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("cascading_token_counter: ANTHROPIC_API_KEY not set, skipping API tier")
        return None

    if len(text) > _API_MAX_CHARS:
        logger.debug(
            "cascading_token_counter: text too large (%d chars) for API tier, skipping",
            len(text),
        )
        return None

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url="https://api.anthropic.com/v1/messages/count_tokens",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "token-counting-2024-11-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_S) as resp:  # noqa: S310
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            count: int = data["input_tokens"]
            logger.debug("cascading_token_counter: API tier returned %d tokens", count)
            return count
    except (urllib.error.URLError, KeyError, ValueError, OSError) as exc:
        logger.debug("cascading_token_counter: API tier failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Tier 2 — Cheap model via OpenRouter/OpenAI
# ---------------------------------------------------------------------------


async def _count_tokens_via_cheap_model(text: str) -> int | None:
    """Estimate token count by querying a cheap language model.

    Asks a cheap model (haiku via OpenRouter) to count the tokens in the
    provided text.  The response is parsed for the first integer-looking word.

    Args:
        text: Text to estimate token count for.

    Returns:
        Parsed token count, or ``None`` on any failure (network, parse, key).
    """
    try:
        from bernstein.core.llm import LLMSettings, get_client  # local import to avoid circular

        settings = LLMSettings()
        if not (settings.openrouter_api_key_paid or settings.openrouter_api_key_free):
            logger.debug("cascading_token_counter: no OpenRouter key, skipping cheap-model tier")
            return None

        provider = "openrouter" if settings.openrouter_api_key_paid else "openrouter_free"
        client = get_client(provider)

        # Truncate to avoid excessive cost on very long texts.
        sample = text[:8000] if len(text) > 8000 else text
        is_truncated = len(text) > 8000

        prompt = (
            f"Count the approximate number of LLM tokens in the following text. "
            f"Reply with ONLY a single integer — no explanation, no units, just the number.\n\n"
            f"{'[First 8000 chars of text:]' if is_truncated else '[Text:]'}\n{sample}"
        )

        resp = await client.chat.completions.create(
            model="anthropic/claude-haiku-4-5",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16,
            temperature=0.0,
        )

        raw = (resp.choices[0].message.content or "").strip()
        # Parse first integer from response.
        for token in raw.split():
            cleaned = token.replace(",", "").strip()
            if cleaned.isdigit():
                count = int(cleaned)
                # Scale up if text was truncated.
                if is_truncated:
                    count = int(count * len(text) / 8000)
                logger.debug("cascading_token_counter: cheap-model tier returned %d tokens", count)
                return count

        logger.debug("cascading_token_counter: cheap-model response unparseable: %r", raw)
        return None

    except Exception as exc:
        logger.debug("cascading_token_counter: cheap-model tier failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Tier 3 — bytes/4 estimation
# ---------------------------------------------------------------------------


def _count_tokens_bytes_estimate(text: str) -> int:
    """Estimate token count via bytes/4 heuristic.

    This is the final fallback tier — always available, no I/O, no API keys.

    Args:
        text: Text to estimate.

    Returns:
        Estimated token count (bytes / 4, rounded down).
    """
    from bernstein.core.token_estimation import estimate_tokens_for_text

    count = estimate_tokens_for_text(text, assumed_type="code")
    logger.debug("cascading_token_counter: bytes-estimate tier returned %d tokens", count)
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def count_tokens_cascading(
    text: str,
    model: str = _API_TIER_MODEL,
    *,
    skip_api: bool = False,
    skip_cheap_model: bool = False,
) -> int:
    """Count tokens in *text* using a three-tier cascading fallback strategy.

    Tries tiers in order until one succeeds:

    1. **API tier** — Anthropic ``/v1/messages/count_tokens`` (exact, requires
       ``ANTHROPIC_API_KEY``).
    2. **Cheap model tier** — Ask a haiku-class model via OpenRouter (requires
       ``OPENROUTER_API_KEY_PAID`` or ``OPENROUTER_API_KEY_FREE``).
    3. **bytes/4 estimation** — Offline heuristic, always available.

    Args:
        text: Text to count tokens for.
        model: Anthropic model name for API-tier counting.
        skip_api: When ``True``, skip tier 1 entirely (useful for tests or
            offline environments).
        skip_cheap_model: When ``True``, skip tier 2 (falls directly to bytes/4).

    Returns:
        Token count estimate. Tier 1 is exact; tiers 2 and 3 are approximate.
    """
    if not text:
        return 0

    # Tier 1: Anthropic API (sync but fast enough — no event loop blocking concern
    # for typical prompt sizes, and avoids adding async to the HTTP call).
    if not skip_api:
        result = _count_tokens_via_api(text, model)
        if result is not None:
            return result

    # Tier 2: Cheap model via OpenRouter.
    if not skip_cheap_model:
        result = await _count_tokens_via_cheap_model(text)
        if result is not None:
            return result

    # Tier 3: bytes/4 estimate — always succeeds.
    return _count_tokens_bytes_estimate(text)
