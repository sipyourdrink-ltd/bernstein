"""Unit tests for chat drivers: Telegram flow + Discord/Slack stubs."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from bernstein.core.chat.bridge import ChatMessage, PendingApproval
from bernstein.core.chat.drivers.discord import DISCORD_STUB_MESSAGE, DiscordBridge
from bernstein.core.chat.drivers.slack import SLACK_STUB_MESSAGE, SlackBridge

# ---------------------------------------------------------------------------
# Fake python-telegram-bot package
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeSentMessage:
    message_id: int = 100


@dataclass(slots=True)
class _FakeBot:
    sent: list[dict[str, Any]] = field(default_factory=list)
    edited: list[dict[str, Any]] = field(default_factory=list)

    async def send_message(self, **kwargs: Any) -> _FakeSentMessage: # NOSONAR — async-signature required by protocol / fixture
        self.sent.append(kwargs)
        return _FakeSentMessage(message_id=len(self.sent) + 99)

    async def edit_message_text(self, **kwargs: Any) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.edited.append(kwargs)


@dataclass(slots=True)
class _FakeUpdater:
    started: bool = False
    stopped: bool = False

    async def start_polling(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.started = True

    async def stop(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.stopped = True


@dataclass(slots=True)
class _FakeApplication:
    token: str = ""
    bot: _FakeBot = field(default_factory=_FakeBot)
    updater: _FakeUpdater = field(default_factory=_FakeUpdater)
    handlers: list[Any] = field(default_factory=list)
    initialized: bool = False
    started: bool = False
    shutdown_called: bool = False

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.initialized = True

    async def start(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.started = True

    async def stop(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.started = False

    async def shutdown(self) -> None: # NOSONAR — async-signature required by protocol / fixture
        self.shutdown_called = True


class _FakeApplicationBuilder:
    def __init__(self) -> None:
        self._token = ""

    def token(self, value: str) -> _FakeApplicationBuilder:
        self._token = value
        return self

    def build(self) -> _FakeApplication:
        return _FakeApplication(token=self._token)


class _FakeCommandHandler:
    def __init__(self, name: str, callback: Any) -> None:
        self.name = name
        self.callback = callback


class _FakeCallbackQueryHandler:
    def __init__(self, callback: Any) -> None:
        self.callback = callback


class _FakeInlineKeyboardButton:
    def __init__(self, text: str, callback_data: str) -> None:
        self.text = text
        self.callback_data = callback_data


class _FakeInlineKeyboardMarkup:
    def __init__(self, rows: list[list[_FakeInlineKeyboardButton]]) -> None:
        self.rows = rows


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a fake ``telegram`` and ``telegram.ext`` in ``sys.modules``."""
    tg = types.ModuleType("telegram")
    tg.InlineKeyboardButton = _FakeInlineKeyboardButton  # type: ignore[attr-defined]
    tg.InlineKeyboardMarkup = _FakeInlineKeyboardMarkup  # type: ignore[attr-defined]

    ext = types.ModuleType("telegram.ext")

    class _AppShim:
        @staticmethod
        def builder() -> _FakeApplicationBuilder:
            return _FakeApplicationBuilder()

    ext.Application = _AppShim  # type: ignore[attr-defined]
    ext.CommandHandler = _FakeCommandHandler  # type: ignore[attr-defined]
    ext.CallbackQueryHandler = _FakeCallbackQueryHandler  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "telegram", tg)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)


# ---------------------------------------------------------------------------
# Discord / Slack stubs
# ---------------------------------------------------------------------------


def test_discord_start_raises_stub_message() -> None:
    bridge = DiscordBridge(token="x")
    with pytest.raises(NotImplementedError) as excinfo:
        asyncio.run(bridge.start())
    assert DISCORD_STUB_MESSAGE in str(excinfo.value)
    assert "op-001b" in str(excinfo.value)


def test_slack_start_raises_stub_message() -> None:
    bridge = SlackBridge(token="x")
    with pytest.raises(NotImplementedError) as excinfo:
        asyncio.run(bridge.start())
    assert SLACK_STUB_MESSAGE in str(excinfo.value)
    assert "op-001b" in str(excinfo.value)


def test_stub_registrations_are_noops() -> None:
    """Registering handlers on stubs is a silent no-op."""
    bridge = DiscordBridge()
    bridge.on_command("run", lambda _m: _async_noop())  # type: ignore[misc,arg-type]
    bridge.on_button(lambda _t, _a, _d: _async_noop())  # type: ignore[misc,arg-type]


async def _async_noop() -> None: # NOSONAR — async-signature required by protocol / fixture
    return None


# ---------------------------------------------------------------------------
# Telegram flow
# ---------------------------------------------------------------------------


def test_telegram_empty_token_rejected() -> None:
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    with pytest.raises(ValueError):
        TelegramBridge(token="")


