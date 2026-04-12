"""Tests for HOOK-003 — HTTP webhook handler (webhook_handler.py)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from bernstein.core.hook_events import HookEvent, TaskPayload
from bernstein.core.webhook_handler import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    WebhookAuth,
    WebhookDispatcher,
    WebhookTarget,
    build_headers,
    compute_hmac_signature,
    deliver_webhook,
    parse_webhook_config,
)

# ---------------------------------------------------------------------------
# WebhookAuth
# ---------------------------------------------------------------------------


class TestWebhookAuth:
    """WebhookAuth resolves tokens and secrets."""

    def test_defaults(self) -> None:
        auth = WebhookAuth()
        assert auth.type == "none"
        assert auth.token == ""
        assert auth.secret == ""

    def test_resolve_token_literal(self) -> None:
        auth = WebhookAuth(type="bearer", token="my-token")
        assert auth.resolve_token() == "my-token"

    def test_resolve_token_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_WH_TOKEN", "secret-from-env")
        auth = WebhookAuth(type="bearer", token="${TEST_WH_TOKEN}")
        assert auth.resolve_token() == "secret-from-env"

    def test_resolve_token_missing_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        auth = WebhookAuth(type="bearer", token="${MISSING_VAR}")
        assert auth.resolve_token() == ""

    def test_resolve_secret_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HMAC_SEC", "hmac-key")
        auth = WebhookAuth(type="hmac", secret="${HMAC_SEC}")
        assert auth.resolve_secret() == "hmac-key"


# ---------------------------------------------------------------------------
# WebhookTarget defaults
# ---------------------------------------------------------------------------


class TestWebhookTarget:
    """WebhookTarget has correct defaults."""

    def test_defaults(self) -> None:
        t = WebhookTarget(url="https://example.com")
        assert t.timeout_s == DEFAULT_TIMEOUT_S
        assert t.max_retries == DEFAULT_MAX_RETRIES
        assert t.events == []

    def test_custom_values(self) -> None:
        t = WebhookTarget(
            url="https://ci.example.com",
            events=["task.completed"],
            timeout_s=15.0,
            max_retries=5,
        )
        assert t.events == ["task.completed"]
        assert t.timeout_s == pytest.approx(15.0)
        assert t.max_retries == 5


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------


class TestHmacSignature:
    """compute_hmac_signature produces valid HMAC-SHA256."""

    def test_basic_signature(self) -> None:
        body = b'{"event":"task.completed"}'
        sig = compute_hmac_signature("my-secret", body)
        assert sig.startswith("sha256=")
        # Verify independently.
        expected = hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
        assert sig == f"sha256={expected}"

    def test_empty_body(self) -> None:
        sig = compute_hmac_signature("key", b"")
        assert sig.startswith("sha256=")
        assert len(sig) == len("sha256=") + 64


# ---------------------------------------------------------------------------
# build_headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    """build_headers produces correct auth headers."""

    def test_no_auth(self) -> None:
        auth = WebhookAuth(type="none")
        headers = build_headers(auth, b"body")
        assert "Authorization" not in headers
        assert "X-Bernstein-Signature" not in headers
        assert headers["Content-Type"] == "application/json"

    def test_bearer_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth = WebhookAuth(type="bearer", token="tok-123")
        headers = build_headers(auth, b"body")
        assert headers["Authorization"] == "Bearer tok-123"

    def test_hmac_auth(self) -> None:
        auth = WebhookAuth(type="hmac", secret="s3cret")
        body = b'{"event":"test"}'
        headers = build_headers(auth, body)
        assert "X-Bernstein-Signature" in headers
        sig = headers["X-Bernstein-Signature"]
        assert sig.startswith("sha256=")

    def test_user_agent(self) -> None:
        auth = WebhookAuth()
        headers = build_headers(auth, b"")
        assert "Bernstein" in headers["User-Agent"]


# ---------------------------------------------------------------------------
# Delivery with mock transport
# ---------------------------------------------------------------------------


def _make_payload() -> TaskPayload:
    """Create a sample TaskPayload for testing."""
    return TaskPayload(
        event=HookEvent.TASK_COMPLETED,
        task_id="t1",
        role="backend",
        title="Fix tests",
    )


class TestDeliverWebhook:
    """deliver_webhook sends payload and handles responses."""

    def test_success_on_200(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True}),
        )
        client = httpx.Client(transport=transport)
        target = WebhookTarget(url="https://example.com/hook", max_retries=1)
        result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is True
        assert result.status_code == 200
        assert result.attempts == 1
        client.close()

    def test_success_on_201(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(201),
        )
        client = httpx.Client(transport=transport)
        target = WebhookTarget(url="https://example.com/hook", max_retries=1)
        result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is True
        assert result.status_code == 201
        client.close()

    def test_failure_on_500_exhausts_retries(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(
            url="https://example.com/hook",
            max_retries=2,
        )
        # Use fast backoff by patching time.sleep.
        with patch("bernstein.core.server.webhook_handler.time.sleep"):
            result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is False
        assert result.status_code == 500
        assert result.attempts == 2
        assert call_count == 2
        client.close()

    def test_retry_succeeds_on_second_attempt(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, text="Unavailable")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(url="https://example.com/hook", max_retries=3)
        with patch("bernstein.core.server.webhook_handler.time.sleep"):
            result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is True
        assert result.attempts == 2
        client.close()

    def test_timeout_results_in_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(
            url="https://example.com/hook",
            timeout_s=1.0,
            max_retries=1,
        )
        result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is False
        assert "Timeout" in result.error
        client.close()

    def test_connection_error_results_in_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(
            url="https://example.com/hook",
            max_retries=1,
        )
        result = deliver_webhook(target, _make_payload(), client=client)
        assert result.success is False
        assert result.error != ""
        client.close()

    def test_bearer_header_sent(self) -> None:
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(
            url="https://example.com/hook",
            auth=WebhookAuth(type="bearer", token="tok-abc"),
            max_retries=1,
        )
        deliver_webhook(target, _make_payload(), client=client)
        assert received_headers.get("authorization") == "Bearer tok-abc"
        client.close()

    def test_hmac_header_sent(self) -> None:
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(
            url="https://example.com/hook",
            auth=WebhookAuth(type="hmac", secret="my-hmac-secret"),
            max_retries=1,
        )
        deliver_webhook(target, _make_payload(), client=client)
        sig = received_headers.get("x-bernstein-signature", "")
        assert sig.startswith("sha256=")
        client.close()

    def test_payload_body_matches_to_dict(self) -> None:
        received_body: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_body.append(request.content)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        target = WebhookTarget(url="https://example.com/hook", max_retries=1)
        payload = _make_payload()
        deliver_webhook(target, payload, client=client)
        assert len(received_body) == 1
        body_dict = json.loads(received_body[0])
        assert body_dict["event"] == "task.completed"
        assert body_dict["task_id"] == "t1"
        client.close()


# ---------------------------------------------------------------------------
# WebhookDispatcher
# ---------------------------------------------------------------------------


class TestWebhookDispatcher:
    """WebhookDispatcher routes events to matching targets."""

    def _make_transport_200(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda r: httpx.Response(200))

    def test_dispatch_matches_events(self) -> None:
        client = httpx.Client(transport=self._make_transport_200())
        targets = [
            WebhookTarget(
                url="https://a.com/hook",
                events=["task.completed"],
                max_retries=1,
            ),
            WebhookTarget(
                url="https://b.com/hook",
                events=["task.failed"],
                max_retries=1,
            ),
        ]
        dispatcher = WebhookDispatcher(targets, client=client)
        results = dispatcher.dispatch(_make_payload())
        # Only the first target matches task.completed
        assert len(results) == 1
        assert results[0].success is True
        client.close()

    def test_dispatch_empty_events_matches_all(self) -> None:
        client = httpx.Client(transport=self._make_transport_200())
        targets = [
            WebhookTarget(url="https://all.com/hook", events=[], max_retries=1),
        ]
        dispatcher = WebhookDispatcher(targets, client=client)
        results = dispatcher.dispatch(_make_payload())
        assert len(results) == 1
        assert results[0].success is True
        client.close()

    def test_dispatch_no_match(self) -> None:
        client = httpx.Client(transport=self._make_transport_200())
        targets = [
            WebhookTarget(
                url="https://nope.com/hook",
                events=["agent.spawned"],
                max_retries=1,
            ),
        ]
        dispatcher = WebhookDispatcher(targets, client=client)
        results = dispatcher.dispatch(_make_payload())
        assert len(results) == 0
        client.close()

    def test_targets_property(self) -> None:
        targets = [WebhookTarget(url="https://a.com")]
        dispatcher = WebhookDispatcher(targets)
        assert dispatcher.targets == targets

    def test_close_idempotent(self) -> None:
        dispatcher = WebhookDispatcher([])
        dispatcher.close()
        dispatcher.close()  # Should not raise


# ---------------------------------------------------------------------------
# parse_webhook_config
# ---------------------------------------------------------------------------


class TestParseWebhookConfig:
    """parse_webhook_config parses YAML-like dicts."""

    def test_basic_config(self) -> None:
        raw: list[dict[str, Any]] = [
            {
                "url": "https://example.com/hooks",
                "events": ["task.completed", "task.failed"],
                "auth": {"type": "bearer", "token": "tok-123"},
            },
        ]
        targets = parse_webhook_config(raw)
        assert len(targets) == 1
        t = targets[0]
        assert t.url == "https://example.com/hooks"
        assert t.events == ["task.completed", "task.failed"]
        assert t.auth.type == "bearer"
        assert t.auth.token == "tok-123"

    def test_hmac_config(self) -> None:
        raw: list[dict[str, Any]] = [
            {
                "url": "https://ci.example.com",
                "auth": {"type": "hmac", "secret": "${HMAC_KEY}"},
            },
        ]
        targets = parse_webhook_config(raw)
        assert targets[0].auth.type == "hmac"
        assert targets[0].auth.secret == "${HMAC_KEY}"

    def test_custom_timeout_and_retries(self) -> None:
        raw: list[dict[str, Any]] = [
            {
                "url": "https://slow.example.com",
                "timeout_s": 30,
                "max_retries": 5,
            },
        ]
        targets = parse_webhook_config(raw)
        assert targets[0].timeout_s == pytest.approx(30.0)
        assert targets[0].max_retries == 5

    def test_missing_url_skipped(self) -> None:
        raw: list[dict[str, Any]] = [
            {"events": ["task.completed"]},
            {"url": "https://valid.com"},
        ]
        targets = parse_webhook_config(raw)
        assert len(targets) == 1
        assert targets[0].url == "https://valid.com"

    def test_empty_config(self) -> None:
        targets = parse_webhook_config([])
        assert targets == []

    def test_no_auth_defaults_to_none(self) -> None:
        raw: list[dict[str, Any]] = [{"url": "https://plain.com"}]
        targets = parse_webhook_config(raw)
        assert targets[0].auth.type == "none"

    def test_defaults_applied(self) -> None:
        raw: list[dict[str, Any]] = [{"url": "https://default.com"}]
        targets = parse_webhook_config(raw)
        assert targets[0].timeout_s == DEFAULT_TIMEOUT_S
        assert targets[0].max_retries == DEFAULT_MAX_RETRIES

    def test_multiple_targets(self) -> None:
        raw: list[dict[str, Any]] = [
            {"url": "https://a.com"},
            {"url": "https://b.com"},
            {"url": "https://c.com"},
        ]
        targets = parse_webhook_config(raw)
        assert len(targets) == 3
