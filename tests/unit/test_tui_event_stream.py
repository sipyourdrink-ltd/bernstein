"""Tests for the TUI SSE thread consumer (issue #2297).

The consumer replaces the TUI poll loop: it reads the server's
``/events`` SSE stream, tracks ``Last-Event-ID`` (the journal index), and
on a dropped-and-reconnected stream resumes from the last id without
missing or duplicating journal entries (AC5). A polling fallback stays
behind the ``BERNSTEIN_TUI_STREAM`` flag for constrained terminals.

Network chunks never align with SSE frames, so the incremental tests feed
every split and seeded multi-split partitions and require the same
delivered frames and final cursor as the whole text. App tests drive the
real ``_consume_thread_stream`` loop through a real ``httpx.AsyncClient``
on an injected ``MockTransport``, and one end-to-end test runs it against the
real ``/events/thread`` route in process; nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from bernstein.tui import app as app_module
from bernstein.tui.event_stream import (
    MAX_FRAME_CHARS,
    JournalStreamConsumer,
    SSEDecoder,
    SSEFrameTooLarge,
    StreamFrame,
    parse_sse_frames,
    stream_enabled,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# Lossless incremental streaming: network chunks never align with SSE frames.
# ---------------------------------------------------------------------------

#: Representative wire text: comment keep-alive, the server's id-less
#: heartbeat, LF and CRLF journal frames, multi-line data, unicode, two
#: frames back to back, a replayed duplicate and an id-only frame.
_REPRESENTATIVE = (
    ": keep-alive\n\n"
    'event: heartbeat\ndata: {"connected": true}\n\n'
    'id: 0\nevent: thread.step\ndata: {"journal_index":0,"event_hash":"aa"}\n\n'
    'id: 1\r\nevent: thread.step\r\ndata: {"journal_index":1,\r\ndata:  "note":"héllo ✓ 日本 🚀"}\r\n\r\n'
    "id: 2\revent: thread.step\rdata: {}\r\r"
    'id:3\nevent:thread.step\ndata:{"journal_index":3}\n\n'
    'id: 2\nevent: thread.step\ndata: {"journal_index":2}\n\n'
    "id: 9\nevent: thread.step\n\n"
    'id: 4\nevent: thread.step\ndata: {"journal_index":4}\n\n'
)
_EXPECTED_IDS = ["0", "1", "2", "3", "4"]


def _delivered(consumer: JournalStreamConsumer, chunks: Sequence[str]) -> list[StreamFrame]:
    out: list[StreamFrame] = []
    for chunk in chunks:
        out.extend(consumer.consume(chunk))
    return out


def _whole() -> tuple[list[StreamFrame], str]:
    consumer = JournalStreamConsumer()
    frames = _delivered(consumer, [_REPRESENTATIVE])
    return frames, consumer.last_event_id


def test_whole_representative_stream_delivers_expected_frames() -> None:
    frames, cursor = _whole()

    assert [f.sse_id for f in frames] == _EXPECTED_IDS
    assert frames[1].data == {"journal_index": 1, "note": "héllo ✓ 日本 🚀"}
    assert frames[2].data == {}
    assert cursor == "4"


def test_split_right_after_id_line_does_not_advance_cursor() -> None:
    """Regression: a partially received id must not commit an empty frame."""
    consumer = JournalStreamConsumer()
    raw = 'id: 7\nevent: task\ndata: {"ok": true}\n\n'

    first = consumer.consume(raw[: len("id: 7\n")])

    assert first == []
    assert consumer.last_event_id == ""
    assert consumer.reconnect_headers() == {}
    second = consumer.consume(raw[len("id: 7\n") :])
    assert [(f.sse_id, f.event, f.data) for f in second] == [("7", "task", {"ok": True})]
    assert consumer.last_event_id == "7"


def test_every_single_split_matches_whole_input() -> None:
    expected, expected_cursor = _whole()
    for cut in range(len(_REPRESENTATIVE) + 1):
        consumer = JournalStreamConsumer()
        got = _delivered(consumer, [_REPRESENTATIVE[:cut], _REPRESENTATIVE[cut:]])
        assert got == expected, f"split at {cut}"
        assert consumer.last_event_id == expected_cursor, f"split at {cut}"
        assert consumer.end_stream() == 0


@pytest.mark.parametrize("seed", range(40))
def test_seeded_multi_split_partitions_match_whole_input(seed: int) -> None:
    expected, expected_cursor = _whole()
    rng = random.Random(seed)
    for _ in range(25):
        cuts = sorted(rng.sample(range(1, len(_REPRESENTATIVE)), rng.randint(1, 30)))
        bounds = [0, *cuts, len(_REPRESENTATIVE)]
        chunks = [_REPRESENTATIVE[a:b] for a, b in itertools.pairwise(bounds)]
        consumer = JournalStreamConsumer()
        assert _delivered(consumer, chunks) == expected, cuts
        assert consumer.last_event_id == expected_cursor


def test_one_character_chunks_match_whole_input() -> None:
    expected, _ = _whole()
    assert _delivered(JournalStreamConsumer(), list(_REPRESENTATIVE)) == expected


def test_crlf_split_between_cr_and_lf() -> None:
    consumer = JournalStreamConsumer()
    chunks = ["id: 5\r", "\nevent: s\r", "\ndata: {}\r", "\n\r", "\n"]

    got = _delivered(consumer, chunks)

    assert [f.sse_id for f in got] == ["5"]
    assert got[0].event == "s"


def test_lone_cr_chunk_then_other_text_is_one_line_end() -> None:
    consumer = JournalStreamConsumer()
    # CR-only line endings: the CR at a chunk end must not swallow content,
    # and nothing is delivered before the blank line arrives.
    for chunk in ["id: 6\r", "event: s\r", 'data: {"n":6}\r']:
        assert consumer.consume(chunk) == []
    assert consumer.last_event_id == ""

    assert consumer.consume("\r") == [StreamFrame("6", "s", {"n": 6})]


def test_two_frames_share_chunks_and_whitespace_only_terminator() -> None:
    consumer = JournalStreamConsumer()
    a = _frame("0", "s", '{"n":0}')
    b = _frame("1", "s", '{"n":1}')
    whole = a + b

    assert consumer.consume(whole[:-1]) == [StreamFrame("0", "s", {"n": 0})]
    assert consumer.last_event_id == "0"
    # The final blank line arrives alone as a whitespace-only chunk.
    assert consumer.consume("\n") == [StreamFrame("1", "s", {"n": 1})]
    assert consumer.last_event_id == "1"


def test_comments_and_multiple_data_lines() -> None:
    consumer = JournalStreamConsumer()
    raw = ': ping\nid: 1\n: mid-frame comment\nevent: s\ndata: {"a":\ndata: 1}\nretry: 10\nweird: x\n\n'

    got = consumer.consume(raw)

    assert got == [StreamFrame("1", "s", {"a": 1})]


def test_leading_bom_is_ignored_once_per_connection() -> None:
    consumer = JournalStreamConsumer()
    assert [f.sse_id for f in consumer.consume("﻿" + _frame("1", "s", "{}"))] == ["1"]


def test_disconnect_mid_id_discards_partial_and_keeps_cursor() -> None:
    consumer = JournalStreamConsumer()
    consumer.consume(_frame("3", "s", "{}") + "id: 4")

    assert consumer.last_event_id == "3"
    assert consumer.end_stream() == len("id: 4")
    # New connection: the stale "id: 4" must not join the new text.
    got = consumer.consume('event: s\ndata: {"fresh":true}\n\n' + _frame("4", "s", '{"n":4}'))
    assert got == [StreamFrame("4", "s", {"n": 4})]
    assert consumer.reconnect_headers() == {"Last-Event-ID": "4"}


def test_disconnect_mid_json_never_commits_truncated_payload() -> None:
    consumer = JournalStreamConsumer()
    consumer.consume(_frame("1", "s", "{}") + 'id: 2\nevent: s\ndata: {"half": ')

    discarded = consumer.end_stream()

    assert discarded > 0
    assert consumer.last_event_id == "1"
    # Reconnect replays 1 (overlap) and the full 2; payload must not be the stale prefix.
    got = consumer.consume(_frame("1", "s", "{}") + _frame("2", "s", '{"half": false}'))
    assert got == [StreamFrame("2", "s", {"half": False})]


def test_duplicate_and_out_of_order_ids_are_dropped() -> None:
    consumer = JournalStreamConsumer()
    raw = "".join(_frame(i, "s", "{}") for i in ["3", "3", "5", "4", "5", "6"])

    assert [f.sse_id for f in consumer.consume(raw)] == ["3", "5", "6"]
    assert consumer.last_event_id == "6"


@pytest.mark.parametrize("bad_id", ["abc", "-1", "+2", "7.0", "0x7", "٣", "1 2"])
def test_non_journal_ids_are_rejected_without_cursor_move(bad_id: str) -> None:
    consumer = JournalStreamConsumer()
    consumer.consume(_frame("2", "s", "{}"))

    got = consumer.consume(_frame(bad_id, "s", "{}") + _frame("3", "s", "{}"))

    assert [f.sse_id for f in got] == ["3"]
    assert consumer.reconnect_headers() == {"Last-Event-ID": "3"}


def test_id_beyond_int_digit_limit_is_rejected_not_raised() -> None:
    consumer = JournalStreamConsumer()
    raw = _frame("5", "s", "{}") + _frame("9" * 5000, "s", "{}") + _frame("6", "s", "{}")

    assert [f.sse_id for f in consumer.consume(raw)] == ["5", "6"]
    assert consumer.last_event_id == "6"


def test_empty_id_and_id_only_frames_are_control_not_journal_events() -> None:
    consumer = JournalStreamConsumer()

    got = consumer.consume("id:\nevent: s\ndata: {}\n\n" + "id: 8\n\n" + "id: 9\nevent: s\n\n")

    assert got == []
    assert consumer.last_event_id == ""


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ('{"ok": 1}', {"ok": 1}),
        ("", {}),
        ("{not json", {"raw": "{not json"}),
        ("5", {"raw": "5"}),
        ('"text"', {"raw": '"text"'}),
        ("[1, 2]", {"raw": "[1, 2]"}),
        ("null", {"raw": "null"}),
        ("[" * 100_000 + "]" * 100_000, {"raw": "[" * 100_000 + "]" * 100_000}),
    ],
)
def test_complete_frame_data_follows_object_contract(data: str, expected: dict[str, Any]) -> None:
    """Non-object JSON is surfaced as ``{"raw": ...}``, only once the frame is complete."""
    consumer = JournalStreamConsumer()
    raw = _frame("1", "s", data)

    assert consumer.consume(raw[:-1]) == []
    got = consumer.consume(raw[-1:])

    assert got == [StreamFrame("1", "s", expected)]
    assert consumer.last_event_id == "1"


def test_parse_sse_frames_does_not_emit_unterminated_block() -> None:
    """Narrowed contract: an unterminated trailing block is in flight, not a frame."""
    raw = _frame("0", "s", "{}") + 'id: 1\nevent: s\ndata: {"partial"'

    assert [f.sse_id for f in parse_sse_frames(raw)] == ["0"]


def test_parse_sse_frames_handles_crlf_and_multiline_data() -> None:
    raw = 'id: 1\r\nevent: s\r\ndata: {"a":\r\ndata: 2}\r\n\r\n'

    assert list(parse_sse_frames(raw)) == [StreamFrame("1", "s", {"a": 2})]


def test_parse_sse_frames_is_unbounded_for_in_memory_input() -> None:
    big = '{"blob": "' + "x" * (MAX_FRAME_CHARS + 10) + '"}'

    frames = list(parse_sse_frames(_frame("1", "s", big)))

    assert frames[0].data["blob"] == "x" * (MAX_FRAME_CHARS + 10)


def test_oversized_frame_is_bounded_and_keeps_earlier_frames() -> None:
    consumer = JournalStreamConsumer(max_frame_chars=64)
    chunk = _frame("1", "s", '{"n":1}') + "id: 2\nevent: s\ndata: " + "SECRET" * 40

    with pytest.raises(SSEFrameTooLarge) as info:
        consumer.consume(chunk)

    assert [f.sse_id for f in info.value.delivered] == ["1"]
    assert consumer.last_event_id == "1"
    assert consumer.pending_chars == 0
    assert "SECRET" not in str(info.value)  # never echoes payload text
    # The connection is poisoned until it is ended; the tail is not re-parsed.
    with pytest.raises(SSEFrameTooLarge):
        consumer.consume("xx\nid: 99\nevent: s\ndata: {}\n\n")
    assert consumer.last_event_id == "1"
    consumer.end_stream()
    assert [f.sse_id for f in consumer.consume(_frame("2", "s", "{}"))] == ["2"]


def test_oversized_unterminated_line_is_bounded_across_chunks() -> None:
    decoder = SSEDecoder(max_frame_chars=100)
    decoder.feed("data: ")
    with pytest.raises(SSEFrameTooLarge):
        for _ in range(50):
            decoder.feed("y" * 10)
    assert decoder.pending_chars == 0


#: Single complete frames for the size-bound controls. Their size includes
#: comments, unknown fields and field-name prefixes, not just data values.
_BOUND_FRAMES = {
    # Shape of the confirmed probe: whole delivered vs split rejected before the fix.
    "probe": 'id: 1\nevent: s\ndata: {"k":"xxxxxxxxx"}\n\n',
    "prefixes": (
        ": keep-alive comment\nid: 1\nevent: thread.step\nretry: 1000\n"
        'x-unknown-field: ignored value\ndata: {"a":\ndata: 1}\n\n'
    ),
    "crlf": ': c\r\nid: 1\r\nevent: s\r\ndata: {"note":"日本"}\r\n\r\n',
    "cr": ": c\rid: 1\revent: s\rdata: {}\r\r",
}


def _frame_size(text: str) -> int:
    """Independent oracle: chars of the frame's lines, one per line ending."""
    lines = re.split(r"\r\n|\r|\n", text)
    return sum(len(line) + 1 for line in lines[: lines.index("")])


