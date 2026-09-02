"""Wait-graph and cycle-naming properties of the mailbox rendezvous (#3450).

The rendezvous records "task X is blocked on task Y" as a pair of mailbox
entries rather than as thread state, so the wait graph is a function of the
entries alone. These tests pin that: the same open/close set fed in three
different insertion orders names the same cycle, closing a leg removes the
wait, and a cycle is reported starting at the lowest task id so two operators
reading the same chain name it identically.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from bernstein.core.communication.rendezvous import (
    RENDEZVOUS_CLOSED_KIND,
    RENDEZVOUS_OPEN_KIND,
    closed_entries,
    encode_close_body,
    encode_open_body,
    find_cycle,
    open_entries,
    wait_graph,
)
from bernstein.core.communication.task_mailbox import MailboxMessage


def _message(*, seq: int, kind: str, body: str, entry_hash: str, task_id: str = "recipient") -> MailboxMessage:
    """Build one journal entry with the fields the analysis reads."""
    return MailboxMessage(
        seq=seq,
        task_id=task_id,
        sender="worker",
        sender_card_fingerprint="unregistered",
        kind=kind,
        body=body,
        body_hash="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        redaction_count=0,
        timestamp=float(seq),
        prev_entry_hash="genesis",
        entry_hash=entry_hash,
    )


def _open(waiter: str, awaited: str, *, seq: int, entry_hash: str, question: str = "hmac-sha256:q") -> MailboxMessage:
    return _message(
        seq=seq,
        kind=RENDEZVOUS_OPEN_KIND,
        body=encode_open_body(question_entry_hash=question, waiter_task_id=waiter, awaited_task_id=awaited),
        entry_hash=entry_hash,
        task_id=awaited,
    )


def _close(open_entry_hash: str, *, seq: int, entry_hash: str, resolution: str = "answered") -> MailboxMessage:
    return _message(
        seq=seq,
        kind=RENDEZVOUS_CLOSED_KIND,
        body=encode_close_body(
            open_entry_hash=open_entry_hash,
            reply_entry_hash="hmac-sha256:reply" if resolution == "answered" else "",
            resolution=resolution,
        ),
        entry_hash=entry_hash,
    )


def test_cycle_is_a_function_of_the_entries_not_insertion_order() -> None:
    """The named cycle depends on the entry set, never on how the list was built."""
    entries = [
        _open("task-a", "task-b", seq=0, entry_hash="hmac-sha256:o1"),
        _open("task-b", "task-a", seq=1, entry_hash="hmac-sha256:o2"),
        _open("task-c", "task-d", seq=2, entry_hash="hmac-sha256:o3"),
        _message(seq=3, kind="finding", body="unrelated", entry_hash="hmac-sha256:f1"),
    ]
    orders = [
        entries,
        list(reversed(entries)),
        [entries[2], entries[1], entries[3], entries[0]],
    ]
    named = [find_cycle(wait_graph(order)) for order in orders]
    assert named == [["task-a", "task-b"], ["task-a", "task-b"], ["task-a", "task-b"]]


def test_closing_one_leg_of_a_cycle_leaves_no_cycle() -> None:
    """A closed rendezvous is no longer a wait, so the cycle disappears."""
    entries = [
        _open("task-a", "task-b", seq=0, entry_hash="hmac-sha256:o1"),
        _open("task-b", "task-a", seq=1, entry_hash="hmac-sha256:o2"),
    ]
    assert find_cycle(wait_graph(entries)) == ["task-a", "task-b"]
    entries.append(_close("hmac-sha256:o2", seq=2, entry_hash="hmac-sha256:c1"))
    assert wait_graph(entries) == {"task-a": {"task-b"}}
    assert find_cycle(wait_graph(entries)) is None


def test_three_task_cycle_is_named_lowest_task_id_first() -> None:
    """All three ids appear, rotated so the lowest id leads and walk order follows."""
    entries = [
        _open("task-3", "task-1", seq=0, entry_hash="hmac-sha256:o1"),
        _open("task-1", "task-2", seq=1, entry_hash="hmac-sha256:o2"),
        _open("task-2", "task-3", seq=2, entry_hash="hmac-sha256:o3"),
    ]
    assert find_cycle(wait_graph(entries)) == ["task-1", "task-2", "task-3"]


def test_cycle_reached_through_a_task_outside_it_still_leads_with_the_lowest_id() -> None:
    """The walk enters the cycle at ``task-n``; the cycle is still named from ``task-m``.

    The three-task case above starts the walk on the cycle's own lowest id, so
    the rotation there is a no-op and an implementation that dropped it would
    still look right. Here a waiter outside the cycle is the lowest id in the
    graph, so the walk reaches the cycle at its higher member: only the
    rotation makes the reported order a function of the cycle rather than of
    where the walk happened to arrive. ``task-a`` waits but is not deadlocked,
    so it must not be named.
    """
    entries = [
        _open("task-a", "task-n", seq=0, entry_hash="hmac-sha256:o1"),
        _open("task-n", "task-m", seq=1, entry_hash="hmac-sha256:o2"),
        _open("task-m", "task-n", seq=2, entry_hash="hmac-sha256:o3"),
    ]
    assert wait_graph(entries) == {"task-a": {"task-n"}, "task-n": {"task-m"}, "task-m": {"task-n"}}
    assert find_cycle(wait_graph(entries)) == ["task-m", "task-n"]


def test_open_entry_binds_question_hash_and_both_task_ids() -> None:
    """The open record carries the question it blocks on and both ends of the wait."""
    entries = [_open("task-a", "task-b", seq=7, entry_hash="hmac-sha256:o1", question="hmac-sha256:qq")]
    (opened,) = open_entries(entries)
    assert opened.entry_hash == "hmac-sha256:o1"
    assert opened.seq == 7
    assert opened.question_entry_hash == "hmac-sha256:qq"
    assert opened.waiter_task_id == "task-a"
    assert opened.awaited_task_id == "task-b"


def test_close_binding_an_unknown_open_entry_opens_no_wait() -> None:
    """A close that names no open entry neither creates nor cancels a wait edge."""
    entries = [
        _open("task-a", "task-b", seq=0, entry_hash="hmac-sha256:o1"),
        _close("hmac-sha256:nowhere", seq=1, entry_hash="hmac-sha256:c1", resolution="timeout"),
    ]
    (closed,) = closed_entries(entries)
    assert closed.open_entry_hash == "hmac-sha256:nowhere"
    assert closed.resolution == "timeout"
    assert wait_graph(entries) == {"task-a": {"task-b"}}


def test_non_rendezvous_messages_contribute_no_wait_edges() -> None:
    """Findings, artefact refs and plain questions are not waits."""
    entries = [
        _message(seq=0, kind="finding", body="a finding", entry_hash="hmac-sha256:f1"),
        _message(seq=1, kind="question", body="schema frozen?", entry_hash="hmac-sha256:q1"),
        _message(seq=2, kind="artefact_ref", body="sha256:beef", entry_hash="hmac-sha256:a1"),
    ]
    assert open_entries(entries) == []
    assert closed_entries(entries) == []
    assert wait_graph(entries) == {}
    assert find_cycle(wait_graph(entries)) is None


def test_malformed_rendezvous_body_is_skipped_without_failing_the_analysis() -> None:
    """One corrupt row cannot make the remaining waits unreadable."""
    entries = [
        _message(seq=0, kind=RENDEZVOUS_OPEN_KIND, body="not json", entry_hash="hmac-sha256:bad"),
        _message(seq=1, kind=RENDEZVOUS_CLOSED_KIND, body="{}", entry_hash="hmac-sha256:bad2"),
        _open("task-a", "task-b", seq=2, entry_hash="hmac-sha256:o1"),
    ]
    assert [entry.entry_hash for entry in open_entries(entries)] == ["hmac-sha256:o1"]
    assert closed_entries(entries) == []
    assert wait_graph(entries) == {"task-a": {"task-b"}}


def test_rendezvous_bodies_are_canonical_json_byte_stable() -> None:
    """Two writers of the same rendezvous produce byte-identical bodies."""
    first = encode_open_body(
        question_entry_hash="hmac-sha256:q",
        waiter_task_id="task-a",
        awaited_task_id="task-b",
    )
    second = encode_open_body(
        awaited_task_id="task-b",
        waiter_task_id="task-a",
        question_entry_hash="hmac-sha256:q",
    )
    assert first == second
    assert first == json.dumps(json.loads(first), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with pytest.raises(ValueError, match="resolution"):
        encode_close_body(open_entry_hash="hmac-sha256:o1", reply_entry_hash="", resolution="maybe")
