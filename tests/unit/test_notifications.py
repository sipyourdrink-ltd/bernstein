"""Tests for notification delivery backends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.models import SmtpConfig

from bernstein.core.notifications import (
    NotificationManager,
    NotificationPayload,
    NotificationTarget,
    format_pagerduty,
)


def test_email_notification_sent() -> None:
    smtp_config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="bernstein@example.com",
        to_addresses=["admin@example.com"],
    )

    target = NotificationTarget(type="email", url="", events=["task.completed"])
    manager = NotificationManager(targets=[target], smtp_config=smtp_config)

    payload = NotificationPayload(
        event="task.completed",
        title="Task Done",
        body="The task is finished.",
        metadata={"cost": "$0.05"},
    )

    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        manager.notify("task.completed", payload)

    mock_smtp.assert_called_with("smtp.example.com", 587)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_with("user", "pass")
    mock_server.send_message.assert_called_once()

    msg = mock_server.send_message.call_args[0][0]
    assert msg["Subject"] == "Task Done"
    assert "The task is finished." in msg.get_payload()[0].get_payload()


def test_desktop_notification_uses_terminal_notifier_on_macos() -> None:
    payload = NotificationPayload(event="task.completed", title="Task Done", body="The task is finished.")
    target = NotificationTarget(type="desktop", url="", events=["task.completed"])
    manager = NotificationManager(targets=[target])

    with (
        patch("bernstein.core.communication.notifications.platform", "darwin"),
        patch("bernstein.core.communication.notifications.which", return_value="/usr/local/bin/terminal-notifier"),
        patch("bernstein.core.communication.notifications.subprocess.run") as mock_run,
    ):
        manager.notify("task.completed", payload)

    mock_run.assert_called_once_with(
        [
            "/usr/local/bin/terminal-notifier",
            "-title",
            "Task Done",
            "-message",
            "The task is finished.",
            "-group",
            "bernstein",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_desktop_notification_uses_notify_send_on_linux() -> None:
    payload = NotificationPayload(event="task.failed", title="Task Failed", body="The task failed.")
    target = NotificationTarget(type="desktop", url="", events=["task.failed"])
    manager = NotificationManager(targets=[target])

    with (
        patch("bernstein.core.communication.notifications.platform", "linux"),
        patch("bernstein.core.communication.notifications.which", return_value="/usr/bin/notify-send"),
        patch("bernstein.core.communication.notifications.subprocess.run") as mock_run,
    ):
        manager.notify("task.failed", payload)

    mock_run.assert_called_once_with(
        ["/usr/bin/notify-send", "Task Failed", "The task failed."],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_desktop_notification_noops_when_notifier_missing() -> None:
    payload = NotificationPayload(event="task.completed", title="Task Done", body="The task is finished.")
    target = NotificationTarget(type="desktop", url="", events=["task.completed"])
    manager = NotificationManager(targets=[target])

    with (
        patch("bernstein.core.communication.notifications.which", return_value=None),
        patch("bernstein.core.communication.notifications.subprocess.run") as mock_run,
    ):
        manager.notify("task.completed", payload)

    mock_run.assert_not_called()


# --- PagerDuty tests ---


def test_format_pagerduty_payload() -> None:
    payload = NotificationPayload(
        event="incident.critical",
        title="Budget cap reached",
        body="Spending cap of $10.00 reached.",
        metadata={"budget_usd": "10.00", "spent_usd": "10.05"},
    )
    body = format_pagerduty(payload, routing_key="test-key-123")

    assert body["routing_key"] == "test-key-123"
    assert body["event_action"] == "trigger"
    assert body["dedup_key"] == "bernstein:incident.critical"
    assert body["payload"]["source"] == "bernstein"
    assert body["payload"]["severity"] == "critical"
    assert body["payload"]["component"] == "orchestrator"
    assert "Budget cap reached" in body["payload"]["summary"]


def test_format_pagerduty_severity_mapping() -> None:
    """Ensure different events get different PagerDuty severity levels."""
    for event, expected_sev in [
        ("task.completed", "info"),
        ("task.failed", "warning"),
        ("budget.warning", "warning"),
        ("budget.exhausted", "critical"),
        ("incident.critical", "critical"),
    ]:
        payload = NotificationPayload(event=event, title="Test", body="Test body")
        body = format_pagerduty(payload, routing_key="key")
        assert body["payload"]["severity"] == expected_sev, event


def test_pagerduty_notification_sent() -> None:
    target = NotificationTarget(
        type="pagerduty",
        url="https://events.pagerduty.com/v2/enqueue",
        routing_key="test-routing-key",
        events=["incident.critical"],
    )
    manager = NotificationManager(targets=[target])
    payload = NotificationPayload(
        event="incident.critical",
        title="Critical failure",
        body="75% of tasks failing",
    )

    with patch("httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp
        manager.notify("incident.critical", payload)

    mock_post.assert_called_once_with(
        "https://events.pagerduty.com/v2/enqueue",
        json={
            "routing_key": "test-routing-key",
            "event_action": "trigger",
            "dedup_key": "bernstein:incident.critical",
            "payload": {
                "summary": "Critical failure: 75% of tasks failing",
                "source": "bernstein",
                "severity": "critical",
                "component": "orchestrator",
                "custom_details": {},
            },
        },
        timeout=10.0,
    )


def test_pagerduty_notification_skipped_without_routing_key() -> None:
    target = NotificationTarget(
        type="pagerduty",
        url="https://events.pagerduty.com/v2/enqueue",
        routing_key=None,
        events=["incident.critical"],
    )
    manager = NotificationManager(targets=[target])
    payload = NotificationPayload(event="incident.critical", title="Test", body="Test body")

    with patch("httpx.post") as mock_post:
        manager.notify("incident.critical", payload)

    mock_post.assert_not_called()
