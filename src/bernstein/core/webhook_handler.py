"""HTTP POST webhook handler for Bernstein hook events.

Sends hook event payloads to configured external HTTP endpoints with:
- Retry (3 attempts, exponential backoff)
- Configurable timeout (default 10s)
- Authentication: Bearer token or HMAC-SHA256 signature
- Configuration via ``bernstein.yaml`` hooks section

Example ``bernstein.yaml``::

    hooks:
      webhooks:
        - url: https://example.com/hooks
          events: ["task.completed", "task.failed"]
          auth:
            type: bearer
            token: "${WEBHOOK_TOKEN}"
        - url: https://ci.internal/bernstein
          events: ["merge.completed"]
          auth:
            type: hmac
            secret: "${HMAC_SECRET}"
          timeout_s: 15
          max_retries: 5
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from bernstein.core.hook_events import HookPayload  # noqa: TC001 — used at runtime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_S: float = 10.0
DEFAULT_MAX_RETRIES: int = 3
BACKOFF_BASE_S: float = 1.0  # 1s, 2s, 4s


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookAuth:
    """Authentication configuration for an outbound webhook.

    Attributes:
        type: ``"bearer"`` for Authorization header, ``"hmac"`` for HMAC-SHA256.
        token: Bearer token value (for ``type="bearer"``).
        secret: HMAC shared secret (for ``type="hmac"``).
    """

    type: str = "none"
    token: str = ""
    secret: str = ""

    def resolve_token(self) -> str:
        """Resolve the token, expanding ``${ENV_VAR}`` references.

        Returns:
            The resolved token string.
        """
        return _resolve_env_var(self.token)

    def resolve_secret(self) -> str:
        """Resolve the HMAC secret, expanding ``${ENV_VAR}`` references.

        Returns:
            The resolved secret string.
        """
        return _resolve_env_var(self.secret)


@dataclass(frozen=True)
class WebhookTarget:
    """A single webhook destination.

    Attributes:
        url: HTTP(S) endpoint to POST to.
        events: List of event names this target subscribes to (empty = all).
        auth: Authentication configuration.
        timeout_s: Per-request timeout in seconds.
        max_retries: Maximum delivery attempts (1 = no retry).
    """

    url: str
    events: list[str] = field(default_factory=list[str])
    auth: WebhookAuth = field(default_factory=WebhookAuth)
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_env_var(value: str) -> str:
    """Expand ``${VAR}`` references in a string from environment variables.

    If the variable is not set, the placeholder is replaced with an empty
    string.

    Args:
        value: String potentially containing ``${VAR}`` placeholders.

    Returns:
        The expanded string.
    """
    if not value.startswith("${") or not value.endswith("}"):
        return value
    var_name = value[2:-1]
    return os.environ.get(var_name, "")


def compute_hmac_signature(secret: str, payload: bytes) -> str:
    """Compute an HMAC-SHA256 signature for a webhook payload.

    Args:
        secret: Shared secret string.
        payload: Raw request body bytes.

    Returns:
        Hex digest prefixed with ``sha256=``.
    """
    digest = hmac_mod.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def build_headers(auth: WebhookAuth, body_bytes: bytes) -> dict[str, str]:
    """Build HTTP headers for the webhook request.

    Args:
        auth: Authentication configuration.
        body_bytes: Serialised request body for HMAC signing.

    Returns:
        Dict of HTTP headers to include.
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "Bernstein-Webhook/1.0",
    }

    if auth.type == "bearer":
        resolved = auth.resolve_token()
        if resolved:
            headers["Authorization"] = f"Bearer {resolved}"

    elif auth.type == "hmac":
        resolved_secret = auth.resolve_secret()
        if resolved_secret:
            sig = compute_hmac_signature(resolved_secret, body_bytes)
            headers["X-Bernstein-Signature"] = sig

    return headers


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass
class DeliveryResult:
    """Outcome of a webhook delivery attempt.

    Attributes:
        success: Whether the delivery succeeded (2xx response).
        status_code: HTTP status code of the last attempt (0 if connection failed).
        attempts: Number of attempts made.
        error: Error message if delivery failed.
        elapsed_s: Total wall-clock seconds across all attempts.
    """

    success: bool
    status_code: int = 0
    attempts: int = 0
    error: str = ""
    elapsed_s: float = 0.0


def deliver_webhook(
    target: WebhookTarget,
    payload: HookPayload,
    *,
    client: httpx.Client | None = None,
) -> DeliveryResult:
    """Deliver a hook payload to a webhook target with retry.

    Uses exponential backoff: 1s, 2s, 4s between retries.

    Args:
        target: Webhook destination configuration.
        payload: The hook event payload to send.
        client: Optional pre-configured httpx client for testing.

    Returns:
        A ``DeliveryResult`` describing the outcome.
    """
    body = payload.to_dict()
    own_client = client is None
    if own_client:
        client = httpx.Client()

    try:
        return _deliver_with_retry(target, body, client)
    finally:
        if own_client:
            client.close()


