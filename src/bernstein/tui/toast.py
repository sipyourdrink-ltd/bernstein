"""TUI-009: Notification toast for events.

Provides a toast notification system for the TUI that displays
ephemeral messages for events like task completion, agent kills,
and budget warnings. Toasts auto-dismiss after a configurable
timeout.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from rich.text import Text


class ToastLevel(Enum):
    """Severity level for toast notifications."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# Colors and icons for each toast level.
TOAST_COLORS: dict[ToastLevel, str] = {
    ToastLevel.INFO: "blue",
    ToastLevel.SUCCESS: "green",
    ToastLevel.WARNING: "yellow",
    ToastLevel.ERROR: "red",
}

TOAST_ICONS: dict[ToastLevel, str] = {
    ToastLevel.INFO: "\u2139",  # i in circle
    ToastLevel.SUCCESS: "\u2713",  # check mark
    ToastLevel.WARNING: "\u26a0",  # warning triangle
    ToastLevel.ERROR: "\u2717",  # x mark
}

# Accessible text labels (no unicode icons).
TOAST_LABELS: dict[ToastLevel, str] = {
    ToastLevel.INFO: "INFO",
    ToastLevel.SUCCESS: "OK",
    ToastLevel.WARNING: "WARN",
    ToastLevel.ERROR: "ERR",
}


@dataclass(frozen=True)
class Toast:
    """A single toast notification.

    Attributes:
        message: The notification text.
        level: Severity level.
        timestamp: When the toast was created.
        duration_s: How long the toast should display (seconds).
        source: Optional source identifier (e.g. task_id).
    """

    message: str
    level: ToastLevel = ToastLevel.INFO
    timestamp: float = field(default_factory=time.time)
    duration_s: float = 5.0
    source: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        """Check if the toast has expired.

        Args:
            now: Current time (defaults to time.time()).

        Returns:
            True if the toast should be dismissed.
        """
        if now is None:
            now = time.time()
        return (now - self.timestamp) >= self.duration_s

    @property
    def remaining_s(self) -> float:
        """Seconds remaining before auto-dismiss."""
        return max(0.0, self.duration_s - (time.time() - self.timestamp))


class ToastManager:
    """Manages a queue of toast notifications.

    Provides methods to add, dismiss, and render active toasts.
    Expired toasts are automatically pruned on access.
    """

    MAX_VISIBLE: ClassVar[int] = 5
    MAX_HISTORY: ClassVar[int] = 50

    def __init__(self) -> None:
        """Initialize the toast manager."""
        self._active: deque[Toast] = deque(maxlen=self.MAX_VISIBLE)
        self._history: deque[Toast] = deque(maxlen=self.MAX_HISTORY)

    def add(
        self,
        message: str,
        level: ToastLevel = ToastLevel.INFO,
        duration_s: float = 5.0,
        source: str = "",
    ) -> Toast:
        """Add a new toast notification.

        Args:
            message: The notification text.
            level: Severity level.
            duration_s: Display duration in seconds.
            source: Optional source identifier.

        Returns:
            The created Toast.
        """
        toast = Toast(
            message=message,
            level=level,
            duration_s=duration_s,
            source=source,
        )
        self._active.append(toast)
        self._history.append(toast)
        return toast

    def task_completed(self, task_id: str, title: str = "") -> Toast:
        """Add a task completion toast.

        Args:
            task_id: The completed task ID.
            title: Optional task title.

        Returns:
            The created Toast.
        """
        msg = f"Task {task_id[:8]} completed"
        if title:
            msg += f": {title[:40]}"
        return self.add(msg, level=ToastLevel.SUCCESS, source=task_id)

    def agent_killed(self, session_id: str, role: str = "") -> Toast:
        """Add an agent killed toast.

        Args:
            session_id: The killed agent's session ID.
            role: Optional agent role.

        Returns:
            The created Toast.
        """
        msg = f"Agent {session_id[:8]} killed"
        if role:
            msg += f" ({role})"
        return self.add(msg, level=ToastLevel.WARNING, source=session_id)

    def budget_warning(self, current_usd: float, budget_usd: float) -> Toast:
        """Add a budget warning toast.

        Args:
            current_usd: Current spend in USD.
            budget_usd: Budget cap in USD.

        Returns:
            The created Toast.
        """
        pct = int((current_usd / budget_usd) * 100) if budget_usd > 0 else 0
        return self.add(
            f"Budget {pct}% used (${current_usd:.2f}/${budget_usd:.2f})",
            level=ToastLevel.WARNING,
            duration_s=10.0,
        )

    def error(self, message: str, source: str = "") -> Toast:
        """Add an error toast.

        Args:
            message: Error description.
            source: Optional source identifier.

        Returns:
            The created Toast.
        """
        return self.add(message, level=ToastLevel.ERROR, duration_s=8.0, source=source)

    def prune(self, now: float | None = None) -> int:
        """Remove expired toasts.

        Args:
            now: Current time (defaults to time.time()).

        Returns:
            Number of toasts pruned.
        """
        if now is None:
            now = time.time()
        before = len(self._active)
        self._active = deque(
            (t for t in self._active if not t.is_expired(now)),
            maxlen=self.MAX_VISIBLE,
        )
        return before - len(self._active)

    def dismiss_all(self) -> None:
        """Dismiss all active toasts."""
        self._active.clear()

    @property
    def active_toasts(self) -> list[Toast]:
        """Return currently active (non-expired) toasts.

        Returns:
            List of active toasts, newest last.
        """
        now = time.time()
        return [t for t in self._active if not t.is_expired(now)]

    @property
    def history(self) -> list[Toast]:
        """Return toast history, newest last.

        Returns:
            List of all toasts (including expired ones).
        """
        return list(self._history)

    @property
    def count(self) -> int:
        """Number of currently active toasts."""
        return len(self.active_toasts)


def render_toast(
    toast: Toast,
    *,
    accessible: bool = False,
    width: int = 50,
) -> Text:
    """Render a single toast notification as Rich Text.

    Args:
        toast: The toast to render.
        accessible: If True, uses text labels instead of icons.
        width: Maximum width in characters.

    Returns:
        Rich Text object with the toast display.
    """
    color = TOAST_COLORS[toast.level]
    prefix = TOAST_LABELS[toast.level] if accessible else TOAST_ICONS[toast.level]
    msg = toast.message
    if len(msg) > width - len(prefix) - 3:
        msg = msg[: width - len(prefix) - 6] + "..."
    text = Text()
    text.append(f" {prefix} ", style=f"bold {color}")
    text.append(msg, style=color)
    return text


def render_toast_stack(
    manager: ToastManager,
    *,
    accessible: bool = False,
    width: int = 50,
) -> Text:
    """Render all active toasts as a vertical stack.

    Args:
        manager: ToastManager with active toasts.
        accessible: If True, uses text labels instead of icons.
        width: Maximum width per toast.

    Returns:
        Rich Text with all active toasts joined by newlines.
    """
    text = Text()
    active = manager.active_toasts
    for i, toast in enumerate(active):
        toast_text = render_toast(toast, accessible=accessible, width=width)
        text.append_text(toast_text)
        if i < len(active) - 1:
            text.append("\n")
    return text
