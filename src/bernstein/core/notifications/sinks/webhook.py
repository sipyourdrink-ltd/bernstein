"""Generic webhook notification sink.

POSTs the full :class:`NotificationEvent` payload as JSON to a
user-supplied URL. The body shape matches
:meth:`NotificationEvent.to_payload` so downstream consumers can
deserialise it directly.

Automation bridge (#2512): when the install can anchor one, the body carries an
additional :data:`~bernstein.core.trigger_sources.receipt.PROOF_ENVELOPE_KEY`
key holding a signed, chain-anchored status proof. A workflow step that gates on
"the run succeeded" can then check the status it was told against the audit
chain instead of trusting the transport. The proof is strictly additive: every
key :meth:`NotificationEvent.to_payload` produced survives verbatim under its
original name, so consumers written against the plain payload keep parsing it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks._http import post_json

__all__ = ["WebhookSink"]

logger = logging.getLogger(__name__)


class WebhookSink:
    """POST events as JSON to an arbitrary HTTP endpoint.

    Required config keys::

        id: <unique sink id>
        kind: webhook
        url: https://hooks.example.com/bernstein

    Optional::

        headers: {X-Token: ${OPS_TOKEN}}
        timeout_s: 10.0
        sdd_dir: .sdd            # where the audit chain and bridge state live
        status_proof: true       # attach a chain-anchored proof (default true)
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
        self._status_proof = bool(config.get("status_proof", True))
        self._sdd_dir = Path(str(config.get("sdd_dir", ".sdd")))

    async def deliver(self, event: NotificationEvent) -> None:
        """POST the event payload, with its status proof when one can be minted."""
        await post_json(
            self._url,
            self._body(event),
            headers=self._headers or None,
            timeout=self._timeout,
        )

    def _body(self, event: NotificationEvent) -> dict[str, Any]:
        """Return the delivery body: the payload, plus a proof when available.

        Minting is best-effort by design. An install that cannot reach its audit
        chain still delivers the notification -- degrading to the pre-bridge
        body is strictly better than dropping an operator's alert -- but the
        failure is logged rather than swallowed silently.
        """
        payload = event.to_payload()
        if not self._status_proof:
            return payload

        from bernstein.core.trigger_sources.receipt import (
            AutomationBridgeError,
            bridge_root,
            emit_status_proof,
            wrap_status_payload,
        )

        try:
            from bernstein.core.security.audit import load_or_create_audit_key

            proof = emit_status_proof(
                root=bridge_root(self._sdd_dir / "automation-bridge"),
                audit_dir=self._sdd_dir / "audit",
                hmac_key=load_or_create_audit_key(),
                payload=payload,
                timestamp=int(event.timestamp),
            )
        except (AutomationBridgeError, OSError, RuntimeError, ValueError):
            logger.warning(
                "webhook sink %s: delivering event %s without a status proof",
                self.sink_id,
                event.event_id,
                exc_info=True,
            )
            return payload
        return wrap_status_payload(payload, proof)

    async def close(self) -> None:
        """No-op."""


def _resolve(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value
