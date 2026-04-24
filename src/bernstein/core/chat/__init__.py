"""Chat-control bridges for driving Bernstein agents from messaging apps.

This sub-package exposes a small platform-agnostic surface for connecting
chat clients (Telegram today, Discord and Slack as conformant stubs) to
the task-server. The pieces are intentionally decoupled so a new driver
can be added without touching the CLI or the session bookkeeping:

  * :class:`BridgeProtocol` -- the abstract interface every driver
    implements.
  * :class:`BindingStore`   -- persists the mapping between a chat thread
    and an active agent session, with atomic writes under ``.sdd/chat/``.
  * :class:`AllowList`      -- permission loader reading
    ``bernstein.yaml :: chat.allowed_users``.
  * :func:`load_driver`     -- factory that resolves a platform name to a
    driver class while keeping optional SDKs import-guarded.
"""

from __future__ import annotations

from bernstein.core.chat.bindings import Binding, BindingStore
from bernstein.core.chat.bridge import (
    BridgeProtocol,
    ChatMessage,
    PendingApproval,
)
from bernstein.core.chat.permissions import AllowList, load_allow_list

__all__ = [
    "AllowList",
    "Binding",
    "BindingStore",
    "BridgeProtocol",
    "ChatMessage",
    "PendingApproval",
    "load_allow_list",
    "load_driver",
]


def load_driver(platform: str) -> type[BridgeProtocol]:
    """Resolve a platform name to a :class:`BridgeProtocol` subclass.

    The import for each driver is deferred so that an optional dependency
    missing on the host (for example ``python-telegram-bot`` not being
    installed) only surfaces when that driver is actually selected.

    Args:
        platform: One of ``"telegram"``, ``"discord"``, ``"slack"``.

    Returns:
        The driver class. Instantiation is the caller's responsibility.

    Raises:
        ValueError: If ``platform`` is not a known driver name.
    """
    normalised = platform.strip().lower()
    if normalised == "telegram":
        from bernstein.core.chat.drivers.telegram import TelegramBridge

        return TelegramBridge
    if normalised == "discord":
        from bernstein.core.chat.drivers.discord import DiscordBridge

        return DiscordBridge
    if normalised == "slack":
        from bernstein.core.chat.drivers.slack import SlackBridge

        return SlackBridge
    raise ValueError(
        f"Unknown chat platform {platform!r}. Supported: telegram, discord, slack.",
    )