def _bound_outcome(limit: int, chunks: Sequence[str]) -> tuple[str, list[str], str]:
    consumer = JournalStreamConsumer(max_frame_chars=limit)
    ids: list[str] = []
    try:
        for chunk in chunks:
            ids += [f.sse_id for f in consumer.consume(chunk)]
    except SSEFrameTooLarge as exc:
        ids += [f.sse_id for f in exc.delivered]
        return ("rejected", ids, consumer.last_event_id)
    return ("accepted", ids, consumer.last_event_id)


@pytest.mark.parametrize("name", sorted(_BOUND_FRAMES))
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_frame_size_bound_is_partition_independent(name: str, offset: int) -> None:
    """Below, at and above the bound: whole text and every split agree."""
    text = _BOUND_FRAMES[name]
    limit = _frame_size(text) + offset
    expected = ("accepted", ["1"], "1") if offset >= 0 else ("rejected", [], "")

    assert _bound_outcome(limit, [text]) == expected
    assert _bound_outcome(limit, list(text)) == expected
    for cut in range(len(text) + 1):
        assert _bound_outcome(limit, [text[:cut], text[cut:]]) == expected, f"split at {cut}"


def test_probe_frame_size_counts_field_prefixes() -> None:
    text = _BOUND_FRAMES["probe"]

    assert _frame_size(text) == len("id: 1\n") + len("event: s\n") + len('data: {"k":"xxxxxxxxx"}\n')
    assert _bound_outcome(20, [text]) == ("rejected", [], "")
    assert _bound_outcome(20, [text[:-2], text[-2:]]) == ("rejected", [], "")


