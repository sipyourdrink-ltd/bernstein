"""Webhook parsing and constant-time token verification for GitLab.

GitLab does not sign webhook payloads.  Instead, every delivery carries
the shared secret in the ``X-Gitlab-Token`` header (plain string compare
on the receiving end).  We use :func:`hmac.compare_digest` to make the
comparison constant-time, mirroring how the GitHub side avoids timing
attacks on the HMAC digest.

Supported event types (``X-Gitlab-Event`` header):

* ``Merge Request Hook`` - push to MR, MR open/close, etc.
* ``Pipeline Hook`` - CI pipeline status change.
* ``Note Hook`` - comments on issues/MRs (where ``/bernstein`` lives).
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitLabWebhookEvent:
    """Parsed GitLab webhook event.

    Attributes:
        event_type: Value of the ``X-Gitlab-Event`` header - for example
            ``"Merge Request Hook"``.
        object_kind: ``payload["object_kind"]`` such as
            ``"merge_request"``, ``"pipeline"`` or ``"note"``.
        action: For MR / issue events this is
            ``payload["object_attributes"]["action"]`` (``"open"``,
            ``"close"``, etc.).  Empty for events with no explicit action.
        project_path: ``namespace/project`` path from the project block.
        sender: Username of the user that triggered the event.
        payload: Raw JSON payload.
    """

    event_type: str
    object_kind: str
    action: str
    project_path: str
    sender: str
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


def verify_token(provided_token: str, expected_token: str) -> bool:
    """Constant-time compare of the GitLab webhook token.

    Args:
        provided_token: Value of the ``X-Gitlab-Token`` header.
        expected_token: Configured shared secret.

    Returns:
        ``True`` when both strings are non-empty and equal under
        :func:`hmac.compare_digest`, otherwise ``False``.
    """
    if not provided_token or not expected_token:
        return False
    # Encode to bytes so non-ASCII tokens (rare but valid) don't trip
    # ``compare_digest``'s ASCII-only check.
    return hmac.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


def parse_webhook(headers: dict[str, str], body: bytes) -> GitLabWebhookEvent:
    """Parse GitLab webhook headers + JSON body into a :class:`GitLabWebhookEvent`.

    Args:
        headers: HTTP request headers (case-insensitive lookup is
            applied internally).
        body: Raw request body bytes.

    Returns:
        Parsed :class:`GitLabWebhookEvent`.

    Raises:
        ValueError: If a required header / payload field is missing or
            the body is not valid JSON.
    """
    lower_headers = {k.lower(): v for k, v in headers.items()}

    event_type = lower_headers.get("x-gitlab-event", "")
    if not event_type:
        msg = "Missing X-Gitlab-Event header"
        raise ValueError(msg)

    try:
        raw_payload: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"Invalid JSON payload: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(raw_payload, dict):
        msg = "GitLab webhook payload must be a JSON object"
        raise ValueError(msg)

    payload: dict[str, Any] = raw_payload  # type: ignore[assignment]
    object_kind = str(payload.get("object_kind", ""))

    # ``action`` lives under object_attributes for MR / issue events.
    raw_attrs: object = payload.get("object_attributes", {})
    attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}  # type: ignore[assignment]
    action = str(attrs.get("action", ""))

    raw_project: object = payload.get("project", {})
    project: dict[str, Any] = raw_project if isinstance(raw_project, dict) else {}  # type: ignore[assignment]
    project_path = str(project.get("path_with_namespace", "") or project.get("name", ""))
    if not project_path:
        msg = "Missing project.path_with_namespace in GitLab payload"
        raise ValueError(msg)

    raw_user: object = payload.get("user", {})
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}  # type: ignore[assignment]
    sender = str(user.get("username", "") or user.get("name", "") or "unknown")

    from bernstein.core.log_safe import for_log
    from bernstein.core.security.sanitize import sanitize_log

    logger.info(
        "Parsed GitLab webhook: event=%s kind=%s action=%s project=%s sender=%s",
        sanitize_log(event_type),
        for_log(object_kind),
        for_log(action),
        for_log(project_path),
        for_log(sender),
    )

    return GitLabWebhookEvent(
        event_type=event_type,
        object_kind=object_kind,
        action=action,
        project_path=project_path,
        sender=sender,
        payload=payload,
    )
