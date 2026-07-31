"""Per-run shared whiteboard primitive (smallest-viable slice).

A *whiteboard* is a per-run, append-only JSONL log that any agent in
the run can write to and read back. This module ships the foundation
the larger ticket calls for - append + filtered-read + schema
validation - without the conflict-resolution machinery, MCP wiring,
or watcher integration. Those remain explicitly deferred (see
``2026-05-07-feat-multi-agent-shared-whiteboard.md`` follow-ups).

Design notes
------------

* Append-only at the line granularity - POSIX append semantics give
  us per-line atomicity below ``PIPE_BUF`` (per
  ``atomic_write.py``'s discussion); a process-local
  :class:`threading.Lock` plus an ``fcntl.LOCK_EX`` advisory lock
  keep multi-thread *and* multi-process appends linearised.
* Read-time visibility filter - the writer declares a list of role
  names that may read the entry. Readers pass their role; entries
  whose ``visibility`` does not include that role are skipped.
  Empty visibility means "public to the run".
* No conflict resolution - overlapping subjects are preserved as
  separate entries; ordering follows the file. ``last-write-wins``
  / ``merge-union`` / ``human-review`` strategies are deferred.
* No HMAC envelope yet - ``audit.py`` integration is a later slice.
* Off-by-default - nothing wires this into the orchestrator. Callers
  opt in by constructing a :class:`Whiteboard` directly.

Schema
------

Each line is a JSON object with the keys validated by
:func:`_validate_entry`::

    {
        "key": str,
        "scope": str,
        "value": <any JSON>,
        "owner_agent_id": str,
        "visibility": [<role>, ...],
        "ts_ns": int,
    }

``visibility=[]`` means the entry is readable by every role.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# Public role label that overrides per-entry visibility - a reader
# whose role is "*" is treated as "see everything", useful for
# orchestrator-side debugging utilities. Regular agents should always
# pass a concrete role string.
WILDCARD_ROLE = "*"

_WHITEBOARD_FILENAME = "whiteboard.jsonl"


@dataclass(frozen=True, slots=True)
class WhiteboardEntry:
    """Single immutable record on the whiteboard.

    Attributes:
        key: Logical name within ``scope`` (e.g. ``"api_contract"``).
        scope: Bucket label, free-form (e.g. ``"backend"``).
        value: JSON-serialisable payload.
        owner_agent_id: Identifier of the writing agent.
        visibility: Roles that may read the entry. Empty tuple means
            the entry is public within the run.
        ts_ns: Nanosecond timestamp from :func:`time.time_ns`. Useful
            for deterministic ordering in tests.
    """

    key: str
    scope: str
    value: Any
    owner_agent_id: str
    visibility: tuple[str, ...] = ()
    ts_ns: int = field(default_factory=time.time_ns)

    def to_json_line(self) -> str:
        """Serialise to a single JSON line (no trailing newline)."""
        return json.dumps(
            {
                "key": self.key,
                "scope": self.scope,
                "value": self.value,
                "owner_agent_id": self.owner_agent_id,
                "visibility": list(self.visibility),
                "ts_ns": self.ts_ns,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WhiteboardEntry:
        """Build an entry from a parsed JSON dict.

        Performs the same shape validation as the writer would so
        out-of-band edits cannot smuggle malformed records back in.

        Raises:
            ValueError: If *raw* is missing required fields or has
                values of the wrong type.
        """
        _validate_entry(raw)
        return cls(
            key=raw["key"],
            scope=raw["scope"],
            value=raw["value"],
            owner_agent_id=raw["owner_agent_id"],
            visibility=tuple(raw.get("visibility", ()) or ()),
            ts_ns=int(raw["ts_ns"]),
        )


def _validate_entry(raw: dict[str, Any]) -> None:
    """Raise ``ValueError`` if *raw* is not a well-formed entry.

    Validation is intentionally loose - we accept any JSON-serialisable
    ``value`` - but the structural keys must be the right type so a
    downstream consumer never has to defensively coerce them.
    """
    required = {"key", "scope", "value", "owner_agent_id", "ts_ns"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"whiteboard entry missing fields: {sorted(missing)}")
    for str_field in ("key", "scope", "owner_agent_id"):
        if not isinstance(raw[str_field], str) or not raw[str_field]:
            raise ValueError(
                f"whiteboard entry '{str_field}' must be a non-empty string",
            )
    if not isinstance(raw["ts_ns"], int):
        raise ValueError("whiteboard entry 'ts_ns' must be an int")
    visibility = raw.get("visibility", [])
    if visibility is None:
        return
    if not isinstance(visibility, list) or any(not isinstance(item, str) for item in visibility):
        raise ValueError("whiteboard entry 'visibility' must be a list of strings")


class Whiteboard:
    """Append-only JSONL whiteboard for a single run.

    The whiteboard lives at ``<root>/<run_id>/whiteboard.jsonl``.
    Concurrent writers are linearised via a per-instance lock plus a
    POSIX advisory lock on the file handle, which is a cheap way to
    keep multi-process tests deterministic without pulling in a real
    coordination service.

    Example:
        >>> wb = Whiteboard(root, run_id="run-1")
        >>> wb.append(WhiteboardEntry(
        ...     key="api_contract",
        ...     scope="backend",
        ...     value={"version": 1},
        ...     owner_agent_id="agent-backend-1",
        ...     visibility=("backend", "qa"),
        ... ))
        >>> entries = wb.read(reader_role="qa")
    """

    def __init__(self, root: Path, run_id: str) -> None:
        """Bind the whiteboard to ``<root>/<run_id>/whiteboard.jsonl``.

        Args:
            root: Directory that hosts per-run subfolders. Conventionally
                ``.bernstein/runs`` inside the active workspace, but the
                caller chooses - nothing in this module assumes the
                Bernstein path layout, which keeps it usable from tests
                and from one-off scripts.
            run_id: Identifier for the current run. Must not contain
                path separators; we still defensively reject them.
        """
        if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
            raise ValueError(f"invalid run_id: {run_id!r}")
        self._run_dir = root / run_id
        self._path = self._run_dir / _WHITEBOARD_FILENAME
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Filesystem path to the underlying JSONL file."""
        return self._path

    def append(self, entry: WhiteboardEntry) -> None:
        """Append *entry* as a single JSON line.

        Concurrent writers from the same process are serialised by
        ``self._lock``; cross-process writers contend for ``LOCK_EX``
        on the file descriptor. The append is followed by ``fsync`` to
        keep the on-disk record durable.
        """
        line = entry.to_json_line() + "\n"
        with self._lock:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            # 0o600 - consistent with atomic_write defaults; runtime state
            # may carry task metadata that should not be world-readable.
            fd = os.open(
                str(self._path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                _flock_exclusive(fd)
                try:
                    os.write(fd, line.encode("utf-8"))
                    with contextlib.suppress(OSError):
                        os.fsync(fd)
                finally:
                    _flock_release(fd)
            finally:
                os.close(fd)

    def read(
        self,
        reader_role: str,
        *,
        scope: str | None = None,
        key: str | None = None,
    ) -> list[WhiteboardEntry]:
        """Return entries visible to *reader_role*, optionally filtered.

        Args:
            reader_role: Role label of the reading agent. Pass
                :data:`WILDCARD_ROLE` to bypass the visibility filter
                (only orchestrator-side debugging should do this).
            scope: If set, only return entries from this scope.
            key: If set, only return entries whose ``key`` matches.

        Returns:
            Entries in file order - i.e. write order, since each line
            is appended atomically. Malformed lines are skipped and
            logged at warning level so a single bad record does not
            poison every reader.
        """
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as fh:
            return list(self._iter_filtered(fh, reader_role, scope, key))

    def iter_visible(
        self,
        reader_role: str,
        *,
        scope: str | None = None,
        key: str | None = None,
    ) -> Iterator[WhiteboardEntry]:
        """Stream visible entries one at a time.

        Useful when the whiteboard grows beyond what callers want to
        materialise at once. Same semantics as :meth:`read`, just
        lazy.
        """
        if not self._path.exists():
            return
        # The file is opened eagerly so the descriptor lifetime is
        # bound to the iterator generator; closing happens when the
        # generator is exhausted or garbage-collected.
        fh = self._path.open("r", encoding="utf-8")
        try:
            yield from self._iter_filtered(fh, reader_role, scope, key)
        finally:
            fh.close()

    def _iter_filtered(
        self,
        fh: Iterable[str],
        reader_role: str,
        scope: str | None,
        key: str | None,
    ) -> Iterator[WhiteboardEntry]:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                entry = WhiteboardEntry.from_dict(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "skipping malformed whiteboard line in %s: %s",
                    self._path,
                    exc,
                )
                continue
            if scope is not None and entry.scope != scope:
                continue
            if key is not None and entry.key != key:
                continue
            if not _is_visible(entry, reader_role):
                continue
            yield entry


def _is_visible(entry: WhiteboardEntry, reader_role: str) -> bool:
    """Return whether *reader_role* may see *entry*.

    Visibility rules:

    * Empty ``visibility`` list - entry is public within the run.
    * ``reader_role == WILDCARD_ROLE`` - bypass filter.
    * Otherwise the role must appear in ``entry.visibility``.

    The owning agent does not get an automatic free pass on the
    grounds that visibility is *the* place to encode "this entry is
    private", and a writer that wants to keep the record visible to
    itself should add its own role to ``visibility`` explicitly. That
    keeps the rule stateless and easy to reason about in tests.
    """
    if reader_role == WILDCARD_ROLE:
        return True
    if not entry.visibility:
        return True
    return reader_role in entry.visibility


def _flock_exclusive(fd: int) -> None:
    """Best-effort exclusive advisory lock on *fd*.

    On platforms without :mod:`fcntl` (Windows) we fall through
    silently; the per-instance threading lock still serialises writers
    inside one process.
    """
    try:
        import fcntl  # platform-conditional import
    except ImportError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOLCK, errno.ENOTSUP}:
            logger.debug("flock not supported on this fd: %s", exc)
            return
        raise


def _flock_release(fd: int) -> None:
    """Release the advisory lock acquired by :func:`_flock_exclusive`."""
    try:
        import fcntl
    except ImportError:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "WILDCARD_ROLE",
    "Whiteboard",
    "WhiteboardEntry",
]
