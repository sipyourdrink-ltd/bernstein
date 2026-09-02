"""Wait graph over mailbox rendezvous entries (#3450).

The task mailbox already carries a ``question`` kind, but nothing records
that the asker then *waited*. A task needing an answer from a sibling can
only poll or proceed without it, and both look identical afterwards: the
question is chained, the silence is not.

A rendezvous records the wait in the same currency as the message. An
opening entry binds the question's ``entry_hash``, the waiting task and the
addressed task; a closing entry binds the opening ``entry_hash``, the reply
``entry_hash`` and a resolution (``answered`` / ``timeout`` / ``refused``).
The pair - not a process-local event object - is what "this task waited and
got an answer" means, so the wait survives a restart, replays from the
record, and stays checkable offline against the mailbox chain.

This module is the analysis half and is deliberately pure: it takes a list
of :class:`~bernstein.core.communication.task_mailbox.MailboxMessage` and
reads no journal of its own. :func:`wait_graph` folds the entries into
"waiter task id -> task ids it is blocked on", counting only opens with no
matching close, and :func:`find_cycle` names a deadlock as a deterministic
list of task ids. Two operators reading the same chain therefore compute
the same graph and name the same cycle, whatever order they read the
entries in.

Writing the entries, suspending the asker on an open and resolving a
replayed wait from its recorded close are not here; this module is what
those need to agree on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bernstein.core.communication.task_mailbox import MailboxMessage

logger = logging.getLogger(__name__)

__all__ = [
    "RENDEZVOUS_CLOSED_KIND",
    "RENDEZVOUS_OPEN_KIND",
    "RENDEZVOUS_RESOLUTIONS",
    "RENDEZVOUS_SCHEMA_VERSION",
    "RendezvousClose",
    "RendezvousOpen",
    "closed_entries",
    "encode_close_body",
    "encode_open_body",
    "find_cycle",
    "open_entries",
    "wait_graph",
]

#: Body schema version stamped into every rendezvous entry.
RENDEZVOUS_SCHEMA_VERSION: int = 1

#: Mailbox entry kind opening a wait.
RENDEZVOUS_OPEN_KIND: str = "rendezvous_open"

#: Mailbox entry kind closing a wait opened by :data:`RENDEZVOUS_OPEN_KIND`.
RENDEZVOUS_CLOSED_KIND: str = "rendezvous_closed"

#: The closed set of ways a wait can end. A wait that ends any other way
#: ends without a record, which is the failure this feature exists to remove.
RENDEZVOUS_RESOLUTIONS: tuple[str, ...] = ("answered", "timeout", "refused")


def _canonical_json(payload: dict[str, Any]) -> str:
    """Return RFC 8785-style canonical JSON text for ``payload``."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def encode_open_body(*, question_entry_hash: str, waiter_task_id: str, awaited_task_id: str) -> str:
    """Return the canonical body of a ``rendezvous_open`` entry.

    Args:
        question_entry_hash: ``entry_hash`` of the question being waited on.
        waiter_task_id: The task that blocks.
        awaited_task_id: The task the answer is expected from.

    Raises:
        ValueError: any field is empty; a wait with an unnamed end is not
            reconstructable from the chain.
    """
    for name, value in (
        ("question_entry_hash", question_entry_hash),
        ("waiter_task_id", waiter_task_id),
        ("awaited_task_id", awaited_task_id),
    ):
        if not value:
            raise ValueError(f"rendezvous open requires a non-empty {name}")
    return _canonical_json(
        {
            "awaited_task_id": awaited_task_id,
            "question_entry_hash": question_entry_hash,
            "v": RENDEZVOUS_SCHEMA_VERSION,
            "waiter_task_id": waiter_task_id,
        }
    )


