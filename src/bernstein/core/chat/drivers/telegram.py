"""Telegram driver for the chat-control bridge.

Implemented against the async ``python-telegram-bot`` v21+ API. The
import of that SDK is guarded so the module can always be imported --
the SDK is only required when :meth:`TelegramBridge.start` actually
runs. That keeps ``bernstein chat serve --platform=discord`` working
for users who only installed the Discord extra.

Key behaviours:

  * **Slash commands.** Handlers registered via :meth:`on_command` are
    routed based on the leading ``/word`` in each message. The raw
    :class:`~bernstein.core.chat.bridge.ChatMessage` is forwarded so
    handlers can read the remaining argv.
  * **Approval buttons.** :meth:`push_approval` renders an
    ``InlineKeyboardMarkup`` with two callback buttons. The callback
    data encodes ``approve:<id>`` / ``reject:<id>`` so the driver can
    resolve the decision back to the pending approval without any
    extra state.
  * **Edit throttle.** :meth:`edit_message` is debounced to one edit
    per thread per 500ms. Telegram rate-limits bot edits aggressively
    and a streaming agent will otherwise melt the channel.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.chat.bridge import (
    BridgeProtocol,
    ButtonHandler,
    ChatMessage,
    CommandHandler,
    PendingApproval,
)

__all__ = ["EDIT_THROTTLE_S", "TelegramBridge", "TelegramDependencyError"]

logger = logging.getLogger(__name__)

#: Minimum seconds between consecutive edits to the same message id.
EDIT_THROTTLE_S: float = 0.5


class TelegramDependencyError(RuntimeError):
    """Raised when ``python-telegram-bot`` is not installed."""


@dataclass(slots=True)
class _EditState:
    """Per-message debouncing bookkeeping.

    Attributes:
        last_edit_ts: Monotonic timestamp of the last successful flush.
        pending_text: Latest body awaiting flush. Empty string means no
            pending write.
        task: Scheduled flush coroutine, if any.
    """

    last_edit_ts: float = 0.0
    pending_text: str = ""
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class TelegramBridge(BridgeProtocol):
    """Telegram implementation of :class:`BridgeProtocol`."""

    platform: str = "telegram"

    def __init__(self, token: str) -> None:
        """Create a bridge bound to a Telegram bot ``token``.

        The token is captured eagerly but no network I/O happens until
        :meth:`start` is called.
        """
        if not token:
            raise ValueError("Telegram bot token must be non-empty.")
        self._token = token
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handler: ButtonHandler | None = None
        self._app: Any = None
        self._edit_state: dict[str, _EditState] = {}
        self._edit_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register ``handler`` for the slash command ``/<name>``."""
        self._command_handlers[name.lstrip("/")] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        """Register the single approve/reject callback."""
        self._button_handler = handler

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Telegram and begin polling for updates."""
        tg_ext: Any = _import_telegram_ext()
        self._app = tg_ext.Application.builder().token(self._token).build()

        # Install every registered slash command.
        tg_cmd_handler_cls: Any = tg_ext.CommandHandler
        for name in self._command_handlers:
            self._app.add_handler(
                tg_cmd_handler_cls(name, self._dispatch_command),
            )

        cb_cls: Any = tg_ext.CallbackQueryHandler
        self._app.add_handler(cb_cls(self._dispatch_button))

        await self._app.initialize()
        await self._app.start()
        updater = getattr(self._app, "updater", None)
        if updater is not None:
            await updater.start_polling()

    async def stop(self) -> None:
        """Flush pending edits and disconnect cleanly."""
        # Cancel any in-flight throttled flushes so the loop can exit.
        async with self._edit_lock:
            for state in self._edit_state.values():
                task = state.task
                if task is not None and not task.done():
                    task.cancel()
            self._edit_state.clear()

        if self._app is None:
            return
        updater = getattr(self._app, "updater", None)
        if updater is not None:
            await updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None

    # ------------------------------------------------------------------
    # Outbound primitives
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, text: str) -> str:
        """Post ``text`` to ``thread_id`` and return the new message id."""
        app = self._require_app()
        sent = await app.bot.send_message(chat_id=_to_chat_id(thread_id), text=text)
        return str(sent.message_id)

    async def edit_message(self, thread_id: str, message_id: str, text: str) -> None:
        """Edit ``message_id`` in ``thread_id``, debounced to 2Hz per message.

        Rapid successive calls to the same ``message_id`` collapse into
        a single deferred write, guaranteeing at most one Telegram API
        call every :data:`EDIT_THROTTLE_S` seconds. The most recently
        requested body always wins.
        """
        key = f"{thread_id}:{message_id}"
        now = time.monotonic()
        async with self._edit_lock:
            state = self._edit_state.setdefault(key, _EditState())
            state.pending_text = text
            elapsed = now - state.last_edit_ts
            if elapsed >= EDIT_THROTTLE_S and (state.task is None or state.task.done()):
                state.last_edit_ts = now
                body = state.pending_text
                state.pending_text = ""
                await self._flush_edit(thread_id, message_id, body)
                return
            if state.task is None or state.task.done():
                delay = max(0.0, EDIT_THROTTLE_S - elapsed)
                state.task = asyncio.create_task(
                    self._deferred_flush(thread_id, message_id, delay, key),
                )

    async def push_approval(self, approval: PendingApproval) -> str:
        """Render an inline approve/reject card for ``approval``."""
        tg: Any = _import_telegram()
        keyboard_cls: Any = tg.InlineKeyboardMarkup
        button_cls: Any = tg.InlineKeyboardButton
        keyboard = keyboard_cls(
            [
                [
                    button_cls("Approve", callback_data=f"approve:{approval.approval_id}"),
                    button_cls("Reject", callback_data=f"reject:{approval.approval_id}"),
                ],
            ],
        )
        app = self._require_app()
        sent = await app.bot.send_message(
            chat_id=_to_chat_id(approval.thread_id),
            text=f"{approval.title}\n\n{approval.body}",
            reply_markup=keyboard,
        )
        return str(sent.message_id)

    # ------------------------------------------------------------------
    # Inbound dispatch -- wired into python-telegram-bot handlers.
    # ------------------------------------------------------------------

    async def _dispatch_command(self, update: Any, _context: Any) -> None:
        """Route a ``/command`` to the matching registered handler."""
        message = _extract_message(update)
        if message is None:
            return
        text: str = str(message.text or "")
        if not text.startswith("/"):
            return
        parts: list[str] = [str(p) for p in text.split()]
        name = parts[0][1:].split("@", 1)[0]  # strip @botname suffix
        handler = self._command_handlers.get(name)
        if handler is None:
            return
        chat_id = str(getattr(message.chat, "id", "") or "")
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", "") or "")
        await handler(
            ChatMessage(
                thread_id=chat_id,
                user_id=user_id,
                text=text,
                message_id=str(getattr(message, "message_id", "") or ""),
                args=parts[1:],
                raw=update,
            ),
        )

    async def _dispatch_button(self, update: Any, _context: Any) -> None:
        """Route an ``InlineKeyboardButton`` press to :attr:`_button_handler`."""
        if self._button_handler is None:
            return
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        data = str(getattr(query, "data", "") or "")
        if ":" not in data:
            return
        decision, approval_id = data.split(":", 1)
        if decision not in {"approve", "reject"}:
            return
        answer: Any = getattr(query, "answer", None)
        if callable(answer):
            result: Any = answer()
            if hasattr(result, "__await__"):
                await result
        message = getattr(query, "message", None)
        chat_id = str(getattr(getattr(message, "chat", None), "id", "") or "")
        await self._button_handler(chat_id, approval_id, decision)

    # ------------------------------------------------------------------
    # Throttle internals
    # ------------------------------------------------------------------

    async def _deferred_flush(
        self,
        thread_id: str,
        message_id: str,
        delay: float,
        key: str,
    ) -> None:
        """Sleep ``delay`` then flush the pending body for ``key``."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # Propagate cancellation after releasing local state; swallowing
            # CancelledError silently would desync the asyncio task tree
            # (Sonar python:S7497).
            raise
        async with self._edit_lock:
            state = self._edit_state.get(key)
            if state is None or not state.pending_text:
                return
            body = state.pending_text
            state.pending_text = ""
            state.last_edit_ts = time.monotonic()
        await self._flush_edit(thread_id, message_id, body)

    async def _flush_edit(self, thread_id: str, message_id: str, text: str) -> None:
        """Issue the actual ``edit_message_text`` API call."""
        app = self._require_app()
        try:
            await app.bot.edit_message_text(
                chat_id=_to_chat_id(thread_id),
                message_id=int(message_id),
                text=text,
            )
        except Exception as exc:  # pragma: no cover - network-only path.
            logger.warning("telegram edit failed for %s:%s: %s", thread_id, message_id, exc)

    def _require_app(self) -> Any:
        if self._app is None:
            raise RuntimeError(
                "TelegramBridge is not started; call await bridge.start() first.",
            )
        return self._app


# ---------------------------------------------------------------------------
# Import helpers -- keep the SDK optional.
# ---------------------------------------------------------------------------


def _import_telegram() -> Any:
    try:
        return importlib.import_module("telegram")
    except ImportError as exc:  # pragma: no cover - exercised via tests with stubbed module.
        raise TelegramDependencyError(
            "python-telegram-bot is not installed. Install with: pip install 'bernstein[telegram]'",
        ) from exc


def _import_telegram_ext() -> Any:
    try:
        return importlib.import_module("telegram.ext")
    except ImportError as exc:  # pragma: no cover - exercised via tests with stubbed module.
        raise TelegramDependencyError(
            "python-telegram-bot is not installed. Install with: pip install 'bernstein[telegram]'",
        ) from exc


def _extract_message(update: Any) -> Any:
    """Return ``update.message`` or ``update.edited_message`` if present."""
    message = getattr(update, "message", None)
    if message is not None:
        return message
    return getattr(update, "edited_message", None)


def _to_chat_id(thread_id: str) -> int | str:
    """Coerce ``thread_id`` to ``int`` for Telegram's integer chat ids."""
    try:
        return int(thread_id)
    except (TypeError, ValueError):
        return thread_id
