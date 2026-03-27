"""Tests for the notifications module — formatters and event dispatch.

All formatters are tested against their output structure.
NotificationManager is tested by patching httpx.post.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from bernstein.core.notifications import (
    NotificationManager,
    NotificationPayload,
    NotificationTarget,
    format_discord,
    format_slack,
    format_telegram,
    format_webhook,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(
    event: str = "run.completed",
    title: str = "Run Complete",
    body: str = "All done!",
    **meta: Any,
) -> NotificationPayload:
    return NotificationPayload(event=event, title=title, body=body, metadata=dict(meta))


def _ok_response() -> MagicMock:
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    return m


# ---------------------------------------------------------------------------
# Slack formatter
# ---------------------------------------------------------------------------


class TestFormatSlack:
    def test_returns_blocks_key(self) -> None:
        result = format_slack(_payload(title="Run Complete", body="3 tasks done"))
        assert "blocks" in result

    def test_title_appears_in_output(self) -> None:
        result = format_slack(_payload(title="My Title"))
        assert "My Title" in str(result)

    def test_event_type_appears_in_output(self) -> None:
        result = format_slack(_payload(event="task.failed", title="Fail"))
        assert "task.failed" in str(result)

    def test_metadata_cost_appears_in_output(self) -> None:
        result = format_slack(_payload(event="run.completed", title="Done", cost_usd=1.23))
        assert "1.23" in str(result)

    def test_task_failed_has_red_attachment_color(self) -> None:
        result = format_slack(_payload(event="task.failed"))
        color_str = str(result).upper()
        assert "FF0000" in color_str

    def test_task_completed_has_green_attachment_color(self) -> None:
        result = format_slack(_payload(event="task.completed"))
        color_str = str(result).upper()
        assert "00FF00" in color_str


# ---------------------------------------------------------------------------
# Discord formatter
# ---------------------------------------------------------------------------


class TestFormatDiscord:
    def test_returns_embeds_key(self) -> None:
        result = format_discord(_payload(title="Done"))
        assert "embeds" in result
        assert len(result["embeds"]) >= 1

    def test_task_failed_has_red_color(self) -> None:
        result = format_discord(_payload(event="task.failed"))
        embed = result["embeds"][0]
        assert embed.get("color") == 0xFF0000

    def test_task_completed_has_green_color(self) -> None:
        result = format_discord(_payload(event="task.completed"))
        embed = result["embeds"][0]
        assert embed.get("color") == 0x00FF00

    def test_run_completed_has_info_color(self) -> None:
        result = format_discord(_payload(event="run.completed"))
        embed = result["embeds"][0]
        assert embed.get("color") == 0x0088FF

    def test_title_in_embed(self) -> None:
        result = format_discord(_payload(title="My Title"))
        embed = result["embeds"][0]
        assert embed.get("title") == "My Title"

    def test_body_in_embed_description(self) -> None:
        result = format_discord(_payload(body="some body text"))
        embed = result["embeds"][0]
        assert "some body text" in (embed.get("description") or "")


# ---------------------------------------------------------------------------
# Telegram formatter
# ---------------------------------------------------------------------------


class TestFormatTelegram:
    def test_returns_string(self) -> None:
        result = format_telegram(_payload(title="Done", body="3 tasks"))
        assert isinstance(result, str)

    def test_title_in_output(self) -> None:
        result = format_telegram(_payload(title="Run Complete"))
        assert "Run Complete" in result

    def test_body_in_output(self) -> None:
        result = format_telegram(_payload(body="3 done, 0 failed"))
        assert "3 done, 0 failed" in result

    def test_event_in_output(self) -> None:
        result = format_telegram(_payload(event="budget.warning"))
        assert "budget.warning" in result


# ---------------------------------------------------------------------------
# Generic webhook formatter
# ---------------------------------------------------------------------------


class TestFormatWebhook:
    def test_event_in_result(self) -> None:
        result = format_webhook(_payload(event="run.completed"))
        assert result["event"] == "run.completed"

    def test_title_in_result(self) -> None:
        result = format_webhook(_payload(title="My Title"))
        assert result["title"] == "My Title"

    def test_body_in_result(self) -> None:
        result = format_webhook(_payload(body="summary text"))
        assert result["body"] == "summary text"

    def test_metadata_preserved(self) -> None:
        result = format_webhook(_payload(cost_usd=2.50, task_count=7))
        assert result["metadata"]["cost_usd"] == 2.50
        assert result["metadata"]["task_count"] == 7


# ---------------------------------------------------------------------------
# NotificationManager — event dispatch
# ---------------------------------------------------------------------------


class TestNotificationManager:
    def test_notifies_only_subscribed_events(self) -> None:
        target = NotificationTarget(
            type="webhook",
            url="https://example.com/hook",
            events=["run.completed"],
        )
        manager = NotificationManager([target])
        posted: list[str] = []

        def _post(url: str, **kwargs: Any) -> MagicMock:
            posted.append(url)
            return _ok_response()

        with patch("bernstein.core.notifications.httpx.post", side_effect=_post):
            manager.notify("task.completed", _payload(event="task.completed"))
            manager.notify("run.completed", _payload(event="run.completed"))

        assert posted == ["https://example.com/hook"]

    def test_errors_are_swallowed(self) -> None:
        target = NotificationTarget(
            type="webhook",
            url="https://bad.example.com",
            events=["run.completed"],
        )
        manager = NotificationManager([target])

        with patch(
            "bernstein.core.notifications.httpx.post",
            side_effect=httpx.ConnectError("down"),
        ):
            # Must not raise
            manager.notify("run.completed", _payload(event="run.completed"))

    def test_slack_target_posts_blocks(self) -> None:
        target = NotificationTarget(
            type="slack",
            url="https://hooks.slack.com/xxx",
            events=["run.completed"],
        )
        manager = NotificationManager([target])
        captured: list[dict[str, Any]] = []

        def _post(url: str, json: dict[str, Any], **kwargs: Any) -> MagicMock:
            captured.append({"url": url, "json": json})
            return _ok_response()

        with patch("bernstein.core.notifications.httpx.post", side_effect=_post):
            manager.notify("run.completed", _payload(event="run.completed", title="Done"))

        assert len(captured) == 1
        assert captured[0]["url"] == "https://hooks.slack.com/xxx"
        assert "blocks" in captured[0]["json"]

    def test_discord_target_posts_embeds(self) -> None:
        target = NotificationTarget(
            type="discord",
            url="https://discord.com/api/webhooks/xxx",
            events=["task.failed"],
        )
        manager = NotificationManager([target])
        captured: list[dict[str, Any]] = []

        def _post(url: str, json: dict[str, Any], **kwargs: Any) -> MagicMock:
            captured.append({"url": url, "json": json})
            return _ok_response()

        with patch("bernstein.core.notifications.httpx.post", side_effect=_post):
            manager.notify("task.failed", _payload(event="task.failed"))

        assert len(captured) == 1
        assert "embeds" in captured[0]["json"]

    def test_telegram_target_posts_to_bot_api(self) -> None:
        target = NotificationTarget(
            type="telegram",
            url="https://api.telegram.org",
            events=["run.completed"],
            token="bot123token",
            chat_id="-100456",
        )
        manager = NotificationManager([target])
        captured: list[dict[str, Any]] = []

        def _post(url: str, json: dict[str, Any], **kwargs: Any) -> MagicMock:
            captured.append({"url": url, "json": json})
            return _ok_response()

        with patch("bernstein.core.notifications.httpx.post", side_effect=_post):
            manager.notify("run.completed", _payload(event="run.completed"))

        assert len(captured) == 1
        assert "bot123token" in captured[0]["url"]
        assert captured[0]["json"]["chat_id"] == "-100456"

    def test_multiple_targets_all_get_notified(self) -> None:
        targets = [
            NotificationTarget(type="webhook", url="https://a.example.com", events=["run.completed"]),
            NotificationTarget(type="webhook", url="https://b.example.com", events=["run.completed"]),
        ]
        manager = NotificationManager(targets)
        posted: list[str] = []

        def _post(url: str, **kwargs: Any) -> MagicMock:
            posted.append(url)
            return _ok_response()

        with patch("bernstein.core.notifications.httpx.post", side_effect=_post):
            manager.notify("run.completed", _payload(event="run.completed"))

        assert sorted(posted) == ["https://a.example.com", "https://b.example.com"]

    def test_no_targets_no_http_calls(self) -> None:
        manager = NotificationManager([])

        with patch("bernstein.core.notifications.httpx.post") as mock_post:
            manager.notify("run.completed", _payload(event="run.completed"))

        mock_post.assert_not_called()
