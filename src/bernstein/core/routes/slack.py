"""Slack webhook routes — slash command and Events API endpoints."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/slack/commands", status_code=200)
async def slack_slash_command(request: Request) -> JSONResponse:
    """Receive a Slack slash command, verify signature, and ack immediately.

    Slack requires a response within 3 seconds.  This endpoint verifies the
    request signature, parses the URL-encoded form payload, and returns an
    immediate acknowledgement.  Any long-running work (task creation, etc.)
    should be dispatched asynchronously using ``response_url``.

    Reads ``SLACK_SIGNING_SECRET`` from environment for HMAC verification.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse error.

    Slash command form fields parsed:
        - ``command``      — the slash command (e.g. ``/bernstein``)
        - ``text``         — text following the command
        - ``user_id``      — Slack user ID
        - ``channel_id``   — Slack channel ID
        - ``response_url`` — URL for delayed responses (up to 30 min)
        - ``trigger_id``   — trigger ID for opening modals
    """
    from bernstein.core.trigger_sources.slack import verify_slack_signature

    body = await request.body()

    # Verify Slack request signature if a signing secret is configured
    signing_secret: str = getattr(request.app.state, "slack_signing_secret", None) or os.environ.get(
        "SLACK_SIGNING_SECRET", ""
    )
    if signing_secret:
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not timestamp or not signature or not verify_slack_signature(body, timestamp, signature, signing_secret):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid Slack signature"},
            )

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
        )
        task = await store.create(task_create)
        logger.info("Created task %s from Slack command: %r", task.id, sanitize_log(text[:60]))
        ack_text = f"Task `{task.id}` created: {text[:60]}"
    else:
        ack_text = f"Received `{payload['command']}` — no task text provided."

    # Acknowledge immediately — Slack requires response within 3 seconds
    return JSONResponse(
        status_code=200,
        content={
            "response_type": "ephemeral",
            "text": ack_text,
        },
    )


@router.post("/webhooks/slack/events", status_code=200)
async def slack_events(request: Request) -> JSONResponse:
    """Receive Slack Events API callbacks.

    Handles:
    - ``url_verification``: returns the challenge value for endpoint verification.
    - ``event_callback`` with ``message`` type: creates a task when the bot is
      mentioned.  Bot messages and ``message_changed`` subtypes are ignored to
      prevent loops.

    Reads ``SLACK_SIGNING_SECRET`` from environment for HMAC verification.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse error.
    """
    from bernstein.core.trigger_sources.slack import normalize_slack_message, verify_slack_signature

    body = await request.body()

    # Verify Slack request signature if a signing secret is configured
    signing_secret: str = getattr(request.app.state, "slack_signing_secret", None) or os.environ.get(
        "SLACK_SIGNING_SECRET", ""
    )
    if signing_secret:
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not timestamp or not signature or not verify_slack_signature(body, timestamp, signature, signing_secret):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid Slack signature"},
            )

    try:
        import json as _json

        payload: dict[str, Any] = _json.loads(body)
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Bad events payload: {exc}"},
        )

    event_type = payload.get("type", "")

    # Slack URL verification handshake
    if event_type == "url_verification":
        return JSONResponse(
            status_code=200,
            content={"challenge": payload.get("challenge", "")},
        )

    if event_type != "event_callback":
        return JSONResponse(status_code=200, content={"ok": True})

    event: dict[str, Any] = payload.get("event", {})
    msg_type = event.get("type", "")
    subtype = event.get("subtype", "")

    # Ignore non-message events, bot_message subtypes, and message_changed
    if msg_type != "message" or subtype in {"bot_message", "message_changed"}:
        return JSONResponse(status_code=200, content={"ok": True})

    # Ignore messages sent by bots (bot_id present means it's a bot)
    if event.get("bot_id"):
        return JSONResponse(status_code=200, content={"ok": True})

    bot_user_id: str = os.environ.get("SLACK_BOT_USER_ID", "")
    text: str = event.get("text", "")

    # Only act when the bot is directly mentioned
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
