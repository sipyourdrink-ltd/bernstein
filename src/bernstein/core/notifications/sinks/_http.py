"""Shared HTTP plumbing for webhook-style notification sinks.

Centralised so :mod:`slack`, :mod:`discord`, and :mod:`webhook` don't
each duplicate the retry-classification logic. ``httpx`` is already a
core dependency (see ``pyproject.toml::dependencies``), so importing it
here is safe.
"""

from __future__ import annotations

from typing import Any

import httpx

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationPermanentError,
)

__all__ = ["DEFAULT_TIMEOUT_S", "post_json"]

DEFAULT_TIMEOUT_S: float = 10.0

# 4xx codes that we treat as transient even though they're client errors.
# 408 (timeout), 425 (too early), 429 (rate-limited) — retrying may help.
_TRANSIENT_4XX = frozenset({408, 425, 429})


async def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """POST ``payload`` to ``url`` and return the response body.

    Classifies failures into the two error classes the dispatcher
    understands:

    * 5xx, network errors, transient 4xx → :class:`NotificationDeliveryError`
    * other 4xx → :class:`NotificationPermanentError`

    Args:
        url: Destination URL.
        payload: JSON-serialisable body.
        headers: Optional extra request headers.
        timeout: Per-request timeout in seconds.
        transport: Optional transport override (used by tests).

    Returns:
        The response body as text (drivers usually ignore it).
    """
    request_headers = {"content-type": "application/json"}
    if headers:
        request_headers.update(headers)
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, json=payload, headers=request_headers)
    except httpx.TimeoutException as exc:
        raise NotificationDeliveryError(f"http timeout for {url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise NotificationDeliveryError(f"http error for {url}: {exc}") from exc

    if response.is_success:
        return response.text

    code = response.status_code
    body = response.text[:500]
    if 500 <= code < 600 or code in _TRANSIENT_4XX:
        raise NotificationDeliveryError(f"http {code} from {url}: {body}")
    raise NotificationPermanentError(f"http {code} from {url}: {body}")
