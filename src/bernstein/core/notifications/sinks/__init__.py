"""First-party notification drivers.

Each module exposes a single class that satisfies
:class:`bernstein.core.notifications.protocol.NotificationSink`. The
classes are wired into the registry by
:mod:`bernstein.core.notifications.registry` via the
``_BUILTIN_DRIVERS`` table.
"""

from __future__ import annotations

from bernstein.core.notifications.sinks.discord import DiscordSink
from bernstein.core.notifications.sinks.email_smtp import EmailSmtpSink
from bernstein.core.notifications.sinks.shell import ShellSink
from bernstein.core.notifications.sinks.slack import SlackSink
from bernstein.core.notifications.sinks.telegram import TelegramSink
from bernstein.core.notifications.sinks.webhook import WebhookSink

__all__ = [
    "DiscordSink",
    "EmailSmtpSink",
    "ShellSink",
    "SlackSink",
    "TelegramSink",
    "WebhookSink",
]