def test_comment_lines_count_until_a_blank_line_resets_the_frame() -> None:
    limit = 64
    terminated = ": ping\n\n" * 100 + _frame("1", "s", "{}")
    assert _bound_outcome(limit, [terminated]) == ("accepted", ["1"], "1")
    assert _bound_outcome(limit, list(terminated)) == ("accepted", ["1"], "1")

    unterminated = ": ping\n" * 100
    assert _bound_outcome(limit, [unterminated])[0] == "rejected"
    assert _bound_outcome(limit, list(unterminated))[0] == "rejected"


def test_repeated_id_and_event_lines_count_toward_the_bound() -> None:
    """Overwritten ``id``/``event`` lines are counted, so retained text stays bounded."""
    limit = 100
    text = "id: " + "1" * 60 + "\nevent: " + "e" * 60 + '\ndata: {"k":"' + "v" * 20 + '"}\n\n'

    assert _bound_outcome(limit, [text])[0] == "rejected"
    assert _bound_outcome(limit, list(text))[0] == "rejected"


def test_pending_never_exceeds_bound_while_feeding() -> None:
    limit = 50
    decoder = SSEDecoder(max_frame_chars=limit)
    text = _frame("1", "s", "{}") + ": comment\nid: 2\nevent: s\ndata: " + "q" * 200
    with pytest.raises(SSEFrameTooLarge):
        for char in text:
            decoder.feed(char)
            assert decoder.pending_chars <= limit
    assert decoder.pending_chars == 0


