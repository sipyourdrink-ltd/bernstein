"""Cascading token count strategy — three-level fallback.

Attempts to count tokens with increasing cost and decreasing accuracy:
1. Anthropic API ``/v1/messages/count_tokens`` (exact, free if you have a key).
2. Cheap LLM model (e.g., claude-haiku) asked to count tokens in the text.
3. Bytes-based estimation via ``token_estimation.estimate_tokens_for_text``.

Each level catches all exceptions and returns ``None`` on failure so that the
cascade falls through to the next level automatically.

Usage::

    count = await count_tokens("def foo(): pass", file_type="code")
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from bernstein.core.llm import call_llm
from bernstein.core.token_estimation import estimate_tokens_for_text

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"
_ANTHROPIC_API_VERSION = "2023-06-01"


# ---------------------------------------------------------------------------
# Individual strategy implementations
# ---------------------------------------------------------------------------


async def count_tokens_via_api(
    text: str,
    model: str = _DEFAULT_MODEL,
) -> int:
    """Count tokens by calling Anthropic's ``/v1/messages/count_tokens`` endpoint.

    Reads ``ANTHROPIC_API_KEY`` from the environment.  Raises on failure so
    that the caller's cascade logic can fall through to the next strategy.

    Args:
        text: The text whose tokens should be counted.
        model: Anthropic model name used for the tokenisation context.

    Returns:
        Exact token count reported by the API.

    Raises:
        RuntimeError: If the API call fails or the key is missing.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(_ANTHROPIC_COUNT_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    input_tokens = data.get("input_tokens")
    if input_tokens is None:
        raise RuntimeError(f"Unexpected API response: {data!r}")
    return int(input_tokens)


async def count_tokens_via_cheap_model(
    text: str,
    model: str = _DEFAULT_MODEL,
) -> int:
    """Ask a cheap LLM to count tokens in *text*.

    Sends the text with a short prompt asking for a token count and parses
    the first integer from the response.  Uses ``openrouter_free`` provider by
    default.

    Args:
        text: The text whose tokens should be counted.
        model: Model to use for the token-counting request.

    Returns:
        Token count extracted from the model's response.

    Raises:
        RuntimeError: If the model call fails or no integer is found in the
            response.
    """
    prompt = (
        "How many tokens does the following text contain when tokenised for "
        "an LLM? Reply with just the integer number, nothing else.\n\n"
        f"TEXT:\n{text}"
    )
    response = await call_llm(prompt, model=model, provider="openrouter_free", max_tokens=32, temperature=0.0)
    matches = re.findall(r"\d+", response.strip())
    if not matches:
        raise RuntimeError(f"No integer found in cheap-model response: {response!r}")
    return int(matches[0])


def count_tokens_via_estimation(text: str, file_type: str = "code") -> int:
    """Estimate token count using a bytes-per-token ratio.

    A thin wrapper around :func:`bernstein.core.token_estimation.estimate_tokens_for_text`
    that documents the role of this function within the cascade.

    Args:
        text: Text content to estimate.
        file_type: Content category — one of ``"code"``, ``"json"``,
            ``"text"``, ``"markup"``, or ``"default"``.

    Returns:
        Estimated token count (integer, floor division).
    """
    return estimate_tokens_for_text(text, assumed_type=file_type)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def count_tokens(
    text: str,
    *,
    file_type: str = "code",
    model: str | None = None,
) -> int:
    """Count tokens using a three-level cascade.

    Attempts each strategy in order, falling through to the next if the
    current one raises any exception:

    1. Anthropic ``/v1/messages/count_tokens`` API (exact).
    2. Cheap LLM asked to count tokens (approximate, costs API credits).
    3. Bytes / bytes-per-token ratio (free, least accurate).

    Args:
        text: Text whose token count is requested.
        file_type: Content category for the bytes-estimation fallback.
            One of ``"code"``, ``"json"``, ``"text"``, ``"markup"``,
            ``"default"``.
        model: Optional model name override passed to levels 1 and 2.

    Returns:
        Token count (always a positive integer; may be 0 for empty text).
    """
    effective_model = model or _DEFAULT_MODEL

    # Level 1 — Anthropic API
    try:
        tokens = await count_tokens_via_api(text, model=effective_model)
        log.debug("Token count via API: %d", tokens)
        return tokens
    except Exception as exc:
        log.debug("API token count failed (%s); trying cheap model", exc)

    # Level 2 — Cheap LLM
    try:
        tokens = await count_tokens_via_cheap_model(text, model=effective_model)
        log.debug("Token count via cheap model: %d", tokens)
        return tokens
    except Exception as exc:
        log.debug("Cheap-model token count failed (%s); falling back to estimation", exc)

    # Level 3 — Bytes estimation (never fails)
    tokens = count_tokens_via_estimation(text, file_type=file_type)
    log.debug("Token count via bytes estimation: %d", tokens)
    return tokens
