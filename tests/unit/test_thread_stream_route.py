"""Tests for ``GET /events/thread``: one run journal served as SSE.

The route serves the existing journal projection (``project_journal`` and
``ThreadStreamEvent.to_sse``) with the journal index as the SSE ``id``.
These tests drive the real app (``create_app`` with its full middleware
stack) through a raw in-process ASGI exchange - no network, no live
server - and feed the bytes the route sends into the real TUI consumer.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.routes import status_events
from bernstein.core.server import create_app
from bernstein.tui.event_stream import JournalStreamConsumer, StreamFrame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from fastapi import FastAPI

RUN = "run-1"
_URL = f"/events/thread?run={RUN}"


@pytest.fixture(autouse=True)
def _fast_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_events, "THREAD_STREAM_POLL_S", 0.01)
    monkeypatch.delenv("BERNSTEIN_REPLAY_RETENTION", raising=False)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    return tmp_path


def _app(workdir: Path, **kwargs: Any) -> FastAPI:
    return create_app(jsonl_path=workdir / ".sdd" / "runtime" / "tasks.jsonl", **kwargs)


def _journal(workdir: Path, rows: int = 3) -> EventJournal:
    journal = EventJournal(RUN, workdir / ".sdd")
    for i in range(rows):
        journal.record("task_claimed", task_id=f"t-{i}", agent_id="agent-a")
    return journal


def _journal_file(workdir: Path) -> Path:
    return workdir / ".sdd" / "runs" / RUN / "journal.jsonl"


class _Exchange:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode()


async def _stream(
    app: FastAPI,
    url: str,
    *,
    until: Callable[[str], bool],
    headers: dict[str, str] | None = None,
    on_body: Callable[[str], Awaitable[None]] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 40000),
    timeout: float = 5.0,
) -> _Exchange:
    """Run one GET through the ASGI app; disconnect once *until* holds."""
    parts = urlsplit(url)
    done = asyncio.Event()
    exchange = _Exchange()
    sent_request = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            exchange.status = message["status"]
            exchange.headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            exchange.chunks.append(message.get("body", b""))
            if on_body is not None:
                await on_body(exchange.text)
            if until(exchange.text) or not message.get("more_body", False):
                done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parts.path,
        "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout)
    return exchange


def _ids(text: str) -> list[str]:
    return [line[4:] for line in text.split("\n") if line.startswith("id: ")]


def _deliver(chunks: list[str]) -> tuple[list[StreamFrame], str]:
    consumer = JournalStreamConsumer()
    out: list[StreamFrame] = []
    for chunk in chunks:
        out.extend(consumer.consume(chunk))
    return out, consumer.last_event_id


@pytest.mark.asyncio
async def test_journal_rows_reach_the_tui_consumer_with_journal_index_ids(workdir: Path) -> None:
    _journal(workdir)
    rows = load_events(_journal_file(workdir)).events

    exchange = await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3)

    assert exchange.status == 200
    assert exchange.headers["content-type"].startswith("text/event-stream")
    frames, cursor = _deliver([exchange.text])
    assert [f.sse_id for f in frames] == ["0", "1", "2"]
    assert [f.event for f in frames] == ["thread.step"] * 3
    assert [f.data["event_hash"] for f in frames] == [row["event_hash"] for row in rows]
    assert [f.data["payload"]["task_id"] for f in frames] == ["t-0", "t-1", "t-2"]
    assert cursor == "2"


@pytest.mark.asyncio
async def test_every_partition_of_route_bytes_delivers_the_same_frames(workdir: Path) -> None:
    _journal(workdir)
    text = (await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3)).text
    expected = _deliver([text])
    assert [f.sse_id for f in expected[0]] == ["0", "1", "2"]

    for cut in range(len(text) + 1):
        assert _deliver([text[:cut], text[cut:]]) == expected, f"split at {cut}"
    rng = random.Random(6842)
    for _ in range(200):
        cuts = sorted(rng.sample(range(1, len(text)), rng.randint(1, 25)))
        bounds = [0, *cuts, len(text)]
        assert _deliver([text[a:b] for a, b in itertools.pairwise(bounds)]) == expected, cuts


@pytest.mark.asyncio
async def test_heartbeats_carry_no_id_and_never_move_the_cursor(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_events, "THREAD_STREAM_HEARTBEAT_S", 0.03)
    _journal(workdir, rows=1)

    text = (await _stream(_app(workdir), _URL, until=lambda t: t.count("event: heartbeat") >= 3)).text

    assert text.startswith('event: heartbeat\ndata: {"connected": true}\n\n')
    blocks = [b for b in text.split("\n\n") if b]
    heartbeats = [b for b in blocks if b.startswith("event: heartbeat")]
    assert len(heartbeats) >= 3
    assert all("id:" not in b for b in heartbeats)
    frames, cursor = _deliver([text])
    assert [f.sse_id for f in frames] == ["0"]
    assert cursor == "0"


@pytest.mark.asyncio
async def test_last_event_id_resumes_after_the_cursor_without_duplicates(workdir: Path) -> None:
    _journal(workdir, rows=4)
    app = _app(workdir)
    consumer = JournalStreamConsumer()
    first = await _stream(app, _URL, until=lambda t: len(_ids(t)) >= 2)
    # The first connection drops right after frame 1 (anything later is unseen).
    seen = first.text[: first.text.index("id: 2")] if "id: 2" in first.text else first.text
    consumer.consume(seen)
    consumer.end_stream()
    assert consumer.reconnect_headers() == {"Last-Event-ID": "1"}

    second = await _stream(
        app, _URL, headers=consumer.reconnect_headers(), until=lambda t: len(_ids(t)) >= 2, client=("127.0.0.2", 1)
    )

    assert _ids(second.text) == ["2", "3"]
    assert [f.sse_id for f in consumer.consume(second.text)] == ["2", "3"]


@pytest.mark.asyncio
async def test_resume_survives_a_server_restart(workdir: Path) -> None:
    journal = _journal(workdir, rows=3)
    first = await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3)
    assert _ids(first.text) == ["0", "1", "2"]
    journal.record("task_completed", task_id="t-2", agent_id="agent-a")

    restarted = _app(workdir)  # a fresh app instance over the same .sdd
    second = await _stream(restarted, _URL, headers={"Last-Event-ID": "2"}, until=lambda t: len(_ids(t)) >= 1)

    assert _ids(second.text) == ["3"]


@pytest.mark.asyncio
async def test_rows_appended_while_connected_are_streamed(workdir: Path) -> None:
    journal = _journal(workdir, rows=2)
    appended = False

    async def append_once(text: str) -> None:
        nonlocal appended
        if not appended and len(_ids(text)) >= 2:
            appended = True
            journal.record("task_completed", task_id="t-1", agent_id="agent-a")

    exchange = await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3, on_body=append_once)

    frames, cursor = _deliver([exchange.text])
    assert [(f.sse_id, f.data["journal_event"]) for f in frames] == [
        ("0", "task_claimed"),
        ("1", "task_claimed"),
        ("2", "task_completed"),
    ]
    assert cursor == "2"


@pytest.mark.asyncio
async def test_partly_written_last_row_is_held_until_complete(workdir: Path) -> None:
    _journal(workdir, rows=2)
    path = _journal_file(workdir)
    row = json.dumps({"event": "task_completed", "index": 2, "event_hash": "ff", "task_id": "t-1"})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(row[:15])
    polls = 0

    async def complete_later(text: str) -> None:
        nonlocal polls
        polls += 1
        if polls == 3:
            assert _ids(text) == ["0", "1"]  # the torn row was not served
            with path.open("a", encoding="utf-8") as handle:
                handle.write(row[15:] + "\n")

    exchange = await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3, on_body=complete_later)

    frames, _ = _deliver([exchange.text])
    assert frames[-1] == StreamFrame(
        "2",
        "thread.step",
        {"event_hash": "ff", "journal_event": "task_completed", "journal_index": 2, "payload": {"task_id": "t-1"}},
    )


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_indices_are_never_resent(workdir: Path) -> None:
    path = _journal_file(workdir)
    path.parent.mkdir(parents=True)
    lines = [{"event": "e", "index": i, "event_hash": f"h{n}"} for n, i in enumerate([0, 1, 1, 0, 2])]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    text = (await _stream(_app(workdir), _URL, until=lambda t: len(_ids(t)) >= 3)).text

    assert _ids(text) == ["0", "1", "2"]
    frames, _ = _deliver([text])
    assert [f.data["event_hash"] for f in frames] == ["h0", "h1", "h4"]


@pytest.mark.asyncio
async def test_stream_ends_when_the_journal_is_removed(workdir: Path) -> None:
    _journal(workdir, rows=1)
    path = _journal_file(workdir)

    async def remove(text: str) -> None:
        if _ids(text) and path.exists():
            path.unlink()

    exchange = await _stream(_app(workdir), _URL, until=lambda _t: False, on_body=remove)

    assert _ids(exchange.text) == ["0"]  # ended on its own; `until` never held


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "headers", "status", "detail"),
    [
        ("/events/thread", {}, 422, "run"),
        ("/events/thread?run=a/b", {}, 422, "run"),
        ("/events/thread?run=..", {}, 400, "invalid run id"),
        ("/events/thread?run=unknown-run", {}, 404, "run journal not found"),
        (_URL, {"Last-Event-ID": "abc"}, 400, "Last-Event-ID must be a journal index"),
        (_URL, {"Last-Event-ID": "-1"}, 400, "Last-Event-ID must be a journal index"),
        (_URL, {"Last-Event-ID": "9" * 19}, 400, "Last-Event-ID must be a journal index"),
    ],
)
async def test_bad_requests_get_a_finite_error(
    workdir: Path, url: str, headers: dict[str, str], status: int, detail: str
) -> None:
    _journal(workdir, rows=1)

    exchange = await _stream(_app(workdir), url, headers=headers, until=lambda _t: False)

    assert exchange.status == status
    assert detail in exchange.text
    assert "id: " not in exchange.text


@pytest.mark.asyncio
async def test_blank_last_event_id_replays_from_the_start(workdir: Path) -> None:
    _journal(workdir, rows=2)

    exchange = await _stream(_app(workdir), _URL, headers={"Last-Event-ID": " "}, until=lambda t: len(_ids(t)) >= 2)

    assert _ids(exchange.text) == ["0", "1"]


@pytest.mark.asyncio
async def test_reconnect_limiter_covers_the_thread_stream(workdir: Path) -> None:
    _journal(workdir, rows=1)
    app = _app(workdir)
    statuses = []
    for _ in range(4):
        exchange = await _stream(app, _URL, until=lambda t: "connected" in t, client=("10.9.9.9", 5))
        statuses.append(exchange.status)

    assert statuses == [200, 200, 200, 429]
    assert "sse_reconnect" in exchange.text


@pytest.mark.asyncio
async def test_auth_token_is_required_when_configured(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    _journal(workdir, rows=1)
    app = _app(workdir, auth_token="route-test-token")

    denied = await _stream(app, _URL, until=lambda _t: False)
    allowed = await _stream(
        app,
        _URL,
        headers={"Authorization": "Bearer route-test-token"},
        until=lambda t: len(_ids(t)) >= 1,
        client=("127.0.0.3", 1),
    )

    assert denied.status == 401
    assert "id: " not in denied.text
    assert allowed.status == 200
    assert _ids(allowed.text) == ["0"]


@pytest.mark.asyncio
async def test_events_stream_is_unchanged_and_carries_no_journal_ids(workdir: Path) -> None:
    _journal(workdir, rows=2)

    exchange = await _stream(_app(workdir), "/events", until=lambda t: "connected" in t)

    assert exchange.status == 200
    assert exchange.text == 'event: heartbeat\ndata: {"connected": true}\n\n'
