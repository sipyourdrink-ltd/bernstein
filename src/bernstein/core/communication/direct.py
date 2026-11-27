"""Directed 1:1 channels between agent sessions.

A minimal file-backed primitive for one-to-one messaging between running
agent sessions. Each receiver has its own inbox directory under
``.sdd/runtime/channels/<to_session>/``; messages are individual JSON files
named by message id (sortable by timestamp prefix). The receiver polls for
new messages, optionally filtering by an opaque cursor (the last seen
``MessageId``).

Mention support: the message body is scanned for ``@<session-id>`` tokens.
Callers (typically the spawn/orchestration loop) can use
:func:`mentions_session` to decide whether a wakeup signal should be raised
for an idle session.

Out of scope (intentionally deferred):
    * TUI inbox rendering
    * retention / expiry policies
    * read receipts / acks
    * broadcast variants
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# A monotonically-comparable opaque token for "I have seen messages up to
# here." Implemented as the message file stem (``<ts_ns>-<rand>``) so plain
# lexicographic sort matches arrival order.
MessageId = str

# ``@`` followed by a session-id-like token (alnum, ``-``, ``_``).
# Anchored on an ASCII word boundary so emails (``user@host``) do not match.
_MENTION_RE = re.compile(r"(?<!\w)@([^\W_][\w-]*)", re.ASCII)


@dataclass(frozen=True)
class Message:
    """A single directed message between two agent sessions.

    Attributes:
        id: Sortable opaque identifier (also the filename stem on disk).
        from_session: Sender session id.
        to_session: Receiver session id.
        body: Free-text payload; may contain ``@<session-id>`` mentions.
        timestamp: Unix epoch seconds when the message was created.
    """

    id: MessageId
    from_session: str
    to_session: str
    body: str
    timestamp: float

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Message:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        raw_ts = d.get("timestamp", 0.0)
        ts: float
        if isinstance(raw_ts, (int, float)):
            ts = float(raw_ts)
        elif isinstance(raw_ts, str) and raw_ts:
            try:
                ts = float(raw_ts)
            except ValueError:
                ts = 0.0
        else:
            ts = 0.0
        return cls(
            id=str(d.get("id", "")),
            from_session=str(d.get("from_session", "")),
            to_session=str(d.get("to_session", "")),
            body=str(d.get("body", "")),
            timestamp=ts,
        )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _channels_root(workdir: Path) -> Path:
    """Return the channels root directory (``<workdir>/.sdd/runtime/channels``)."""
    return workdir / ".sdd" / "runtime" / "channels"


def _inbox_dir(workdir: Path, to_session: str) -> Path:
    """Return the inbox directory for *to_session*, creating it on demand."""
    d = _channels_root(workdir) / to_session
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_message_id(now_s: float | None = None) -> MessageId:
    """Generate a sortable opaque message id.

    Format: ``<ts_ns>-<rand12>``. Lexicographic sort matches arrival order
    even across processes since the nanosecond prefix is monotonically
    advancing for any single sender.
    """
    ts_ns = int((now_s if now_s is not None else time.time()) * 1e9)
    return f"{ts_ns:020d}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Send / receive
# ---------------------------------------------------------------------------


def send(
    from_session: str,
    to_session: str,
    message: str,
    workdir: Path,
) -> MessageId:
    """Deliver *message* from one session to another via a file-backed inbox.

    The message is written to ``<workdir>/.sdd/runtime/channels/<to_session>/<msg_id>.json``.
    The inbox directory is created if it does not exist.

    Args:
        from_session: Sender session id.
        to_session: Receiver session id.
        message: Free-text message body. May contain ``@<session-id>`` tokens.
        workdir: Project root (parent of ``.sdd/``).

    Returns:
        The newly-created :data:`MessageId`.

    Raises:
        ValueError: If either session id is empty.
    """
    if not from_session or not to_session:
        raise ValueError("from_session and to_session must be non-empty")

    now = time.time()
    msg_id = _new_message_id(now)
    msg = Message(
        id=msg_id,
        from_session=from_session,
        to_session=to_session,
        body=message,
        timestamp=now,
    )

    inbox = _inbox_dir(workdir, to_session)
    target = inbox / f"{msg_id}.json"
    # Atomic-ish write via tmp + rename so a polling reader never sees a
    # partial JSON file.
    tmp = inbox / f".{msg_id}.json.tmp"
    tmp.write_text(json.dumps(msg.to_dict()), encoding="utf-8")
    tmp.replace(target)
    return msg_id


def receive(
    session: str,
    workdir: Path,
    since: MessageId | None = None,
) -> list[Message]:
    """Poll the inbox for *session* and return messages newer than *since*.

    Messages are returned in arrival order (oldest first). If *since* is
    ``None``, every message currently in the inbox is returned.

    Args:
        session: Receiver session id (the inbox owner).
        workdir: Project root (parent of ``.sdd/``).
        since: Opaque cursor returned by a previous call (the id of the
            last seen message). Strictly exclusive lower bound.

    Returns:
        Ordered list of :class:`Message` objects.
    """
    inbox = _channels_root(workdir) / session
    if not inbox.exists():
        return []

    messages: list[Message] = []
    for path in sorted(inbox.glob("*.json")):
        if path.name.startswith("."):
            continue
        msg_id = path.stem
        if since is not None and msg_id <= since:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Skip half-written or corrupt files; the next poll will retry
            # if the writer eventually completes.
            continue
        # Only a JSON object is a message. Any other valid JSON (an array, a number) is skipped
        # like a corrupt file, so one bad entry does not stop the read (#8054).
        if not isinstance(data, dict):
            continue
        messages.append(Message.from_dict(data))
    return messages


# ---------------------------------------------------------------------------
# Mention parsing / wakeup helpers
# ---------------------------------------------------------------------------


def parse_mentions(body: str) -> list[str]:
    """Extract ``@<session-id>`` tokens from *body* in left-to-right order.

    Mentions inside email-like substrings (``user@host``) are ignored.
    Duplicates are preserved so callers can count multiple references.

    Args:
        body: Free-text message content.

    Returns:
        List of session ids (without the leading ``@``).
    """
    if not body:
        return []
    return _MENTION_RE.findall(body)


def mentions_session(body: str, session: str) -> bool:
    """Return ``True`` iff *body* contains ``@<session>`` as a mention.

    Args:
        body: Free-text message content.
        session: Session id to test for.
    """
    if not session:
        return False
    return session in parse_mentions(body)


@dataclass(frozen=True)
class WakeupSignal:
    """Flag emitted when *session* has unread messages mentioning it.

    Attributes:
        session: Session that should be woken.
        triggering_messages: Inbox messages that contain the mention.
    """

    session: str
    triggering_messages: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def should_wake(self) -> bool:
        """True iff at least one mentioning message is queued."""
        return bool(self.triggering_messages)


def detect_wakeup(
    session: str,
    workdir: Path,
    since: MessageId | None = None,
) -> WakeupSignal:
    """Inspect *session*'s inbox and emit a wakeup if it is mentioned.

    The orchestrator's spawn loop calls this per idle session. The returned
    :class:`WakeupSignal` exposes a single ``should_wake`` flag plus the
    triggering messages so the caller can include them in the next prompt.

    Args:
        session: Receiver session id.
        workdir: Project root (parent of ``.sdd/``).
        since: Optional cursor; only newer messages are considered.

    Returns:
        A :class:`WakeupSignal`. ``should_wake`` is False when the inbox is
        empty or contains only non-mentioning messages.
    """
    new_messages = receive(session, workdir, since=since)
    triggering = tuple(m for m in new_messages if mentions_session(m.body, session))
    return WakeupSignal(session=session, triggering_messages=triggering)


__all__ = [
    "Message",
    "MessageId",
    "WakeupSignal",
    "detect_wakeup",
    "mentions_session",
    "parse_mentions",
    "receive",
    "send",
]
