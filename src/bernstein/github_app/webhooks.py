"""Webhook parsing and HMAC-SHA256 signature verification.

GitHub sends webhooks with an ``X-Hub-Signature-256`` header containing
an HMAC-SHA256 digest of the request body.  This module verifies that
signature and parses the JSON payload into a typed ``WebhookEvent``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.webhook_signatures import verify_hmac_sha256

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookEvent:
    """Parsed GitHub webhook event."""

    event_type: str
    """GitHub event name: ``issues``, ``pull_request``, ``push``, ``issue_comment``."""

    action: str
    """Event action: ``opened``, ``closed``, ``synchronize``, etc.  Empty for push events."""

    repo_full_name: str
    """Full repository name in ``owner/repo`` format."""

    sender: str
    """GitHub username that triggered the event."""

    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    """Raw JSON payload from GitHub."""


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify the ``X-Hub-Signature-256`` HMAC-SHA256 digest.

    Args:
        body: Raw request body bytes.
        signature: Value of the ``X-Hub-Signature-256`` header
            (e.g. ``sha256=abc123...``).
        secret: The webhook secret configured in the GitHub App.

    Returns:
        ``True`` if the signature is valid, ``False`` otherwise.
    """
    return verify_hmac_sha256(body, signature, secret, prefix="sha256=")


def parse_webhook(headers: dict[str, str], body: bytes) -> WebhookEvent:
    """Parse GitHub webhook headers and JSON body into a ``WebhookEvent``.

    Args:
        headers: HTTP request headers (case-insensitive lookup expected).
        body: Raw request body bytes containing JSON.

    Returns:
        Parsed ``WebhookEvent``.

    Raises:
        ValueError: If required headers or payload fields are missing.
    """
    # Normalise header keys to lowercase for case-insensitive lookup
    lower_headers = {k.lower(): v for k, v in headers.items()}
    event_type = lower_headers.get("x-github-event", "")
    if not event_type:
        msg = "Missing X-GitHub-Event header"
        raise ValueError(msg)

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"Invalid JSON payload: {exc}"
        raise ValueError(msg) from exc

    action = payload.get("action", "")

    # Extract repo full name — different location for push vs other events
    repo: dict[str, Any] = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "")
    if not repo_full_name:
        msg = "Missing repository.full_name in payload"
        raise ValueError(msg)

    # Extract sender
    sender_obj: dict[str, Any] = payload.get("sender", {})
    sender = sender_obj.get("login", "unknown")

    from bernstein.core.sanitize import sanitize_log

    logger.info(
        "Parsed webhook: event=%s action=%s repo=%s sender=%s",
        sanitize_log(event_type),
        sanitize_log(action),
        sanitize_log(repo_full_name),
        sanitize_log(sender),
    )

    return WebhookEvent(
        event_type=event_type,
        action=action,
        repo_full_name=repo_full_name,
        sender=sender,
        payload=payload,
    )
