"""Tests for :mod:`bernstein.core.review_responder.webhook`."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bernstein.core.review_responder.webhook import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TARGET_EVENT,
    WebhookListener,
    verify_signature,
)


def _sign(secret: bytes, body: bytes) -> str:
    """Compute the GitHub-style ``sha256=<hex>`` signature header value."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_verify_signature_match() -> None:
    """A matching HMAC returns True."""
    secret = b"shh"
    body = b'{"hello":"world"}'
    sig = _sign(secret, body)
    assert verify_signature(secret=secret, body=body, signature=sig) is True


def test_verify_signature_mismatch() -> None:
    """A mismatched HMAC returns False."""
    assert (
        verify_signature(
            secret=b"shh",
            body=b'{"x":1}',
            signature="sha256=" + "0" * 64,
        )
        is False
    )


def test_verify_signature_empty_secret_rejected() -> None:
    """Empty secret rejects every signature — no silent allow-all."""
    assert verify_signature(secret=b"", body=b"x", signature="sha256=anything") is False


def test_verify_signature_missing_prefix_rejected() -> None:
    """Signature without the ``sha256=`` prefix is rejected."""
    assert verify_signature(secret=b"k", body=b"x", signature="abcd") is False


def test_webhook_listener_requires_secret() -> None:
    """Constructing without a secret raises ValueError."""
    with pytest.raises(ValueError):
        WebhookListener(secret=b"", on_comment=lambda _p: None)


def test_webhook_listener_rejects_bad_signature() -> None:
    """A request with a mismatched signature returns 401 and never invokes the callback."""
    seen: list[Any] = []

    def _on(payload: Any) -> None:
        seen.append(payload)

    listener = WebhookListener(secret=b"k", on_comment=_on)
    client = TestClient(listener.app)
    body = b'{"a":1}'
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: "sha256=" + "0" * 64,
            EVENT_HEADER: TARGET_EVENT,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert seen == []


def test_webhook_listener_accepts_signed_event() -> None:
    """A correctly signed target event is queued for the bundler."""
    seen: list[Any] = []
    secret = b"k"
    body = json.dumps({"comment": {"id": 1}}).encode()

    listener = WebhookListener(secret=secret, on_comment=lambda p: seen.append(p))
    client = TestClient(listener.app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: _sign(secret, body),
            EVENT_HEADER: TARGET_EVENT,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    assert seen and isinstance(seen[0], dict)


def test_webhook_listener_ignores_non_target_event() -> None:
    """Other event types are ignored with HTTP 202 (no callback)."""
    seen: list[Any] = []
    secret = b"k"
    body = b"{}"
    listener = WebhookListener(secret=secret, on_comment=lambda p: seen.append(p))
    client = TestClient(listener.app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: _sign(secret, body),
            EVENT_HEADER: "issues",
        },
    )
    assert resp.status_code == 202
    assert seen == []