def encode_close_body(*, open_entry_hash: str, reply_entry_hash: str, resolution: str) -> str:
    """Return the canonical body of a ``rendezvous_closed`` entry.

    Args:
        open_entry_hash: ``entry_hash`` of the opening entry this closes.
        reply_entry_hash: ``entry_hash`` of the answer, or ``""`` when the
            wait ended without one.
        resolution: One of :data:`RENDEZVOUS_RESOLUTIONS`.

    Raises:
        ValueError: ``open_entry_hash`` is empty, the resolution is outside
            the closed set, or ``answered`` carries no reply entry.
    """
    if not open_entry_hash:
        raise ValueError("rendezvous close requires a non-empty open_entry_hash")
    if resolution not in RENDEZVOUS_RESOLUTIONS:
        raise ValueError(f"unknown rendezvous resolution {resolution!r}; expected one of {RENDEZVOUS_RESOLUTIONS}")
    if resolution == "answered" and not reply_entry_hash:
        raise ValueError("an answered rendezvous must bind the reply entry_hash")
    return _canonical_json(
        {
            "open_entry_hash": open_entry_hash,
            "reply_entry_hash": reply_entry_hash,
            "resolution": resolution,
            "v": RENDEZVOUS_SCHEMA_VERSION,
        }
    )


@dataclass(frozen=True)
class RendezvousOpen:
    """One opened wait, read back from its mailbox entry.

    Attributes:
        entry_hash: The opening entry's chain hash; what a close binds to.
        seq: Chain append index of the opening entry.
        question_entry_hash: The question the waiter blocks on.
        waiter_task_id: The task that blocks.
        awaited_task_id: The task the answer is expected from.
    """

    entry_hash: str
    seq: int
    question_entry_hash: str
    waiter_task_id: str
    awaited_task_id: str

    @classmethod
    def from_message(cls, message: MailboxMessage) -> RendezvousOpen:
        """Parse one ``rendezvous_open`` entry.

        Raises:
            ValueError: the body is not a canonical open payload.
        """
        payload = _decode(message.body)
        waiter = str(payload.get("waiter_task_id", ""))
        awaited = str(payload.get("awaited_task_id", ""))
        question = str(payload.get("question_entry_hash", ""))
        if not (waiter and awaited and question):
            raise ValueError("rendezvous open body is missing a bound field")
        return cls(
            entry_hash=message.entry_hash,
            seq=message.seq,
            question_entry_hash=question,
            waiter_task_id=waiter,
            awaited_task_id=awaited,
        )


@dataclass(frozen=True)
class RendezvousClose:
    """One closed wait, read back from its mailbox entry.

    Attributes:
        entry_hash: The closing entry's chain hash.
        seq: Chain append index of the closing entry.
        open_entry_hash: The opening entry this close binds.
        reply_entry_hash: The answer entry, or ``""`` when there was none.
        resolution: One of :data:`RENDEZVOUS_RESOLUTIONS`.
    """

    entry_hash: str
    seq: int
    open_entry_hash: str
    reply_entry_hash: str
    resolution: str

    @classmethod
    def from_message(cls, message: MailboxMessage) -> RendezvousClose:
        """Parse one ``rendezvous_closed`` entry.

        Raises:
            ValueError: the body is not a canonical close payload.
        """
        payload = _decode(message.body)
        open_entry_hash = str(payload.get("open_entry_hash", ""))
        resolution = str(payload.get("resolution", ""))
        if not open_entry_hash:
            raise ValueError("rendezvous close body binds no open entry")
        if resolution not in RENDEZVOUS_RESOLUTIONS:
            raise ValueError(f"unknown rendezvous resolution {resolution!r}")
        return cls(
            entry_hash=message.entry_hash,
            seq=message.seq,
            open_entry_hash=open_entry_hash,
            reply_entry_hash=str(payload.get("reply_entry_hash", "")),
            resolution=resolution,
        )


