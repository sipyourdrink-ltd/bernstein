"""OAuth 2.0 device-code flow helper used by ``bernstein connect linear --oauth``.

The device-code grant (RFC 8628) is the right shape for a CLI that can't
reliably bind a localhost callback (containers, SSH sessions, CI). The
flow:

1. POST to the device-code endpoint with ``client_id`` + scope.
2. Show the user the verification URL + user code printed by the IdP.
3. Poll the token endpoint with the device code until the user authorises
   or the polling deadline expires.

This module reuses :class:`bernstein.core.security.oauth_pkce.PKCETokens`
for its return type so callers downstream get the same shape regardless
of which OAuth flow they triggered.

OAuth cancellation is reported via :class:`OAuthDeviceCancelled` so the
CLI can print a friendly "you cancelled" message rather than a stack
trace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from bernstein.core.security.oauth_pkce import OAuthError, PKCETokens

logger = logging.getLogger(__name__)


class OAuthDeviceCancelled(OAuthError):
    """Raised when the user explicitly denies the device-code authorisation."""


class OAuthDeviceTimeout(OAuthError):
    """Raised when polling reaches the IdP-supplied expiry without success."""


@dataclass(frozen=True)
class DeviceCodeChallenge:
    """The ``(verification_url, user_code, device_code, interval, expires_at)`` bundle."""

    verification_url: str
    user_code: str
    device_code: str
    interval_s: float
    expires_at: float


def begin_device_code(
    *,
    device_endpoint: str,
    client_id: str,
    scope: str = "",
    client: httpx.Client | None = None,
) -> DeviceCodeChallenge:
    """POST to the device-code endpoint and return the challenge."""
    payload = {"client_id": client_id, "scope": scope}
    owns = client is None
    cli = client if client is not None else httpx.Client(timeout=10.0)
    try:
        resp = cli.post(device_endpoint, data=payload, timeout=10.0)
    finally:
        if owns:
            cli.close()
    if resp.status_code >= 400:
        raise OAuthError(
            f"Device-code request failed (HTTP {resp.status_code}): {resp.text[:200]}",
        )
    body: dict[str, Any] = resp.json()
    try:
        verification = str(body["verification_uri_complete"] or body["verification_uri"])
        user_code = str(body["user_code"])
        device_code = str(body["device_code"])
        interval = float(body.get("interval", 5))
        expires_in = float(body.get("expires_in", 600))
    except KeyError as exc:
        raise OAuthError(f"Device-code response missing field: {exc}") from exc
    return DeviceCodeChallenge(
        verification_url=verification,
        user_code=user_code,
        device_code=device_code,
        interval_s=interval,
        expires_at=time.monotonic() + expires_in,
    )


def poll_device_code(
    challenge: DeviceCodeChallenge,
    *,
    token_endpoint: str,
    client_id: str,
    client: httpx.Client | None = None,
    sleep_fn: Any = time.sleep,
) -> PKCETokens:
    """Poll the token endpoint until authorisation succeeds, fails, or times out.

    Args:
        challenge: The device-code challenge returned by :func:`begin_device_code`.
        token_endpoint: IdP token URL (``grant_type=urn:ietf:params:oauth:grant-type:device_code``).
        client_id: OAuth client id.
        client: Optional injected :class:`httpx.Client`; tests patch this.
        sleep_fn: Patchable sleep so tests run instantly.

    Raises:
        OAuthDeviceCancelled: Provider returned ``access_denied``.
        OAuthDeviceTimeout: Provider returned ``expired_token`` or our
            local deadline elapsed.
        OAuthError: Any other unrecoverable failure.
    """
    owns = client is None
    cli = client if client is not None else httpx.Client(timeout=10.0)
    interval = challenge.interval_s
    try:
        while True:
            if time.monotonic() >= challenge.expires_at:
                raise OAuthDeviceTimeout("Device-code authorisation expired before approval.")

            payload = {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": challenge.device_code,
                "client_id": client_id,
            }
            try:
                resp = cli.post(token_endpoint, data=payload, timeout=10.0)
            except httpx.HTTPError as exc:
                raise OAuthError(f"Device-code polling network error: {exc}") from exc

            body: dict[str, Any]
            try:
                body = resp.json()
            except ValueError as exc:
                raise OAuthError(f"Device-code response not JSON: {resp.text[:200]}") from exc

            if resp.status_code == 200 and "access_token" in body:
                return PKCETokens.from_response(body)

            error = str(body.get("error", "unknown_error"))
            if error == "authorization_pending":
                sleep_fn(interval)
                continue
            if error == "slow_down":
                interval = min(interval * 2.0, 30.0)
                sleep_fn(interval)
                continue
            if error == "access_denied":
                raise OAuthDeviceCancelled("User denied the device-code authorisation.")
            if error == "expired_token":
                raise OAuthDeviceTimeout("Device-code expired; restart with `bernstein connect`.")
            raise OAuthError(f"Device-code grant failed: {error}")
    finally:
        if owns:
            cli.close()
