"""FastAPI route for tracker webhook ingestion.

Exposes ``POST /webhooks/trackers/<adapter_name>``.  The route delegates
verification, parsing, and replay-protection to
:class:`bernstein.core.trackers.webhook_receiver.WebhookReceiver` so the
HTTP layer stays thin and the business logic stays unit-testable.

Per-adapter configuration is loaded from ``bernstein.yaml`` under
``trackers.<name>.webhook``:

```yaml
trackers:
  jira_cloud:
    webhook:
      enabled: true
      secret_env: JIRA_CLOUD_WEBHOOK_SECRET
      public_url_base: https://bernstein.example.com
```

Polling remains the default; the webhook route is opt-in per adapter via
``enabled: true`` and a configured ``secret_env`` that resolves to a
non-empty value at request time.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.routes._unconfigured import UNCONFIGURED_STATUS
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.trackers.webhook_receiver import (
    ReceiveResult,
    WebhookReceiver,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_receiver(request: Request) -> WebhookReceiver:
    """Return the per-app :class:`WebhookReceiver`, constructing on demand.

    The first call wires a default-configured receiver onto
    ``app.state``.  Subsequent calls reuse the same instance so the
    in-memory replay ledger stays warm across requests.
    """

    receiver = getattr(request.app.state, "tracker_webhook_receiver", None)
    if receiver is None:
        receiver = WebhookReceiver()
        request.app.state.tracker_webhook_receiver = receiver
    return receiver


def _status_to_response(result: ReceiveResult) -> JSONResponse:
    """Translate a :class:`ReceiveResult` into an HTTP response.

    The status mapping is chosen so trackers stop retrying on permanent
    rejections (4xx) and back off on transient ones (5xx).  ``replay``
    returns 200 so the tracker treats the delivery as accepted without
    creating a duplicate ticket downstream.
    """

    body = {"status": result.status, "delivery_id": result.delivery_id}
    if result.status == "accepted":
        return JSONResponse(status_code=200, content=body)
    if result.status in {"replay", "ignored"}:
        return JSONResponse(status_code=200, content=body)
    if result.status == "unknown_adapter":
        return JSONResponse(status_code=404, content=body)
    if result.status in {"disabled", "not_configured"}:
        # Permanent by the rule stated above: an adapter this deployment did
        # not configure will not start working because the tracker retried.
        return JSONResponse(status_code=UNCONFIGURED_STATUS, content=body)
    if result.status in {"bad_signature", "bad_payload"}:
        return JSONResponse(status_code=401 if result.status == "bad_signature" else 400, content=body)
    # Default to 500 for unexpected statuses so monitoring catches them.
    return JSONResponse(status_code=500, content=body)


@router.post("/webhooks/trackers/{adapter}", status_code=200)
async def tracker_webhook(adapter: str, request: Request) -> JSONResponse:
    """Receive a tracker webhook, verify, dedupe, and enqueue.

    Path parameter:
        adapter: Short adapter name registered via
            :func:`bernstein.core.trackers.webhook_receiver.register_handler`.

    The endpoint accepts any JSON object.  All verification and replay
    decisions are made before the body is enqueued.  When verification
    succeeds and the delivery is fresh the parsed
    :class:`~bernstein.core.trackers.webhook_receiver.TrackerEvent` is
    stashed on ``app.state.tracker_event_queue`` if present so the
    orchestrator's normal task ingestion can drain it; if no queue is
    wired we simply log the event.  Either way the tracker receives a
    200 so it does not retry.
    """

    receiver = _get_receiver(request)
    body = await request.body()
    headers = dict(request.headers.items())

    result = receiver.receive(adapter, headers, body)

    if result.status == "accepted" and result.event is not None:
        queue = getattr(request.app.state, "tracker_event_queue", None)
        if queue is not None:
            # bot-ack: pre-existing-1723 (queue boundary; do not raise into HTTP)
            try:
                queue.put_nowait(result.event)
            except Exception as exc:  # boundary
                logger.warning(
                    "Tracker webhook queue rejected event adapter=%s id=%s: %s",
                    sanitize_log(adapter),
                    sanitize_log(str(result.delivery_id)),
                    exc,
                )
        else:
            logger.info(
                "Tracker webhook accepted adapter=%s id=%s ticket=%s",
                sanitize_log(adapter),
                sanitize_log(str(result.delivery_id)),
                sanitize_log(str(result.event.ticket.id)),
            )

    return _status_to_response(result)