def _deliver_with_retry(
    target: WebhookTarget,
    body: dict[str, Any],
    client: httpx.Client,
) -> DeliveryResult:
    """Internal retry loop for webhook delivery.

    Args:
        target: Webhook destination.
        body: Serialised payload dict.
        client: httpx client to use.

    Returns:
        Delivery result.
    """
    import json

    body_bytes = json.dumps(body).encode("utf-8")
    headers = build_headers(target.auth, body_bytes)

    start = time.monotonic()
    last_error = ""
    last_status = 0

    for attempt in range(1, target.max_retries + 1):
        try:
            resp = client.post(
                target.url,
                content=body_bytes,
                headers=headers,
                timeout=target.timeout_s,
            )
            last_status = resp.status_code

            if 200 <= resp.status_code < 300:
                elapsed = time.monotonic() - start
                logger.debug(
                    "Webhook delivered to %s on attempt %d (status=%d, %.3fs)",
                    target.url,
                    attempt,
                    resp.status_code,
                    elapsed,
                )
                return DeliveryResult(
                    success=True,
                    status_code=resp.status_code,
                    attempts=attempt,
                    elapsed_s=elapsed,
                )

            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.debug(
                "Webhook attempt %d/%d to %s failed: %s",
                attempt,
                target.max_retries,
                target.url,
                last_error,
            )

        except httpx.TimeoutException:
            last_error = f"Timeout after {target.timeout_s}s"
            logger.debug(
                "Webhook attempt %d/%d to %s timed out",
                attempt,
                target.max_retries,
                target.url,
            )
        except httpx.HTTPError as exc:
            last_error = str(exc)
            logger.debug(
                "Webhook attempt %d/%d to %s error: %s",
                attempt,
                target.max_retries,
                target.url,
                last_error,
            )

        # Backoff before retry (skip after last attempt).
        if attempt < target.max_retries:
            backoff = BACKOFF_BASE_S * (2 ** (attempt - 1))
            time.sleep(backoff)

    elapsed = time.monotonic() - start
    logger.warning(
        "Webhook delivery to %s failed after %d attempts (%.1fs): %s",
        target.url,
        target.max_retries,
        elapsed,
        last_error,
    )
    return DeliveryResult(
        success=False,
        status_code=last_status,
        attempts=target.max_retries,
        error=last_error,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Dispatches hook events to matching webhook targets.

    Typically instantiated once from the ``hooks.webhooks`` section of
    ``bernstein.yaml`` and kept alive for the orchestrator's lifetime.

    Args:
        targets: List of webhook destinations.
        client: Optional shared httpx client.
    """

    def __init__(
        self,
        targets: list[WebhookTarget],
        client: httpx.Client | None = None,
    ) -> None:
        self._targets = targets
        self._client = client or httpx.Client()
        self._owns_client = client is None

    @property
    def targets(self) -> list[WebhookTarget]:
        """Return the configured webhook targets."""
        return list(self._targets)

    def dispatch(self, payload: HookPayload) -> list[DeliveryResult]:
        """Send a payload to all matching webhook targets.

        A target matches if its events list is empty (subscribe-all) or
        if the payload's event name is in the list.

        Args:
            payload: The hook event payload to deliver.

        Returns:
            List of delivery results, one per matching target.
        """
        results: list[DeliveryResult] = []
        event_name = payload.event.value

        for target in self._targets:
            if target.events and event_name not in target.events:
                continue
            result = deliver_webhook(target, payload, client=self._client)
            results.append(result)

        return results

    def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if self._owns_client:
            self._client.close()


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------


def parse_webhook_config(raw: list[dict[str, Any]]) -> list[WebhookTarget]:
    """Parse the ``hooks.webhooks`` section from ``bernstein.yaml``.

    Args:
        raw: List of dicts from the YAML config.

    Returns:
        List of validated ``WebhookTarget`` instances.
    """
    targets: list[WebhookTarget] = []
    for entry in raw:
        url = str(entry.get("url", ""))
        if not url:
            logger.warning("Webhook config entry missing 'url', skipping: %s", entry)
            continue

        events_raw: object = entry.get("events", [])
        events: list[str] = [str(e) for e in cast("list[object]", events_raw)] if isinstance(events_raw, list) else []

        auth_raw: object = entry.get("auth", {})
        if isinstance(auth_raw, dict):
            auth_dict = cast("dict[str, Any]", auth_raw)
            auth = WebhookAuth(
                type=str(auth_dict.get("type", "none")),
                token=str(auth_dict.get("token", "")),
                secret=str(auth_dict.get("secret", "")),
            )
        else:
            auth = WebhookAuth()

        timeout_s = float(entry.get("timeout_s", DEFAULT_TIMEOUT_S))
        max_retries = int(entry.get("max_retries", DEFAULT_MAX_RETRIES))

        targets.append(
            WebhookTarget(
                url=url,
                events=events,
                auth=auth,
                timeout_s=timeout_s,
                max_retries=max_retries,
            )
        )

    return targets
