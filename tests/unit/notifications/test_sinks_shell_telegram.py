"""Tests for the shell + telegram sinks (subprocess and bridge stubs)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks.shell import ShellSink
from bernstein.core.notifications.sinks.telegram import TelegramSink


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="ev-shell",
        kind=NotificationEventKind.POST_TASK,
        title="title",
        body="body",
        severity="info",
        timestamp=1.0,
    )


def _writer_script(out_path: Any) -> str:
    """Return a Python one-liner that copies stdin into ``out_path``."""
    return f"import sys, pathlib; pathlib.Path({str(out_path)!r}).write_text(sys.stdin.read())"


@pytest.mark.asyncio
async def test_shell_sink_invokes_command_and_passes_payload(tmp_path: Any) -> None:
    out_file = tmp_path / "out.json"
    sink = ShellSink(
        {
            "id": "shell-1",
            "kind": "shell",
            "command": [sys.executable, "-c", _writer_script(out_file)],
            "timeout_s": 10,
        },
    )
    await sink.deliver(_event())
    assert out_file.exists()
    written = out_file.read_text(encoding="utf-8")
    assert "ev-shell" in written
    assert "post_task" in written


@pytest.mark.asyncio
async def test_shell_sink_command_not_found_is_permanent() -> None:
    sink = ShellSink(
        {
            "id": "shell-x",
            "kind": "shell",
            "command": ["/nonexistent/binary/abc"],
        },
    )
    with pytest.raises(NotificationPermanentError, match="not found"):
        await sink.deliver(_event())


@pytest.mark.asyncio
async def test_shell_sink_non_zero_exit_is_transient_by_default() -> None:
    sink = ShellSink(
        {
            "id": "shell-fail",
            "kind": "shell",
            "command": [sys.executable, "-c", "import sys; sys.exit(2)"],
        },
    )
    with pytest.raises(NotificationDeliveryError, match="exited 2"):
        await sink.deliver(_event())


@pytest.mark.asyncio
async def test_shell_sink_non_zero_can_be_marked_permanent() -> None:
    sink = ShellSink(
        {
            "id": "shell-fail",
            "kind": "shell",
            "command": [sys.executable, "-c", "import sys; sys.exit(7)"],
            "non_zero_exit_is_permanent": True,
        },
    )
    with pytest.raises(NotificationPermanentError, match="exited 7"):
        await sink.deliver(_event())


def test_shell_sink_requires_non_empty_command() -> None:
    with pytest.raises(NotificationPermanentError, match="command"):
        ShellSink({"id": "x", "kind": "shell", "command": []})


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class _FakeBridge:
    """Stand-in for TelegramBridge.send_message."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.stopped = False
        self._fail = fail

    async def send_message(self, thread_id: str, text: str) -> str:
        if self._fail:
            raise RuntimeError("boom")
        self.sent.append((thread_id, text))
        return "msg-1"

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_telegram_sink_uses_supplied_bridge() -> None:
    bridge = _FakeBridge()
    sink = TelegramSink({"id": "tg", "kind": "telegram", "chat_id": "-100", "bridge": bridge})
    await sink.deliver(_event())
    assert bridge.sent == [("-100", "title\n\nbody")]
    # close() must NOT stop the externally-owned bridge.
    await sink.close()
    assert bridge.stopped is False


@pytest.mark.asyncio
async def test_telegram_sink_translates_runtime_error_to_transient() -> None:
    bridge = _FakeBridge(fail=True)
    sink = TelegramSink({"id": "tg", "kind": "telegram", "chat_id": "-100", "bridge": bridge})
    with pytest.raises(NotificationDeliveryError):
        await sink.deliver(_event())


def test_telegram_sink_requires_chat_id() -> None:
    with pytest.raises(NotificationPermanentError, match="chat_id"):
        TelegramSink({"id": "tg", "kind": "telegram", "bridge": _FakeBridge()})


def test_telegram_sink_requires_transport() -> None:
    with pytest.raises(NotificationPermanentError, match="bridge.*token"):
        TelegramSink({"id": "tg", "kind": "telegram", "chat_id": "-100"})
