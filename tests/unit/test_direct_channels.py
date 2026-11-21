"""Unit tests for directed 1:1 channels and @mention wakeup detection."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.communication.direct import (
    Message,
    detect_wakeup,
    mentions_session,
    parse_mentions,
    receive,
    send,
)

# ---------------------------------------------------------------------------
# send / receive round-trip
# ---------------------------------------------------------------------------


def test_send_receive_round_trip(tmp_path: Path) -> None:
    """A sent message lands in the receiver's inbox and round-trips intact."""
    msg_id = send(
        from_session="worker-1",
        to_session="worker-2",
        message="ping",
        workdir=tmp_path,
    )
    assert msg_id

    inbox = receive(session="worker-2", workdir=tmp_path)
    assert len(inbox) == 1
    msg = inbox[0]
    assert isinstance(msg, Message)
    assert msg.id == msg_id
    assert msg.from_session == "worker-1"
    assert msg.to_session == "worker-2"
    assert msg.body == "ping"
    assert msg.timestamp > 0


def test_receive_isolates_per_session(tmp_path: Path) -> None:
    """Messages addressed to A do not leak into B's inbox."""
    send("a", "b", "hello b", workdir=tmp_path)
    send("a", "c", "hello c", workdir=tmp_path)

    inbox_b = receive("b", workdir=tmp_path)
    inbox_c = receive("c", workdir=tmp_path)
    inbox_d = receive("d", workdir=tmp_path)

    assert [m.body for m in inbox_b] == ["hello b"]
    assert [m.body for m in inbox_c] == ["hello c"]
    assert inbox_d == []


def test_receive_since_cursor_filters_seen_messages(tmp_path: Path) -> None:
    """Passing the last-seen id excludes it and earlier messages."""
    first = send("a", "b", "one", workdir=tmp_path)
    # Tiny sleep so the second id sorts strictly higher even if the system
    # clock has very low resolution on some platforms.
    time.sleep(0.001)
    second = send("a", "b", "two", workdir=tmp_path)

    after_first = receive("b", workdir=tmp_path, since=first)
    assert [m.id for m in after_first] == [second]

    after_second = receive("b", workdir=tmp_path, since=second)
    assert after_second == []


def test_receive_returns_arrival_order(tmp_path: Path) -> None:
    """Messages come back oldest-first regardless of disk listing order."""
    ids = []
    for i in range(5):
        ids.append(send("a", "b", f"msg-{i}", workdir=tmp_path))
        time.sleep(0.001)

    inbox = receive("b", workdir=tmp_path)
    assert [m.id for m in inbox] == ids


def test_send_rejects_empty_session_ids(tmp_path: Path) -> None:
    """Empty sender/receiver ids raise rather than corrupt the layout."""
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        send("", "b", "x", workdir=tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        send("a", "", "x", workdir=tmp_path)


# ---------------------------------------------------------------------------
# mention parsing
# ---------------------------------------------------------------------------


def test_parse_mentions_extracts_session_ids() -> None:
    """All ``@<id>`` tokens are captured in left-to-right order."""
    body = "@worker-2 please retry, then ping @worker-3 (cc @worker-2)"
    assert parse_mentions(body) == ["worker-2", "worker-3", "worker-2"]


def test_parse_mentions_ignores_email_addresses() -> None:
    """``user@host`` patterns must not be treated as mentions."""
    assert parse_mentions("contact me at user@example.com") == []


def test_parse_mentions_handles_empty_input() -> None:
    """Empty/None-equivalent inputs return an empty list."""
    assert parse_mentions("") == []
    assert parse_mentions("no mentions here") == []


def test_mentions_session_matches_exact_token() -> None:
    """Only an exact ``@session`` match counts as a mention."""
    assert mentions_session("hi @worker-2", "worker-2")
    assert not mentions_session("hi @worker-22", "worker-2")
    assert not mentions_session("hi worker-2", "worker-2")


# ---------------------------------------------------------------------------
# wakeup detection (mention-detection flag flip)
# ---------------------------------------------------------------------------


def test_detect_wakeup_flips_on_mention(tmp_path: Path) -> None:
    """A mentioning message in the inbox flips ``should_wake`` to True."""
    # Baseline: empty inbox → no wakeup.
    initial = detect_wakeup("worker-2", workdir=tmp_path)
    assert initial.should_wake is False
    assert initial.triggering_messages == ()

    # Non-mentioning message → still no wakeup.
    send("worker-1", "worker-2", "fyi the deploy is done", workdir=tmp_path)
    quiet = detect_wakeup("worker-2", workdir=tmp_path)
    assert quiet.should_wake is False

    # Mentioning message → wakeup flag flips.
    send("worker-1", "worker-2", "@worker-2 please retry", workdir=tmp_path)
    woken = detect_wakeup("worker-2", workdir=tmp_path)
    assert woken.should_wake is True
    assert len(woken.triggering_messages) == 1
    assert woken.triggering_messages[0].body == "@worker-2 please retry"


def test_detect_wakeup_respects_since_cursor(tmp_path: Path) -> None:
    """A cursor past the mention suppresses the wakeup on the next poll."""
    seen = send("worker-1", "worker-2", "@worker-2 ping", workdir=tmp_path)

    # Without cursor: triggers wakeup.
    first = detect_wakeup("worker-2", workdir=tmp_path)
    assert first.should_wake is True

    # After acknowledging the message via cursor: no further wakeup.
    second = detect_wakeup("worker-2", workdir=tmp_path, since=seen)
    assert second.should_wake is False


def test_receive_continues_after_a_non_object_record(tmp_path: Path) -> None:
    """A JSON array or scalar in the inbox is skipped, not an AttributeError for the whole read (#8054)."""
    inbox = tmp_path / ".sdd" / "runtime" / "channels" / "demo"
    inbox.mkdir(parents=True)
    (inbox / "001.json").write_text("[]", encoding="utf-8")
    (inbox / "002.json").write_text("7", encoding="utf-8")
    (inbox / "003.json").write_text('{"id":"003","body":"first"}', encoding="utf-8")
    (inbox / "004.json").write_text('{"id":"004","body":"second"}', encoding="utf-8")

    assert [m.body for m in receive("demo", workdir=tmp_path)] == ["first", "second"]
    # The cursor still excludes what came before it.
    assert [m.body for m in receive("demo", workdir=tmp_path, since="003")] == ["second"]
