"""Tests for notification delivery backends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.models import SmtpConfig
from bernstein.core.notifications import NotificationManager, NotificationPayload, NotificationTarget


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
        patch("bernstein.core.notifications.platform", "darwin"),
        patch("bernstein.core.notifications.which", return_value="/usr/local/bin/terminal-notifier"),
        patch("bernstein.core.notifications.subprocess.run") as mock_run,
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
    )


def test_desktop_notification_uses_notify_send_on_linux() -> None:
    payload = NotificationPayload(event="task.failed", title="Task Failed", body="The task failed.")
    target = NotificationTarget(type="desktop", url="", events=["task.failed"])
    manager = NotificationManager(targets=[target])

    with (
        patch("bernstein.core.notifications.platform", "linux"),
        patch("bernstein.core.notifications.which", return_value="/usr/bin/notify-send"),
        patch("bernstein.core.notifications.subprocess.run") as mock_run,
    ):
        manager.notify("task.failed", payload)

    mock_run.assert_called_once_with(
        ["/usr/bin/notify-send", "Task Failed", "The task failed."],
        check=False,
        capture_output=True,
        text=True,
    )


def test_desktop_notification_noops_when_notifier_missing() -> None:
    payload = NotificationPayload(event="task.completed", title="Task Done", body="The task is finished.")
    target = NotificationTarget(type="desktop", url="", events=["task.completed"])
    manager = NotificationManager(targets=[target])

    with (
        patch("bernstein.core.notifications.which", return_value=None),
        patch("bernstein.core.notifications.subprocess.run") as mock_run,
    ):
        manager.notify("task.completed", payload)

    mock_run.assert_not_called()