def test_telegram_run_command_routes_to_registered_handler(fake_telegram: None) -> None:
    """A ``/run`` update must be dispatched to the registered handler."""
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    received: list[ChatMessage] = []

    async def handler(msg: ChatMessage) -> None: # NOSONAR — async-signature required by protocol / fixture
        received.append(msg)

    bridge = TelegramBridge(token="dummy")
    bridge.on_command("run", handler)

    async def scenario() -> None:
        await bridge.start()
        # Locate the /run handler the driver registered on the fake app.
        app = bridge._app  # type: ignore[attr-defined]
        run_handlers = [h for h in app.handlers if isinstance(h, _FakeCommandHandler) and h.name == "run"]
        assert len(run_handlers) == 1

        update = _fake_update(text='/run "Add JWT auth"', chat_id=42, user_id=7, message_id=11)
        await run_handlers[0].callback(update, None)
        await bridge.stop()

    asyncio.run(scenario())
    assert len(received) == 1
    assert received[0].thread_id == "42"
    assert received[0].user_id == "7"
    assert received[0].args == ['"Add', "JWT", 'auth"']


def test_telegram_approval_button_round_trip(fake_telegram: None) -> None:
    """A callback_query with ``approve:<id>`` should fire the button handler."""
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    decisions: list[tuple[str, str, str]] = []

    async def button(thread_id: str, approval_id: str, decision: str) -> None: # NOSONAR — async-signature required by protocol / fixture
        decisions.append((thread_id, approval_id, decision))

    bridge = TelegramBridge(token="dummy")
    bridge.on_button(button)

    async def scenario() -> None:
        await bridge.start()
        app = bridge._app  # type: ignore[attr-defined]
        cb_handlers = [h for h in app.handlers if isinstance(h, _FakeCallbackQueryHandler)]
        assert len(cb_handlers) == 1
        update = _fake_callback_update(data="approve:t-42", chat_id=99)
        await cb_handlers[0].callback(update, None)
        # Rejection path too.
        await cb_handlers[0].callback(_fake_callback_update(data="reject:t-43", chat_id=99), None)
        await bridge.stop()

    asyncio.run(scenario())
    assert decisions == [("99", "t-42", "approve"), ("99", "t-43", "reject")]


def test_telegram_push_approval_renders_inline_keyboard(fake_telegram: None) -> None:
    """push_approval must attach an InlineKeyboardMarkup with two buttons."""
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    bridge = TelegramBridge(token="dummy")

    async def scenario() -> list[dict[str, Any]]:
        await bridge.start()
        await bridge.push_approval(
            PendingApproval(
                approval_id="t-7",
                title="Approve shell command?",
                body="rm -rf /tmp/scratch",
                thread_id="42",
            ),
        )
        app = bridge._app  # type: ignore[attr-defined]
        sent = list(app.bot.sent)
        await bridge.stop()
        return sent

    sent = asyncio.run(scenario())
    assert len(sent) == 1
    markup = sent[0]["reply_markup"]
    assert isinstance(markup, _FakeInlineKeyboardMarkup)
    assert len(markup.rows) == 1
    buttons = markup.rows[0]
    assert [b.callback_data for b in buttons] == ["approve:t-7", "reject:t-7"]
    assert "Approve shell command?" in sent[0]["text"]


def test_telegram_edit_throttle_collapses_rapid_updates(fake_telegram: None) -> None:
    """Five rapid edits to the same message must produce exactly one API call."""
    from bernstein.core.chat.drivers.telegram import TelegramBridge

    bridge = TelegramBridge(token="dummy")

    async def scenario() -> list[dict[str, Any]]:
        await bridge.start()
        app = bridge._app  # type: ignore[attr-defined]
        # Burst five updates back-to-back without yielding long enough to
        # release the 500ms throttle.
        for i in range(5):
            await bridge.edit_message("42", "100", f"tick {i}")
        # Cancel any pending deferred flush so stop() doesn't race.
        edited = list(app.bot.edited)
        await bridge.stop()
        return edited

    edits = asyncio.run(scenario())
    assert len(edits) == 1, f"expected a single throttled edit, got {edits}"
    assert edits[0]["text"] == "tick 0"


# ---------------------------------------------------------------------------
# Helpers for fake Telegram Updates
# ---------------------------------------------------------------------------


def _fake_update(*, text: str, chat_id: int, user_id: int, message_id: int) -> Any:
    chat = types.SimpleNamespace(id=chat_id)
    user = types.SimpleNamespace(id=user_id)
    message = types.SimpleNamespace(
        text=text,
        chat=chat,
        from_user=user,
        message_id=message_id,
    )
    return types.SimpleNamespace(message=message, edited_message=None, callback_query=None)


def _fake_callback_update(*, data: str, chat_id: int) -> Any:
    chat = types.SimpleNamespace(id=chat_id)
    message = types.SimpleNamespace(chat=chat)

    async def _answer() -> None: # NOSONAR — async-signature required by protocol / fixture
        return None

    query = types.SimpleNamespace(data=data, message=message, answer=_answer)
    return types.SimpleNamespace(callback_query=query, message=None, edited_message=None)
