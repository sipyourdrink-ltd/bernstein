"""Desktop notification integration for macOS and Linux.

Sends native OS notifications for run completions, task failures, and
budget threshold warnings.  On macOS uses ``osascript``; on Linux uses
``notify-send``.  Platforms without a supported mechanism silently
return ``False`` from :func:`send_notification`.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums / data
# ---------------------------------------------------------------------------


class NotificationLevel(StrEnum):
    """Severity of a desktop notification."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Notification:
    """Immutable descriptor for a single desktop notification.

    Attributes:
        title: Notification title / headline.
        message: Body text.
        level: Severity level.
        sound: Whether to play the default system sound.
    """

    title: str
    message: str
    level: NotificationLevel
    sound: bool = False


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

Platform = Literal["macos", "linux", "unsupported"]


def detect_platform() -> Platform:
    """Return the current OS as a platform tag.

    Returns:
        ``"macos"`` on Darwin, ``"linux"`` on Linux, ``"unsupported"``
        otherwise.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def build_notify_command(
    notification: Notification,
    platform: Platform,
) -> list[str] | None:
    """Build the OS-specific command to display *notification*.

    Args:
        notification: The notification to display.
        platform: Target platform tag (``"macos"``, ``"linux"``, or
            ``"unsupported"``).

    Returns:
        A subprocess argv list, or ``None`` when the platform has no
        supported notification mechanism.
    """
    if platform == "macos":
        script = f'display notification "{notification.message}" with title "{notification.title}"'
        if notification.sound:
            script += ' sound name "default"'
        return ["osascript", "-e", script]

    if platform == "linux":
        urgency_map: dict[NotificationLevel, str] = {
            NotificationLevel.INFO: "low",
            NotificationLevel.SUCCESS: "low",
            NotificationLevel.WARNING: "normal",
            NotificationLevel.ERROR: "critical",
        }
        urgency = urgency_map.get(notification.level, "normal")
        return [
            "notify-send",
            "--urgency",
            urgency,
            notification.title,
            notification.message,
        ]

    return None


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def send_notification(notification: Notification) -> bool:
    """Detect the platform, build the command, and display *notification*.

    Returns:
        ``True`` if the notification was delivered (exit code 0),
        ``False`` otherwise (unsupported platform, command failure, etc.).
    """
    platform = detect_platform()
    cmd = build_notify_command(notification, platform)
    if cmd is None:
        log.debug("Desktop notifications not supported on %s", platform)
        return False

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        if result.returncode != 0:
            log.warning(
                "Notification command failed (rc=%d): %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Failed to send desktop notification: %s", exc)
        return False
    else:
        return True


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def notify_run_complete(total_tasks: int, failed: int, cost_usd: float) -> bool:
    """Send a summary notification when a run finishes.

    Args:
        total_tasks: Total number of tasks in the run.
        failed: How many tasks failed.
        cost_usd: Total estimated cost in USD.

    Returns:
        ``True`` if the notification was delivered.
    """
    if failed:
        level = NotificationLevel.WARNING
        title = "Bernstein run finished with failures"
    else:
        level = NotificationLevel.SUCCESS
        title = "Bernstein run complete"

    message = f"{total_tasks} tasks, {failed} failed, ${cost_usd:.2f}"
    return send_notification(
        Notification(title=title, message=message, level=level, sound=True),
    )


def notify_task_failed(task_id: str, title: str, error: str) -> bool:
    """Send a notification when a single task fails.

    Args:
        task_id: Identifier of the failed task.
        title: Human-readable task title.
        error: Error description.

    Returns:
        ``True`` if the notification was delivered.
    """
    message = f"Task {task_id}: {title}\n{error}"
    return send_notification(
        Notification(
            title="Task failed",
            message=message,
            level=NotificationLevel.ERROR,
            sound=True,
        ),
    )


def notify_budget_threshold(spent: float, budget: float, pct: float) -> bool:
    """Send a notification when spending crosses a budget threshold.

    Args:
        spent: Amount spent so far in USD.
        budget: Total budget in USD.
        pct: Percentage of budget consumed (0-100).

    Returns:
        ``True`` if the notification was delivered.
    """
    message = f"${spent:.2f} of ${budget:.2f} ({pct:.0f}%)"
    return send_notification(
        Notification(
            title="Budget threshold reached",
            message=message,
            level=NotificationLevel.WARNING,
            sound=True,
        ),
    )