# ---------------------------------------------------------------------------
# App integration: real consumer, real httpx client, injected transport.
# ---------------------------------------------------------------------------

_SSE_HEADERS = {"content-type": "text/event-stream; charset=utf-8"}


class _Stop(BaseException):
    """Ends the reconnect loop from the fake sleep (not an ``Exception``)."""


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes], *, fail: bool = False, hang: asyncio.Event | None = None) -> None:
        self._chunks = chunks
        self._fail = fail
        self._hang = hang
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._hang is not None:
            self._hang.set()
            await asyncio.Event().wait()
        if self._fail:
            raise httpx.ReadError("connection dropped")

    async def aclose(self) -> None:
        self.closed = True


class _Harness:
    """Scripted ``/events/thread`` server: one response per connection attempt."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, connections: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.refreshes = 0
        self.intervals: list[float] = []
        self._connections = connections
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._connections[len(self.requests) - 1]

        def client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        async def fake_sleep(delay: float) -> None:
            self.sleeps.append(delay)
            if len(self.sleeps) >= len(self._connections):
                raise _Stop

        monkeypatch.setattr(app_module.httpx, "AsyncClient", client_factory)
        monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
        monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
        monkeypatch.setenv(app_module.STREAM_RUN_ENV_VAR, "run-1")
        self.app = app_module.BernsteinApp(poll_interval=0.25)

        def count_refresh() -> None:
            self.refreshes += 1

        def record_interval(interval: float, callback: Any) -> None:
            self.intervals.append(interval)

        monkeypatch.setattr(self.app, "action_refresh", count_refresh)
        monkeypatch.setattr(self.app, "set_interval", record_interval)

    async def run(self) -> None:
        with contextlib.suppress(_Stop):
            await self.app._consume_thread_stream()


def _sse(chunks: Sequence[bytes], **kwargs: Any) -> httpx.Response:
    return httpx.Response(200, headers=_SSE_HEADERS, stream=_ChunkStream(chunks, **kwargs))


@pytest.mark.asyncio
async def test_app_delivers_byte_split_frames_without_partial_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = (
        'event: heartbeat\ndata: {"connected": true}\n\n'
        + _frame("0", "thread.step", '{"note":"日本 🚀"}')
        + _frame("1", "thread.step", "{}")
    ).encode()
    snowman = wire.index("日".encode()) + 1  # split inside a multi-byte character
    after_id = wire.index(b"id: 0\n") + len(b"id: 0\n")
    end_0 = wire.index(b"id: 1")
    chunks = [wire[:after_id], wire[after_id:snowman], wire[snowman : end_0 - 1], wire[end_0 - 1 : -1], b"\n"]
    harness = _Harness(monkeypatch, [_sse(chunks)])
    seen: list[StreamFrame] = []
    real_consume = harness.app._stream_consumer.consume

    def spy(raw: str) -> list[StreamFrame]:
        got = real_consume(raw)
        seen.extend(got)
        return got

    monkeypatch.setattr(harness.app._stream_consumer, "consume", spy)

    await harness.run()

    assert [(f.sse_id, f.data) for f in seen] == [("0", {"note": "日本 🚀"}), ("1", {})]
    # chunk 4 completes frame 0, chunk 5 (a lone "\n") completes frame 1.
    assert harness.refreshes == 2
    assert harness.app._stream_consumer.last_event_id == "1"


@pytest.mark.asyncio
async def test_app_whitespace_only_chunk_terminates_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, [_sse([b'id: 1\nevent: s\ndata: {"n":1}\n', b"\n"])])
    per_chunk: list[tuple[str, list[StreamFrame], int]] = []
    real_consume = harness.app._stream_consumer.consume

    def spy(raw: str) -> list[StreamFrame]:
        got = real_consume(raw)
        per_chunk.append((raw, got, harness.refreshes))
        return got

    monkeypatch.setattr(harness.app._stream_consumer, "consume", spy)

    await harness.run()

    # The first chunk lacks its blank line: no frame, no refresh. The lone
    # "\n" chunk must reach the consumer and complete the frame.
    assert per_chunk == [
        ('id: 1\nevent: s\ndata: {"n":1}\n', [], 0),
        ("\n", [StreamFrame("1", "s", {"n": 1})], 0),
    ]
    assert harness.refreshes == 1
    assert harness.app._stream_consumer.last_event_id == "1"


@pytest.mark.asyncio
async def test_app_reconnects_after_mid_json_drop_without_stale_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    first = (_frame("0", "s", "{}") + 'id: 1\nevent: s\ndata: {"half"').encode()
    second = (_frame("0", "s", "{}") + _frame("1", "s", '{"n":1}') + _frame("2", "s", "{}")).encode()
    harness = _Harness(monkeypatch, [_sse([first], fail=True), _sse([second])])
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "tkn")

    await harness.run()

    assert harness.refreshes == 2  # one per connection that delivered a complete frame
    assert harness.app._stream_consumer.last_event_id == "2"
    assert "last-event-id" not in harness.requests[0].headers
    assert harness.requests[1].headers["last-event-id"] == "0"
    assert all(r.headers["authorization"] == "Bearer tkn" for r in harness.requests)
    assert all(r.url.path == "/events/thread" and r.url.params["run"] == "run-1" for r in harness.requests)
    assert harness.app._stream_consumer.pending_chars == 0


@pytest.mark.asyncio
async def test_app_waits_between_connections_on_eof_and_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(
        monkeypatch,
        [_sse([]), httpx.Response(429, json={"detail": "slow down"}), _sse([_frame("0", "s", "{}").encode()])],
    )

    await harness.run()

    assert harness.sleeps == [0.25, 0.25, 0.25]  # no immediate reconnect on clean EOF
    assert len(harness.requests) == 3
    assert harness.refreshes == 1  # the 429 body is never parsed as events


@pytest.mark.asyncio
async def test_app_falls_back_to_polling_on_oversized_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch, [_sse([_frame("0", "s", "{}").encode(), b"data: " + b"x" * 64])])
    harness.app._stream_consumer = JournalStreamConsumer(max_frame_chars=32)

    await harness.run()  # returns on its own: no reconnect loop

    assert harness.sleeps == []
    assert len(harness.requests) == 1
    assert harness.intervals == [0.25]
    assert harness.refreshes == 2  # frame 0, then the fallback's immediate resync
    assert harness.app._stream_consumer.last_event_id == "0"


@pytest.mark.asyncio
async def test_app_cancellation_closes_stream_and_discards_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    hang = asyncio.Event()
    stream = _ChunkStream([b"id: 1\nevent: s\ndata: {"], hang=hang)
    harness = _Harness(monkeypatch, [httpx.Response(200, headers=_SSE_HEADERS, stream=stream)])

    task = asyncio.ensure_future(harness.run())
    await asyncio.wait_for(hang.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed
    assert harness.app._stream_consumer.pending_chars == 0
    assert harness.app._stream_consumer.last_event_id == ""
    assert harness.refreshes == 0
    assert harness.sleeps == []


@pytest.mark.asyncio
async def test_app_follows_the_owner_run_and_restarts_the_cursor_on_a_new_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bernstein.core.orchestration.run_closure_owner import write_spawner_run_owner

    monkeypatch.chdir(tmp_path)
    sdd = tmp_path / ".sdd"
    write_spawner_run_owner(sdd_dir=sdd, run_id="run-a", journal_head="h", journal_event_count=1)
    harness = _Harness(
        monkeypatch,
        [_sse([_frame("5", "thread.step", "{}").encode()]), _sse([_frame("0", "thread.step", "{}").encode()])],
    )
    monkeypatch.delenv(app_module.STREAM_RUN_ENV_VAR)
    first_consumer = harness.app._stream_consumer
    real_consume = first_consumer.consume

    def switch_run_after_first_frame(raw: str) -> list[StreamFrame]:
        got = real_consume(raw)
        if got:
            write_spawner_run_owner(sdd_dir=sdd, run_id="run-b", journal_head="h", journal_event_count=1)
        return got

    monkeypatch.setattr(first_consumer, "consume", switch_run_after_first_frame)

    await harness.run()

    assert [r.url.params["run"] for r in harness.requests] == ["run-a", "run-b"]
    # run-b's journal starts at 0: the old cursor (5) must not be sent or applied.
    assert "last-event-id" not in harness.requests[1].headers
    assert harness.app._stream_consumer is not first_consumer
    assert harness.app._stream_consumer.last_event_id == "0"
    assert harness.refreshes == 2


@pytest.mark.asyncio
async def test_app_refreshes_on_the_poll_interval_while_no_run_is_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .sdd/runtime/spawner-run-owner.json
    harness = _Harness(monkeypatch, [_sse([]), _sse([]), _sse([])])
    monkeypatch.delenv(app_module.STREAM_RUN_ENV_VAR)

    await harness.run()

    assert harness.requests == []
    assert harness.refreshes == 3
    assert harness.sleeps == [0.25, 0.25, 0.25]


class _ASGIStreamTransport(httpx.AsyncBaseTransport):
    """Bridge an httpx client to an in-process ASGI app, streaming the body.

    ``httpx.ASGITransport`` waits for the whole response body, which an SSE
    stream never finishes. This bridge hands each body chunk to the client
    as the app sends it, and tells the app the client disconnected when the
    response is closed. No network is involved.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body: asyncio.Queue[bytes | None] = asyncio.Queue()
        started: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        disconnect = asyncio.Event()
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    body.put_nowait(message["body"])
                if not message.get("more_body", False):
                    body.put_nowait(None)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": ("127.0.0.1", 40001),
            "server": (request.url.host, request.url.port or 80),
        }
        task = asyncio.ensure_future(self._app(scope, receive, send))

        def finished(done: asyncio.Future[None]) -> None:
            body.put_nowait(None)
            if not started.done():
                error = None if done.cancelled() else done.exception()
                started.set_exception(error or RuntimeError("app sent no response"))

        task.add_done_callback(finished)
        start = await started
        return httpx.Response(
            start["status"], headers=start.get("headers", []), stream=_QueueStream(body, disconnect, task)
        )


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, body: asyncio.Queue[bytes | None], disconnect: asyncio.Event, task: Any) -> None:
        self._body = body
        self._disconnect = disconnect
        self._task = task

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self._body.get()) is not None:
            yield chunk

    async def aclose(self) -> None:
        self._disconnect.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._task, timeout=5)


