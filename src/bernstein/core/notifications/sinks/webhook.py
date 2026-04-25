"""Generic webhook notification sink.

POSTs the full :class:`NotificationEvent` payload as JSON to a
user-supplied URL. The body shape matches
:meth:`NotificationEvent.to_payload` so downstream consumers can
deserialise it directly.
"""

from __future__ import annotations

import os
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks._http import post_json

__all__ = ["WebhookSink"]


class WebhookSink:
    """POST events as JSON to an arbitrary HTTP endpoint.

    Required config keys::

        id: <unique sink id>
        kind: webhook
        url: https://hooks.example.com/bernstein

    Optional::

        headers: {X-Token: ${OPS_TOKEN}}
        timeout_s: 10.0
    """

    kind: str = "webhook"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        url = _resolve(config.get("url"))
        if not url:
            raise NotificationPermanentError(
                f"webhook sink {self.sink_id!r} requires 'url'",
            )
        self._url = url
        raw_headers = config.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise NotificationPermanentError(
                f"webhook sink {self.sink_id!r} headers must be a mapping",
            )
        self._headers: dict[str, str] = {}
        for k, v in raw_headers.items():
            resolved = _resolve(v)
            if resolved is not None:
                self._headers[str(k)] = resolved
        self._timeout = float(config.get("timeout_s", 10.0))

    async def deliver(self, event: NotificationEvent) -> None:
        """POST the event payload to the configured URL."""
        await post_json(
            self._url,
            event.to_payload(),
            headers=self._headers or None,
            timeout=self._timeout,
        )

    async def close(self) -> None:
        """No-op."""


def _resolve(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value
