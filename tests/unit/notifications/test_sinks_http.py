"""Tests for the Slack/Discord/Webhook drivers (mocked HTTP transport)."""

from __future__ import annotations

import json

import httpx
import pytest

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks.discord import DiscordSink
from bernstein.core.notifications.sinks.slack import SlackSink
from bernstein.core.notifications.sinks.webhook import WebhookSink


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="ev-http",
        kind=NotificationEventKind.POST_TASK,
        title="hello",
        body="world",
        severity="warning",
        timestamp=1.0,
    )


def _mock_transport(response: httpx.Response) -> httpx.MockTransport:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    transport = httpx.MockTransport(_handler)
    transport.captured = captured  # type: ignore[attr-defined]
    return transport


@pytest.mark.asyncio
async def test_slack_sink_posts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = SlackSink({"id": "slack-test", "kind": "slack", "webhook_url": "https://example/hook"})
    transport = _mock_transport(httpx.Response(200, text="ok"))

    async def _patched_post(url: str, payload: dict[str, object], **kwargs: object) -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(url, json=payload, headers={"content-type": "application/json"})
        if not response.is_success:
            raise NotificationDeliveryError("boom")
        return response.text

    monkeypatch.setattr("bernstein.core.notifications.sinks.slack.post_json", _patched_post)
    await sink.deliver(_event())
    assert transport.captured  # type: ignore[attr-defined]
    body = json.loads(transport.captured[0].content)  # type: ignore[attr-defined]
    assert "*hello*" in body["text"]
    assert "world" in body["text"]


@pytest.mark.asyncio
async def test_discord_sink_uses_severity_color(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = DiscordSink({"id": "d", "kind": "discord", "webhook_url": "https://example/hook"})
    transport = _mock_transport(httpx.Response(200))

    async def _patched_post(url: str, payload: dict[str, object], **_: object) -> str:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(url, json=payload)
        return response.text

    monkeypatch.setattr("bernstein.core.notifications.sinks.discord.post_json", _patched_post)
    await sink.deliver(_event())
    body = json.loads(transport.captured[0].content)  # type: ignore[attr-defined]
    assert body["embeds"][0]["color"] == 0xF1C40F  # warning


@pytest.mark.asyncio
async def test_webhook_sink_passes_headers_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = WebhookSink({"id": "wh", "kind": "webhook", "url": "https://h", "headers": {"X-T": "abc"}})
    transport = _mock_transport(httpx.Response(204))

    async def _patched_post(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
        **_: object,
    ) -> str:
        assert headers == {"X-T": "abc"}
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(url, json=payload, headers=headers or {})
        return ""

    monkeypatch.setattr("bernstein.core.notifications.sinks.webhook.post_json", _patched_post)
    await sink.deliver(_event())
    body = json.loads(transport.captured[0].content)  # type: ignore[attr-defined]
    assert body["event_id"] == "ev-http"
    assert body["kind"] == "post_task"


@pytest.mark.asyncio
async def test_http_helper_classifies_5xx_as_transient() -> None:
    from bernstein.core.notifications.sinks._http import post_json

    transport = _mock_transport(httpx.Response(503, text="busy"))
    with pytest.raises(NotificationDeliveryError, match="503"):
        await post_json("https://x", {"a": 1}, transport=transport)


@pytest.mark.asyncio
async def test_http_helper_classifies_4xx_as_permanent() -> None:
    from bernstein.core.notifications.sinks._http import post_json

    transport = _mock_transport(httpx.Response(404, text="nope"))
    with pytest.raises(NotificationPermanentError, match="404"):
        await post_json("https://x", {"a": 1}, transport=transport)


@pytest.mark.asyncio
async def test_http_helper_classifies_429_as_transient() -> None:
    from bernstein.core.notifications.sinks._http import post_json

    transport = _mock_transport(httpx.Response(429))
    with pytest.raises(NotificationDeliveryError, match="429"):
        await post_json("https://x", {"a": 1}, transport=transport)


def test_slack_requires_webhook_url() -> None:
    with pytest.raises(NotificationPermanentError, match="webhook_url"):
        SlackSink({"id": "x", "kind": "slack"})


def test_discord_requires_webhook_url() -> None:
    with pytest.raises(NotificationPermanentError, match="webhook_url"):
        DiscordSink({"id": "x", "kind": "discord"})


def test_webhook_requires_url() -> None:
    with pytest.raises(NotificationPermanentError, match="'url'"):
        WebhookSink({"id": "x", "kind": "webhook"})


def test_slack_resolves_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_HOOK", "https://hooks.slack.com/x")
    sink = SlackSink({"id": "s", "kind": "slack", "webhook_url": "${MY_HOOK}"})
    assert "hooks.slack.com" in sink._webhook_url  # type: ignore[attr-defined]
