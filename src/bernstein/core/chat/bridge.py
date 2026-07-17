"""Abstract bridge protocol shared by all chat drivers.

A *bridge* is the driver-specific glue between a chat platform and the
task-server. Drivers subscribe to a small set of lifecycle events
(``on_command`` / ``on_button``) and offer a handful of outbound
primitives (``send_message`` / ``edit_message`` / ``push_approval``).

Keeping this surface minimal lets the CLI and the per-thread session
manager stay platform-agnostic: any class that conforms to
:class:`BridgeProtocol` can slot in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.approval.card import ApprovalCardV2


@dataclass(slots=True)
class ChatMessage:
    """Inbound message normalised across platforms.

    Attributes:
        thread_id: Stable identifier for the chat thread / DM / channel.
            Drivers MUST use a value that is stable for the lifetime of
            the conversation so bindings survive restarts.
        user_id: Platform-native user id of the sender, as a string.
        text: Raw text body of the message.
        message_id: Platform-native message id, used when we later want
            to edit the same message to stream output back.
        args: Parsed positional arguments for slash-style commands. The
            driver splits on whitespace so common cases are handled
            without every handler re-parsing the body.
        raw: Opaque platform payload for advanced consumers.
    """

    thread_id: str
    user_id: str
    text: str
    message_id: str = ""
    args: list[str] = field(default_factory=list)
    raw: Any = None


@dataclass(slots=True)
class PendingApproval:
    """Outbound payload describing a gated tool call awaiting a human.

    Attributes:
        approval_id: Unique id (typically the task id); used as the
            callback-data token for the inline buttons.
        title: Short human-friendly headline.
        body: Multi-line description (diff preview, tool args, etc.).
        thread_id: Where to deliver the approval card.
        card: Optional hash-committed v2 decision envelope (issue #2511).
            When set, drivers render the envelope fields verbatim via
            :func:`bernstein.core.approval.card.render_card_text` instead of
            re-summarising ``body``, so what is displayed is exactly what was
            hashed into ``card_hash``.
        card_hash: The ``card_hash`` the decision must echo on resolve. Empty
            for legacy v1 cards.
    """

    approval_id: str
    title: str
    body: str
    thread_id: str
    card: ApprovalCardV2 | None = None
    card_hash: str = ""


def approval_body(approval: PendingApproval) -> str:
    """Return the operator-facing body a driver should render for *approval*.

    For a v2 card the body is the verbatim projection of the hashed envelope;
    for a legacy v1 card it is the free-text ``body``. Centralising this keeps
    every driver rendering exactly what was hashed (no driver-local
    re-summarisation).
    """
    if approval.card is not None:
        from bernstein.core.approval.card import render_card_text

        return render_card_text(approval.card)
    return approval.body


CommandHandler = Callable[[ChatMessage], Awaitable[None]]
ButtonHandler = Callable[[str, str, str], Awaitable[None]]
"""Signature: ``(thread_id, approval_id, decision)`` where decision is
``"approve"`` or ``"reject"``."""


class BridgeProtocol(ABC):
    """Platform-agnostic chat driver interface.

    Concrete drivers are expected to be async and non-blocking. ``start``
    runs until ``stop`` is called or an unrecoverable transport error
    occurs.
    """

    platform: str = "abstract"

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and begin dispatching events."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect cleanly and release all resources."""

    @abstractmethod
    async def send_message(self, thread_id: str, text: str) -> str:
        """Post a new message and return its platform-native id."""

    @abstractmethod
    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        """Replace the body of a previously-sent message."""

    @abstractmethod
    async def push_approval(self, approval: PendingApproval) -> str:
        """Render an approval card with inline approve/reject buttons.

        Returns:
            The platform-native message id for the approval card, which
            the caller may keep so it can later edit or delete it.
        """

    @abstractmethod
    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register a slash-command handler (``name`` without leading ``/``)."""

    @abstractmethod
    def on_button(self, handler: ButtonHandler) -> None:
        """Register the approval-button callback."""