def _decode(body: str) -> dict[str, Any]:
    """Return the decoded rendezvous body object.

    Raises:
        ValueError: the body is not a JSON object.
    """
    try:
        decoded: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("rendezvous body is not JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("rendezvous body is not a JSON object")
    return cast("dict[str, Any]", decoded)


def open_entries(messages: Sequence[MailboxMessage]) -> list[RendezvousOpen]:
    """Return every parseable ``rendezvous_open`` entry, in chain order.

    Entries are ordered by ``(seq, entry_hash)`` rather than by the caller's
    list order, so the result is a function of the entries themselves. A row
    whose body does not parse is skipped with a warning: one corrupt entry
    must not make the remaining waits unreadable.
    """
    return sorted(
        _parse_all(messages, RENDEZVOUS_OPEN_KIND, RendezvousOpen.from_message),
        key=lambda entry: (entry.seq, entry.entry_hash),
    )


def closed_entries(messages: Sequence[MailboxMessage]) -> list[RendezvousClose]:
    """Return every parseable ``rendezvous_closed`` entry, in chain order.

    Ordering and the skip-on-malformed rule match :func:`open_entries`.
    """
    return sorted(
        _parse_all(messages, RENDEZVOUS_CLOSED_KIND, RendezvousClose.from_message),
        key=lambda entry: (entry.seq, entry.entry_hash),
    )


def _parse_all[EntryT: (RendezvousOpen, RendezvousClose)](
    messages: Sequence[MailboxMessage],
    kind: str,
    parse: Callable[[MailboxMessage], EntryT],
) -> list[EntryT]:
    """Parse every entry of ``kind``, skipping (and logging) malformed rows."""
    parsed: list[EntryT] = []
    for message in messages:
        if message.kind != kind:
            continue
        try:
            parsed.append(parse(message))
        except ValueError as exc:
            logger.warning(
                "rendezvous: skipping malformed %s entry at seq %d: %s",
                sanitize_log(kind),
                message.seq,
                sanitize_log(str(exc)),
            )
    return parsed


def wait_graph(messages: Sequence[MailboxMessage]) -> dict[str, set[str]]:
    """Fold the entries into the outstanding wait graph.

    An open entry whose ``entry_hash`` is bound by some close is resolved and
    contributes no edge; every other open entry does. The result maps a
    waiting task id to the task ids it is currently blocked on, and depends
    only on the set of entries - not on the order they were read in.
    """
    resolved = {entry.open_entry_hash for entry in closed_entries(messages)}
    graph: dict[str, set[str]] = {}
    for entry in open_entries(messages):
        if entry.entry_hash in resolved:
            continue
        graph.setdefault(entry.waiter_task_id, set()).add(entry.awaited_task_id)
    return graph


def find_cycle(graph: Mapping[str, set[str]]) -> list[str] | None:
    """Return the task ids forming a wait cycle, or ``None`` when there is none.

    The walk starts from the lowest task id and follows successors in sorted
    order, and the cycle it finds is rotated so the lowest task id in it
    leads and the walk order follows. Two operators folding the same entries
    therefore name the same cycle in the same order, which is what makes a
    deadlock a reportable object rather than an absence of progress.

    A task that opened a wait on itself is a cycle of one.
    """
    unvisited, on_path, done = 0, 1, 2
    state: dict[str, int] = {}
    for root in sorted(graph):
        if state.get(root, unvisited) != unvisited:
            continue
        path: list[str] = [root]
        state[root] = on_path
        stack: list[tuple[str, list[str], int]] = [(root, sorted(graph.get(root, set())), 0)]
        while stack:
            node, successors, index = stack[-1]
            if index >= len(successors):
                stack.pop()
                path.pop()
                state[node] = done
                continue
            stack[-1] = (node, successors, index + 1)
            successor = successors[index]
            marker = state.get(successor, unvisited)
            if marker == on_path:
                cycle = path[path.index(successor) :]
                pivot = cycle.index(min(cycle))
                return cycle[pivot:] + cycle[:pivot]
            if marker == unvisited:
                state[successor] = on_path
                path.append(successor)
                stack.append((successor, sorted(graph.get(successor, set())), 0))
    return None
