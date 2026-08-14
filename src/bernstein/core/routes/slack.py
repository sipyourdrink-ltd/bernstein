"""Slack webhook routes - slash command and Events API endpoints."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.routes._unconfigured import UNCONFIGURED_STATUS
from bernstein.core.tenanting import request_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_slack_request(request: Request, body: bytes, verify_fn: Any) -> JSONResponse | None:
    """Return an error response unless the request is a verified Slack delivery.

    A signing secret must be configured.  When it is not, the endpoint is
    disabled and answers ``UNCONFIGURED_STATUS``: only signed Slack
    deliveries are accepted, and without the secret no delivery can be
    shown to be one.  This matches the GitHub webhook handler in
    ``routes/webhooks.py``.

    Returns ``None`` when the request carries a valid signature, a 401
    response when the signature is missing or wrong, and an
    ``UNCONFIGURED_STATUS`` response when no signing secret is set.
    """
    signing_secret: str = getattr(request.app.state, "slack_signing_secret", None) or os.environ.get(
        "SLACK_SIGNING_SECRET", ""
    )
    if not signing_secret:
        from bernstein.core.sanitize import sanitize_log

        logger.error(
            "Rejecting POST %s: SLACK_SIGNING_SECRET is not configured. "
            "Set the env var (or pass slack_signing_secret= when building "
            "the app) to enable the endpoint; only signed Slack requests "
            "are accepted.",
            sanitize_log(request.url.path),
        )
        return JSONResponse(
            status_code=UNCONFIGURED_STATUS,
            content={
                "detail": (
                    "Slack webhook endpoint is not configured: set "
                    "SLACK_SIGNING_SECRET to the signing secret issued "
                    "by the Slack app."
                ),
            },
        )
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    if not timestamp or not signature or not verify_fn(body, timestamp, signature, signing_secret):
        return JSONResponse(status_code=401, content={"detail": "Invalid Slack signature"})
    return None


@router.post("/webhooks/slack/commands", status_code=200)
async def slack_slash_command(request: Request) -> JSONResponse:
    """Receive a Slack slash command, verify signature, and ack immediately.

    Slack requires a response within 3 seconds.  This endpoint verifies the
    request signature, parses the URL-encoded form payload, and returns an
    immediate acknowledgement.  Any long-running work (task creation, etc.)
    should be dispatched asynchronously using ``response_url``.

    Reads ``SLACK_SIGNING_SECRET`` from environment for HMAC verification.
    The secret MUST be configured: when it is not, the endpoint is
    disabled and returns ``UNCONFIGURED_STATUS``; only signed Slack
    requests are accepted.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse
    error, ``UNCONFIGURED_STATUS`` when the endpoint is not configured.

    Slash command form fields parsed:
        - ``command``      - the slash command (e.g. ``/bernstein``)
        - ``text``         - text following the command
        - ``user_id``      - Slack user ID
        - ``channel_id``   - Slack channel ID
        - ``response_url`` - URL for delayed responses (up to 30 min)
        - ``trigger_id``   - trigger ID for opening modals
    """
    from bernstein.core.trigger_sources.slack import verify_slack_signature

    body = await request.body()

    # Verify the Slack request signature - the signing secret MUST be configured.
    error_resp = _verify_slack_request(request, body, verify_slack_signature)
    if error_resp is not None:
        return error_resp

    # Parse URL-encoded form payload
    try:
        from urllib.parse import parse_qs

        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)

        def _first(key: str) -> str:
            values = parsed.get(key, [""])
            return values[0] if values else ""

        payload: dict[str, Any] = {
            "command": _first("command"),
            "text": _first("text"),
            "user_id": _first("user_id"),
            "channel_id": _first("channel_id"),
            "response_url": _first("response_url"),
            "trigger_id": _first("trigger_id"),
            "thread_ts": _first("thread_ts"),
        }
    # bot-ack: pre-existing-1723 (multipart form parse may raise diverse errors)
    except Exception:
        logger.debug("Bad slash command payload", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": "Bad slash command payload"},
        )

    from bernstein.core.sanitize import sanitize_log

    logger.info(
        "Slack slash command received: command=%r user=%r channel=%r text=%r",
        sanitize_log(payload["command"]),
        sanitize_log(payload["user_id"]),
        sanitize_log(payload["channel_id"]),
        sanitize_log(payload["text"]),
    )

    # Create a task from the slash command text
    text: str = payload["text"].strip()
    if text:
        from bernstein.core.server import TaskCreate, TaskStore

        store: TaskStore = request.app.state.store  # type: ignore[attr-defined]
        slack_context = {
            "channel_id": payload["channel_id"],
            "user_id": payload["user_id"],
            "thread_ts": payload["thread_ts"],
            "response_url": payload["response_url"],
        }
        task_create = TaskCreate(
            title=text[:60],
            description=text,
            role="backend",
            priority=1,
            scope="small",
            slack_context=slack_context,
            tenant_id=request_tenant_id(request),
        )
        task = await store.create(task_create)
        logger.info("Created task %s from Slack command: %r", task.id, sanitize_log(text[:60]))
        ack_text = f"Task `{task.id}` created: {text[:60]}"
    else:
        ack_text = f"Received `{payload['command']}` - no task text provided."

    # Acknowledge immediately - Slack requires response within 3 seconds
    return JSONResponse(
        status_code=200,
        content={
            "response_type": "ephemeral",
            "text": ack_text,
        },
    )


def _parse_slack_body(body: bytes) -> dict[str, Any] | None:
    """Parse a Slack events payload, returning None on failure.

    A payload that parses as JSON but is not an object (a list, a string, a
    number, a boolean, ``null``) is a parse failure too: every reader below
    treats the result as a mapping, so the shape is checked once here rather
    than assumed at each use.
    """
    try:
        import json as _json

        parsed = _json.loads(body)
    # bot-ack: pre-existing-1723 (best-effort Slack event parse)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _is_actionable_slack_event(event: dict[str, Any]) -> bool:
    """Return True if the Slack event is a user message we should act on."""
    if event.get("type") != "message":
        return False
    if event.get("subtype") in {"bot_message", "message_changed"}:
        return False
    return not event.get("bot_id")


@router.post("/webhooks/slack/events", status_code=200)
async def slack_events(request: Request) -> JSONResponse:
    """Receive Slack Events API callbacks.

    Handles:
    - ``url_verification``: returns the challenge value for endpoint verification.
    - ``event_callback`` with ``message`` type: creates a task when the bot is
      mentioned.  Bot messages and ``message_changed`` subtypes are ignored to
      prevent loops.

    Reads ``SLACK_SIGNING_SECRET`` from environment for HMAC verification.
    The secret MUST be configured: when it is not, the endpoint is
    disabled and returns ``UNCONFIGURED_STATUS``; only signed Slack
    requests are accepted.  Note that the ``url_verification`` handshake
    is signed by Slack too, so registering the endpoint works normally.
    Payload shape is validated before it is read: the body and, when
    present, its ``event`` member must both be JSON objects.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse
    error or malformed payload shape, ``UNCONFIGURED_STATUS`` when the
    endpoint is not configured.
    """
    from bernstein.core.trigger_sources.slack import normalize_slack_message, verify_slack_signature

    body = await request.body()

    error_resp = _verify_slack_request(request, body, verify_slack_signature)
    if error_resp is not None:
        return error_resp

    payload = _parse_slack_body(body)
    if payload is None:
        return JSONResponse(status_code=400, content={"detail": "Bad events payload"})

    event_type = payload.get("type", "")

    if event_type == "url_verification":
        return JSONResponse(status_code=200, content={"challenge": payload.get("challenge", "")})

    if event_type != "event_callback":
        return JSONResponse(status_code=200, content={"ok": True})

    # A present ``event`` must be an object.  Absent is fine - there is simply
    # nothing to act on - but a non-object member is a malformed payload and
    # takes the documented 400 rather than reaching the readers below, which
    # (like ``normalize_slack_message``) all assume a mapping.
    if "event" in payload and not isinstance(payload["event"], dict):
        return JSONResponse(status_code=400, content={"detail": "Bad events payload"})
    event: dict[str, Any] = payload.get("event", {})

    if not _is_actionable_slack_event(event):
        return JSONResponse(status_code=200, content={"ok": True})

    bot_user_id: str = os.environ.get("SLACK_BOT_USER_ID", "")
    text: str = event.get("text", "")

    if bot_user_id and f"<@{bot_user_id}>" not in text:
        return JSONResponse(status_code=200, content={"ok": True})

    trigger_event = normalize_slack_message(payload)

    # Strip mention from task text
    clean_text = text.replace(f"<@{bot_user_id}>", "").strip() if bot_user_id else text.strip()

    if clean_text:
        from bernstein.core.server import TaskCreate, TaskStore

        store: TaskStore = request.app.state.store  # type: ignore[attr-defined]
        slack_context = {
            "channel": event.get("channel", ""),
            "user": trigger_event.sender,
            "thread_ts": event.get("thread_ts") or event.get("ts", ""),
        }
        task_create = TaskCreate(
            title=clean_text[:60],
            description=clean_text,
            role="backend",
            priority=1,
            scope="small",
            slack_context=slack_context,
            tenant_id=request_tenant_id(request),
        )
        task = await store.create(task_create)
        from bernstein.core.sanitize import sanitize_log as _sl

        logger.info(
            "Created task %s from Slack message event: channel=%r user=%r text=%r",
            task.id,
            _sl(slack_context["channel"]),
            _sl(slack_context["user"]),
            _sl(clean_text[:60]),
        )

    return JSONResponse(status_code=200, content={"ok": True})
