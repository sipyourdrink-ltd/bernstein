"""Discord driver stub.

Conforms to :class:`~bernstein.core.chat.bridge.BridgeProtocol` so that
wiring code, tests, and the CLI can treat every driver uniformly today
while the real implementation lands in a follow-up.

All network-touching methods raise :class:`NotImplementedError` with a
pointer to the follow-up ticket. In-memory registration methods
(``on_command`` / ``on_button``) are no-ops so a caller that wires
handlers up front does not explode -- they simply have no effect.
"""

from __future__ import annotations

from bernstein.core.chat.bridge import (
    BridgeProtocol,
    ButtonHandler,
    CommandHandler,
    PendingApproval,
)

__all__ = ["DISCORD_STUB_MESSAGE", "DiscordBridge"]

DISCORD_STUB_MESSAGE = "Discord driver coming in op-001b -- PRs welcome"


class DiscordBridge(BridgeProtocol):
    """Placeholder driver for Discord; raises on :meth:`start`."""

    platform: str = "discord"

    def __init__(self, token: str = "") -> None:
        """Accept a token so the CLI surface matches the Telegram driver."""
        self._token = token

    async def start(self) -> None:
        """Reject startup until the real driver lands."""
        raise NotImplementedError(DISCORD_STUB_MESSAGE)

    async def stop(self) -> None:
        """No-op; nothing was ever started."""

    async def send_message(self, thread_id: str, text: str) -> str:
        raise NotImplementedError(DISCORD_STUB_MESSAGE)

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        raise NotImplementedError(DISCORD_STUB_MESSAGE)

    async def push_approval(self, approval: PendingApproval) -> str:
        raise NotImplementedError(DISCORD_STUB_MESSAGE)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """No-op: command registrations are silently discarded."""

    def on_button(self, handler: ButtonHandler) -> None:
        """No-op: button handler registrations are silently discarded."""
