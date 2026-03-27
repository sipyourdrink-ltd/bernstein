"""Generic webhook trigger source — normalizes arbitrary HTTP webhook payloads."""

from __future__ import annotations

import os
import time
from typing import Any

from bernstein.core.models import TriggerEvent


def normalize_webhook(
    path: str,
    method: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> TriggerEvent:
    """Normalize a generic webhook request into a TriggerEvent.

    Args:
        path: Request path (e.g. "/webhooks/trigger/deploy").
        method: HTTP method (e.g. "POST").
        headers: Request headers.
        payload: Parsed JSON body.

    Returns:
        Normalized TriggerEvent.
    """
    return TriggerEvent(
        source="webhook",
        timestamp=time.time(),
        raw_payload=payload,
        message=str(payload.get("message", ""))[:200] or None,
        metadata={
            "request_path": path,
            "request_method": method,
            "request_headers": dict(headers),
            "environment": payload.get("environment", ""),
        },
    )


def interpolate_env_vars(value: str) -> str:
    """Interpolate ``{ENV_VAR}`` placeholders in header values with env vars.

    Args:
        value: Header value potentially containing ``{VAR_NAME}`` placeholders.

    Returns:
        Value with env vars substituted.
    """
    if "{" not in value:
        return value
    import re

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return re.sub(r"\{(\w+)\}", _replace, value)