@pytest.mark.asyncio
async def test_streamed_tui_refreshes_from_the_live_journal_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end in process: journal rows -> /events/thread -> TUI refresh.

    Replaces the strict xfail that recorded the id-less ``/events`` gap. The
    real ``create_app`` route projects a real ``EventJournal``; the TUI's real
    reconnect loop reads it through a real ``httpx.AsyncClient``.
    """
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.routes import status_events
    from bernstein.core.server import create_app

    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    journal = EventJournal("run-live", tmp_path / ".sdd")
    journal.record("task_claimed", task_id="t-1", agent_id="agent-a")
    journal.record("task_claimed", task_id="t-2", agent_id="agent-a")
    server = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    monkeypatch.setattr(status_events, "THREAD_STREAM_POLL_S", 0.01)
    monkeypatch.setenv(app_module.STREAM_RUN_ENV_VAR, "run-live")
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    class _Recording(_ASGIStreamTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return await super().handle_async_request(request)

    monkeypatch.setattr(app_module.httpx, "AsyncClient", lambda **kw: real_client(transport=_Recording(server), **kw))
    tui = app_module.BernsteinApp(poll_interval=0.01)
    refreshes: list[str] = []
    monkeypatch.setattr(tui, "action_refresh", lambda: refreshes.append(tui._stream_consumer.last_event_id))
    consumer = tui._stream_consumer
    delivered: list[StreamFrame] = []
    errors: list[BaseException] = []
    real_consume = consumer.consume

    def consume_spy(raw: str) -> list[StreamFrame]:
        try:
            got = real_consume(raw)
        except Exception as exc:
            errors.append(exc)
            raise
        delivered.extend(got)
        if [f.sse_id for f in delivered] == ["0", "1"]:
            journal.record("task_completed", task_id="t-1", agent_id="agent-a")  # arrives live
        if consumer.last_event_id == "2":
            raise _Stop
        return got

    monkeypatch.setattr(consumer, "consume", consume_spy)

    try:
        with contextlib.suppress(_Stop):
            await asyncio.wait_for(tui._consume_thread_stream(), timeout=10)
    except TimeoutError:
        pytest.fail(f"no journal frame reached the TUI: errors={errors!r}, delivered={delivered!r}")

    assert errors == []
    assert [(f.sse_id, f.data["journal_event"]) for f in delivered] == [
        ("0", "task_claimed"),
        ("1", "task_claimed"),
        ("2", "task_completed"),
    ]
    assert refreshes and refreshes[-1] in {"0", "1"}  # refreshed on delivery; _Stop fired before frame 2's refresh
    assert len(requests) == 1
    assert requests[0].url.path == "/events/thread"
    assert requests[0].url.params["run"] == "run-live"
