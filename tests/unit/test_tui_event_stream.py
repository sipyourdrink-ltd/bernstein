"""Tests for the TUI SSE thread consumer (issue #2297).

The consumer replaces the TUI poll loop: it reads the server's
``/events`` SSE stream, tracks ``Last-Event-ID`` (the journal index), and
on a dropped-and-reconnected stream resumes from the last id without
missing or duplicating journal entries (AC5). A polling fallback stays
behind the ``BERNSTEIN_TUI_STREAM`` flag for constrained terminals.
"""

from __future__ import annotations

import pytest

from bernstein.tui.event_stream import (
    JournalStreamConsumer,
    parse_sse_frames,
    stream_enabled,
)


def _frame(sse_id: str, event: str, data: str) -> str:
    return f"id: {sse_id}\nevent: {event}\ndata: {data}\n\n"


def test_parse_sse_frames_extracts_id_event_data() -> None:
    raw = _frame("0", "thread.step", '{"event_hash":"aa"}') + _frame("1", "thread.step", '{"event_hash":"bb"}')

    frames = list(parse_sse_frames(raw))

    assert [f.sse_id for f in frames] == ["0", "1"]
    assert [f.event for f in frames] == ["thread.step", "thread.step"]
    assert frames[0].data == {"event_hash": "aa"}


def test_consumer_tracks_last_event_id() -> None:
    consumer = JournalStreamConsumer()
    raw = _frame("0", "thread.step", "{}") + _frame("3", "thread.step", "{}")

    delivered = list(consumer.consume(raw))

    assert [f.sse_id for f in delivered] == ["0", "3"]
    assert consumer.last_event_id == "3"


def test_consumer_resumes_without_gap_or_duplicate() -> None:
    """A reconnect replays from Last-Event-ID with no gap and no dupe (AC5)."""
    consumer = JournalStreamConsumer()
    # first connection delivers 0,1,2
    list(consumer.consume(_frame("0", "s", "{}") + _frame("1", "s", "{}") + _frame("2", "s", "{}")))
    assert consumer.last_event_id == "2"

    # server, on reconnect with Last-Event-ID=2, re-sends 1,2 (overlap) then 3,4.
    # the consumer must drop the already-seen 1,2 and deliver only 3,4.
    reconnect = _frame("1", "s", "{}") + _frame("2", "s", "{}") + _frame("3", "s", "{}") + _frame("4", "s", "{}")
    delivered = list(consumer.consume(reconnect))

    assert [f.sse_id for f in delivered] == ["3", "4"]
    assert consumer.last_event_id == "4"


def test_consumer_ignores_heartbeat_without_id() -> None:
    consumer = JournalStreamConsumer()
    raw = "event: heartbeat\ndata: {}\n\n" + _frame("0", "s", "{}")

    delivered = list(consumer.consume(raw))

    assert [f.sse_id for f in delivered] == ["0"]
    assert consumer.last_event_id == "0"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("off", False)],
)
def test_stream_enabled_reads_flag(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    if value:
        monkeypatch.setenv("BERNSTEIN_TUI_STREAM", value)
    else:
        monkeypatch.delenv("BERNSTEIN_TUI_STREAM", raising=False)

    assert stream_enabled() is expected


def test_last_event_id_header_for_reconnect() -> None:
    consumer = JournalStreamConsumer()
    list(consumer.consume(_frame("5", "s", "{}")))

    assert consumer.reconnect_headers() == {"Last-Event-ID": "5"}


def test_app_uses_stream_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TUI selects the SSE hot path when the flag is on (AC1)."""
    from bernstein.tui.app import BernsteinApp

    monkeypatch.setenv("BERNSTEIN_TUI_STREAM", "1")
    app = BernsteinApp()
    assert app._use_stream is True


def test_app_falls_back_to_polling_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the flag the TUI keeps the polling fallback (constrained terminals)."""
    from bernstein.tui.app import BernsteinApp

    monkeypatch.delenv("BERNSTEIN_TUI_STREAM", raising=False)
    app = BernsteinApp()
    assert app._use_stream is False
