"""Slack trigger source — normalize Slack Events API payloads into TriggerEvents."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from bernstein.core.models import TriggerEvent


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

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(computed, signature)


def normalize_slack_message(payload: dict[str, Any]) -> TriggerEvent:
    """Normalize a Slack message event into a TriggerEvent.

    Args:
        payload: Parsed JSON from a Slack Events API callback.

    Returns:
        Normalized TriggerEvent.
    """
    event = payload.get("event", {})
    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "")
    ts = event.get("ts", "")

    return TriggerEvent(
        source="slack",
        timestamp=float(ts) if ts else time.time(),
        raw_payload=payload,
        sender=user,
        message=text,
        metadata={
            "channel": channel,
            "slack_ts": ts,
            "team_id": payload.get("team_id", ""),
        },
    )
