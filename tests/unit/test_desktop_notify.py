"""Tests for desktop notification integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bernstein.core.desktop_notify import (
    Notification,
    NotificationLevel,
    build_notify_command,
    detect_platform,
    notify_budget_threshold,
    notify_run_complete,
    notify_task_failed,
    send_notification,
)

# ---------------------------------------------------------------------------
# Notification dataclass
# ---------------------------------------------------------------------------


class TestNotification:
    """Notification creation and immutability."""

    def test_create_defaults(self) -> None:
        n = Notification(
            title="hi",
            message="body",
            level=NotificationLevel.INFO,
        )
        assert n.title == "hi"
        assert n.message == "body"
        assert n.level == NotificationLevel.INFO
        assert n.sound is False

    def test_create_with_sound(self) -> None:
        n = Notification(
            title="t",
            message="m",
            level=NotificationLevel.ERROR,
            sound=True,
        )
        assert n.sound is True

    def test_frozen(self) -> None:
        n = Notification(
            title="t",
            message="m",
            level=NotificationLevel.SUCCESS,
        )
        with pytest.raises(AttributeError):
            n.title = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NotificationLevel enum
# ---------------------------------------------------------------------------


class TestNotificationLevel:
    """Enum values are lowercase strings."""

    def test_values(self) -> None:
        assert NotificationLevel.INFO == "info"
        assert NotificationLevel.SUCCESS == "success"
        assert NotificationLevel.WARNING == "warning"
        assert NotificationLevel.ERROR == "error"


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    """Platform detection based on sys.platform."""

    def test_macos(self) -> None:
        with patch("bernstein.core.desktop_notify.sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert detect_platform() == "macos"

    def test_linux(self) -> None:
        with patch("bernstein.core.desktop_notify.sys") as mock_sys:
            mock_sys.platform = "linux"
            assert detect_platform() == "linux"

    def test_linux_variant(self) -> None:
        with patch("bernstein.core.desktop_notify.sys") as mock_sys:
            mock_sys.platform = "linux2"
            assert detect_platform() == "linux"

    def test_unsupported(self) -> None:
        with patch("bernstein.core.desktop_notify.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert detect_platform() == "unsupported"


# ---------------------------------------------------------------------------
# build_notify_command
# ---------------------------------------------------------------------------


class TestBuildNotifyCommand:
    """Command construction for each platform."""

    def test_macos_without_sound(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
            sound=False,
        )
        cmd = build_notify_command(n, "macos")
        assert cmd is not None
        assert cmd[0] == "osascript"
        assert cmd[1] == "-e"
        assert 'display notification "M" with title "T"' in cmd[2]
        assert "sound" not in cmd[2]

    def test_macos_with_sound(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.SUCCESS,
            sound=True,
        )
        cmd = build_notify_command(n, "macos")
        assert cmd is not None
        assert 'sound name "default"' in cmd[2]

    def test_linux_info(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        cmd = build_notify_command(n, "linux")
        assert cmd is not None
        assert cmd[0] == "notify-send"
        assert cmd[1:3] == ["--urgency", "low"]
        assert cmd[3] == "T"
        assert cmd[4] == "M"

    def test_linux_warning(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.WARNING,
        )
        cmd = build_notify_command(n, "linux")
        assert cmd is not None
        assert cmd[1:3] == ["--urgency", "normal"]

    def test_linux_error(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.ERROR,
        )
        cmd = build_notify_command(n, "linux")
        assert cmd is not None
        assert cmd[1:3] == ["--urgency", "critical"]

    def test_unsupported_returns_none(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        assert build_notify_command(n, "unsupported") is None


# ---------------------------------------------------------------------------
# send_notification
# ---------------------------------------------------------------------------


class TestSendNotification:
    """End-to-end send with subprocess mocked."""

    def test_success(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        with (
            patch(
                "bernstein.core.desktop_notify.detect_platform",
                return_value="macos",
            ),
            patch(
                "bernstein.core.desktop_notify.subprocess.run",
            ) as mock_run,
        ):
            mock_run.return_value.returncode = 0
            assert send_notification(n) is True
            mock_run.assert_called_once()

    def test_command_failure(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        with (
            patch(
                "bernstein.core.desktop_notify.detect_platform",
                return_value="macos",
            ),
            patch(
                "bernstein.core.desktop_notify.subprocess.run",
            ) as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = b"err"
            assert send_notification(n) is False

    def test_unsupported_platform(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        with patch(
            "bernstein.core.desktop_notify.detect_platform",
            return_value="unsupported",
        ):
            assert send_notification(n) is False

    def test_os_error(self) -> None:
        n = Notification(
            title="T",
            message="M",
            level=NotificationLevel.INFO,
        )
        with (
            patch(
                "bernstein.core.desktop_notify.detect_platform",
                return_value="linux",
            ),
            patch(
                "bernstein.core.desktop_notify.subprocess.run",
                side_effect=OSError("no notify-send"),
            ),
        ):
            assert send_notification(n) is False


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


class TestNotifyRunComplete:
    """notify_run_complete builds the right notification."""

    def test_all_passed(self) -> None:
        with patch(
            "bernstein.core.desktop_notify.send_notification",
            return_value=True,
        ) as mock_send:
            result = notify_run_complete(total_tasks=5, failed=0, cost_usd=1.23)
            assert result is True
            n = mock_send.call_args[0][0]
            assert n.level == NotificationLevel.SUCCESS
            assert "5 tasks" in n.message
            assert "0 failed" in n.message
            assert "$1.23" in n.message
            assert n.sound is True

    def test_some_failed(self) -> None:
        with patch(
            "bernstein.core.desktop_notify.send_notification",
            return_value=True,
        ) as mock_send:
            notify_run_complete(total_tasks=10, failed=3, cost_usd=4.50)
            n = mock_send.call_args[0][0]
            assert n.level == NotificationLevel.WARNING
            assert "failures" in n.title.lower()
            assert "3 failed" in n.message


class TestNotifyTaskFailed:
    """notify_task_failed builds the right notification."""

    def test_builds_error_notification(self) -> None:
        with patch(
            "bernstein.core.desktop_notify.send_notification",
            return_value=True,
        ) as mock_send:
            result = notify_task_failed(
                task_id="t-42",
                title="Fix widget",
                error="Timeout",
            )
            assert result is True
            n = mock_send.call_args[0][0]
            assert n.level == NotificationLevel.ERROR
            assert "t-42" in n.message
            assert "Timeout" in n.message
            assert n.sound is True


class TestNotifyBudgetThreshold:
    """notify_budget_threshold builds the right notification."""

    def test_builds_warning_notification(self) -> None:
        with patch(
            "bernstein.core.desktop_notify.send_notification",
            return_value=True,
        ) as mock_send:
            result = notify_budget_threshold(
                spent=8.0,
                budget=10.0,
                pct=80.0,
            )
            assert result is True
            n = mock_send.call_args[0][0]
            assert n.level == NotificationLevel.WARNING
            assert "$8.00" in n.message
            assert "$10.00" in n.message
            assert "80%" in n.message
            assert n.sound is True
