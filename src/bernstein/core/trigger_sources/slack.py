"""Slack trigger source - normalize Slack Events API payloads into TriggerEvents."""

from __future__ import annotations

import math
import time
from typing import Any

from bernstein.core.tasks.models import TriggerEvent
from bernstein.core.webhook_signatures import verify_hmac_sha256


def _parse_slack_timestamp(raw_ts: Any) -> float:
    """Parse a Slack ``ts`` string to a finite epoch float, falling back to ``time.time()``."""
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        val = float(raw_ts)
        return val if math.isfinite(val) else time.time()
    if isinstance(raw_ts, str) and raw_ts:
        try:
            val = float(raw_ts)
        except ValueError:
            return time.time()
        return val if math.isfinite(val) else time.time()
    return time.time()


def verify_slack_signature(body: bytes, timestamp: str, signature: str, signing_secret: str) -> bool:
    """Verify a Slack request signature using HMAC-SHA256.

    Args:
        body: Raw request body bytes.
        timestamp: Value of X-Slack-Request-Timestamp header.
        signature: Value of X-Slack-Signature header (v0=...).
        signing_secret: Slack app signing secret.

    Returns:
        True if signature is valid.
    """
    # Reject requests older than 5 minutes (replay attack prevention)
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode()
    return verify_hmac_sha256(sig_basestring, signature, signing_secret, prefix="v0=")


def normalize_slack_message(payload: dict[str, Any]) -> TriggerEvent:
    """Normalize a Slack message event into a TriggerEvent.

    Args:
        payload: Parsed JSON from a Slack Events API callback.

    Returns:
        Normalized TriggerEvent.
    """
    raw_event = payload.get("event")
    event = raw_event if isinstance(raw_event, dict) else {}
    channel = event.get("channel") if isinstance(event.get("channel"), str) else ""
    user = event.get("user") if isinstance(event.get("user"), str) else ""
    text = event.get("text") if isinstance(event.get("text"), str) else ""
    raw_ts = event.get("ts")
    ts = raw_ts if isinstance(raw_ts, str) else ""
    team_id = payload.get("team_id") if isinstance(payload.get("team_id"), str) else ""
    event_id = payload.get("event_id") if isinstance(payload.get("event_id"), str) else ""

    return TriggerEvent(
        source="slack",
        timestamp=_parse_slack_timestamp(raw_ts),
        raw_payload=payload,
        sender=user,
        message=text,
        metadata={
            "channel": channel,
            "slack_ts": ts,
            "team_id": team_id,
            "event_id": event_id,
        },
    )
