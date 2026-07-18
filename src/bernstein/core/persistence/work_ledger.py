"""Durable work ledger: a hash-chained record of the task graph (#2358).

Run state (WAL, per-step journal, worktrees) is machine-local. A crash on
the same host is recoverable, but an in-flight goal could not move to
another machine, be handed to a colleague, or survive a reimage. The work
ledger closes that gap: a compact, portable representation of the task
graph and every state transition, where each entry links its predecessor's
hash. The ledger IS the resumable state -- ``bernstein ledger resume``
rebuilds scheduler state purely by replaying the chain, so tampering or
divergence surfaces as a hash mismatch at an exact position, never as a
silent merge.

Entry hashing contract (load-bearing)
-------------------------------------
Each entry is hashed with::

    entry_hash = SHA256(
        canonical_json({
            "kind":      <transition kind, e.g. "task.completed">,
            "payload":   <redacted JSON payload dict>,
            "prev_hash": <entry_hash of entry N-1, or "0"*64 for genesis>,
            "task_id":   <task the transition concerns, "" for run-level>,
        })
    )

``canonical_json`` is ``json.dumps(..., sort_keys=True,
separators=(",", ":"))`` encoded as UTF-8 -- the exact contract the
per-step replay journal uses (:mod:`bernstein.core.persistence.journal`),
so a verifier that can walk one chain can walk the other. ``seq``, ``ts``,
``redactions``, and ``schema_version`` are row metadata and are **never**
part of the hash, exactly like ``ts`` in the journal.

Redaction before persistence (load-bearing)
-------------------------------------------
The ledger is designed to travel (git ref, clone, handoff), so the
redaction layer runs *before* any entry is hashed or written: every string
in the payload passes through
:func:`bernstein.core.security.redactor.redact_text`. Because the hash is
computed over the redacted payload, the portable chain verifies everywhere
without ever carrying the cleartext. The number of redactions applied is
kept as (unhashed) row metadata for observability.

Storage layout
--------------
``<sdd_dir>/runtime/ledger/<run-id>/000000.jsonl`` -- one JSON object per
line, one line per transition, append-only. Recovery on open revalidates
every entry hash from genesis and fails closed on interior corruption; a
torn trailing line (crash mid-write) degrades gracefully to the last
validated entry. This mirrors the replay journal's recovery contract so
both chains behave identically under a hard kill.

Divergence
----------
Two chains that share a prefix and then differ are *diverged*: two heads
referencing the same parent entry. :func:`compare_chains` names the exact
fork position and both heads; import/anchor paths refuse to proceed on a
diverged pair rather than merging silently. A resume appends an explicit
``run.resumed`` entry carrying a fresh nonce, so two independent resumes
of the same chain become structurally divergent at the very next entry
and are caught at the next anchor or fetch.

Git-ref anchoring lives in :mod:`bernstein.core.persistence.ledger_git`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.journal import GENESIS_HASH
from bernstein.core.security.redactor import redact_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Ledger row schema version. Metadata only -- never hashed.
LEDGER_SCHEMA_VERSION = 1

#: Well-known transition kinds. The replay projection understands these;
#: unknown kinds are carried through untouched so a ledger written by a
#: newer writer (e.g. a scheduler daemon) still replays on an older reader.
KIND_RUN_OPEN = "run.open"
KIND_RUN_RESUMED = "run.resumed"
KIND_RUN_CLOSED = "run.closed"
KIND_TASK_SCHEDULED = "task.scheduled"
KIND_TASK_STARTED = "task.started"
KIND_TASK_COMPLETED = "task.completed"
KIND_TASK_FAILED = "task.failed"
KIND_TASK_ABANDONED = "task.abandoned"

#: Durable suspension transitions (#2552). A ``task.suspended`` entry persists
#: an operator park through orchestrator restarts and daemon crashes the same
#: way detached run state survives; ``task.resumed`` records the durable
#: revival. A parked task is deliberately excluded from :meth:`resume_frontier`
#: -- it waits for an explicit ``bernstein task resume`` (or its ``--until``
#: wake), never an auto-restart. A reader that predates these kinds still
#: replays the chain (they count as unknown task kinds).
KIND_TASK_SUSPENDED = "task.suspended"
KIND_TASK_RESUMED = "task.resumed"

#: Mission transition kinds (#2509). A mission is a ledger-projected multi-day
#: goal; these transitions are the only mission state that touches disk -- the
#: mission status is a pure projection over them (see
#: :mod:`bernstein.core.orchestration.missions`). The replay projection above
#: treats them as unknown task kinds (counted, otherwise ignored), so a mission
#: ledger still resumes on a reader that predates missions.
KIND_MISSION_DEFINED = "mission.defined"
KIND_MISSION_PHASE_ENTERED = "mission.phase_entered"
KIND_MISSION_PHASE_PASSED = "mission.phase_passed"
KIND_MISSION_PHASE_HALTED = "mission.phase_halted"

#: Default bucket filename; the layout matches the replay journal so a
#: future compaction pass can roll ``<n>.jsonl`` files for both.
_DEFAULT_BUCKET = "000000.jsonl"

#: Transition kinds must be lowercase dotted identifiers.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

#: Task ids share the git-ref-safe alphabet used across the persistence
#: layer (worktree ids, snapshot ids).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LedgerError(RuntimeError):
    """Raised for unrecoverable ledger read/write/verify errors."""


# ---------------------------------------------------------------------------
# Canonical entry encoding (the public contract)
# ---------------------------------------------------------------------------


def canonical_entry_payload(
    *,
    prev_hash: str,
    kind: str,
    task_id: str,
    payload: dict[str, Any],
) -> bytes:
    """Return the canonical UTF-8 bytes the entry hash is taken over.

    See the module docstring for the contract; this function is the single
    source of truth. A third-party verifier re-derives any entry hash by
    building this document and hashing it with SHA-256.
    """
    document = {
        "kind": kind,
        "payload": payload,
        "prev_hash": prev_hash,
        "task_id": task_id,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_entry_hash(
    *,
    prev_hash: str,
    kind: str,
    task_id: str,
    payload: dict[str, Any],
) -> str:
    """Return the SHA-256 hex digest of the canonical entry payload."""
    return hashlib.sha256(
        canonical_entry_payload(
            prev_hash=prev_hash,
            kind=kind,
            task_id=task_id,
            payload=payload,
        )
    ).hexdigest()


# ---------------------------------------------------------------------------
# Redaction on the write path
# ---------------------------------------------------------------------------


def _redact_value(value: Any) -> tuple[Any, int]:
    """Recursively redact secrets from *value*; return ``(clean, count)``.

    Strings pass through :func:`redact_text`; dicts and lists are walked
    recursively; scalars pass through untouched. The traversal is applied
    before hashing so the persisted chain never references cleartext.
    """
    if isinstance(value, str):
        cleaned, count = redact_text(value)
        return cleaned, count
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            cleaned, count = _redact_value(item)
            out[key] = cleaned
            total += count
        return out, total
    if isinstance(value, list):
        total = 0
        items: list[Any] = []
        for item in value:
            cleaned, count = _redact_value(item)
            items.append(cleaned)
            total += count
        return items, total
    return value, 0


def _redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Redact every string in *payload*; return ``(clean, redactions)``."""
    cleaned, count = _redact_value(payload)
    if not isinstance(cleaned, dict):  # pragma: no cover -- type guard
        msg = "payload redaction must preserve the dict shape"
        raise LedgerError(msg)
    return cleaned, count


def redact_ledger_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* as :meth:`WorkLedger.append` will persist it.

    Redaction runs before hashing on the write path, so a payload that binds
    its own content hash must hash the redacted form or the persisted hash will
    disagree with anything a reader recomputes. Redaction is idempotent, so a
    payload already passed through here survives :meth:`WorkLedger.append`
    unchanged.
    """
    cleaned, _ = _redact_payload(payload)
    return cleaned


# ---------------------------------------------------------------------------
# Dataclass: one entry in the ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """One task-graph transition in the hash-chained work ledger.

    Attributes mirror the canonical-entry contract one-to-one. ``seq`` is a
    monotonically increasing integer starting at 0; ``ts`` is unix epoch
    seconds at write time; ``redactions`` counts secrets scrubbed from the
    payload before hashing. All three are metadata -- never hashed.
    """

    seq: int
    prev_hash: str
    kind: str
    task_id: str
    payload: dict[str, Any]
    entry_hash: str
    ts: float
    redactions: int = 0
    schema_version: int = LEDGER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of the row."""
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "kind": self.kind,
            "task_id": self.task_id,
            "payload": self.payload,
            "entry_hash": self.entry_hash,
            "ts": self.ts,
            "redactions": self.redactions,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEntry:
        """Build an entry from a deserialised dict row."""
        payload = raw.get("payload")
        return cls(
            seq=int(raw["seq"]),
            prev_hash=str(raw["prev_hash"]),
            kind=str(raw["kind"]),
            task_id=str(raw.get("task_id", "")),
            payload=payload if isinstance(payload, dict) else {},
            entry_hash=str(raw["entry_hash"]),
            ts=float(raw.get("ts", 0.0)),
            redactions=int(raw.get("redactions", 0)),
            schema_version=int(raw.get("schema_version", LEDGER_SCHEMA_VERSION)),
        )

    def canonical_line(self) -> str:
        """Return the canonical single-line JSON form of the row."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerVerification:
    """Outcome of :meth:`LedgerReader.verify`.

    Attributes:
        ok: True when every entry parses and the chain matches.
        head_hash: The tail entry hash discovered while walking the chain.
        entries: Number of entries successfully walked.
        errors: Human-readable messages naming the exact entry position
            (``entry <seq> (line <n>)``) of every fault.
    """

    ok: bool
    head_hash: str
    entries: int
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Chain walking (shared by recovery, reader, and verifier)
# ---------------------------------------------------------------------------


def _parse_ledger_row(stripped: str) -> dict[str, Any] | None:
    """Parse one stripped line into a ledger row, or ``None`` if malformed."""
    try:
        row: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict) or "entry_hash" not in row:
        return None
    return row


def _row_position(seq: int, line_no: int) -> str:
    """Return the canonical position phrase for error messages."""
    return f"entry {seq} (line {line_no})"


def _check_row(
    row: dict[str, Any],
    *,
    line_no: int,
    expected_seq: int,
    prev_hash: str,
) -> list[str]:
    """Return chain errors for *row*, empty when the row verifies."""
    errors: list[str] = []
    raw_seq = row.get("seq", -1)
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError):
        seq = -1
    position = _row_position(seq if seq >= 0 else expected_seq, line_no)
    if seq != expected_seq:
        errors.append(f"{position}: seq mismatch (expected {expected_seq}, got {raw_seq!r})")

    stored_prev = str(row.get("prev_hash", ""))
    if stored_prev != prev_hash:
        errors.append(f"{position}: prev_hash mismatch (expected {prev_hash[:16]}..., got {stored_prev[:16]}...)")

    payload = row.get("payload")
    recomputed = compute_entry_hash(
        prev_hash=stored_prev,
        kind=str(row.get("kind", "")),
        task_id=str(row.get("task_id", "")),
        payload=payload if isinstance(payload, dict) else {},
    )
    stored_hash = str(row.get("entry_hash", ""))
    if recomputed != stored_hash:
        errors.append(
            f"{position}: entry_hash mismatch (recomputed {recomputed[:16]}..., stored {stored_hash[:16]}...)"
        )
    return errors


def _walk_validated_lines(bucket_path: Path) -> tuple[str, list[str]]:
    """Walk *bucket_path*, revalidate the chain, and collect canonical lines.

    Returns ``(tip_hash, validated_lines)``. Fail-closed contract mirrors
    the replay journal: a parseable row whose chain does not verify raises
    :class:`LedgerError` naming the offending position; a torn/unparseable
    trailing line (crash mid-write) is tolerated only when nothing
    well-formed follows it.
    """
    prev_hash = GENESIS_HASH
    expected_seq = 0
    validated: list[str] = []
    pending_torn_line: int | None = None

    with bucket_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue

            row = _parse_ledger_row(stripped)
            if row is None:
                if pending_torn_line is None:
                    pending_torn_line = line_no
                continue

            if pending_torn_line is not None:
                msg = (
                    f"ledger {bucket_path}: line {pending_torn_line} is corrupt but "
                    f"line {line_no} continues the chain; refusing to recover across "
                    f"a broken interior row. Move the ledger aside to recover."
                )
                raise LedgerError(msg)

            errors = _check_row(row, line_no=line_no, expected_seq=expected_seq, prev_hash=prev_hash)
            if errors:
                msg = f"ledger {bucket_path}: {errors[0]}. Move the ledger aside to recover."
                raise LedgerError(msg)

            prev_hash = str(row["entry_hash"])
            expected_seq += 1
            validated.append(stripped)

    if pending_torn_line is not None:
        logger.warning(
            "ledger %s: torn trailing line %d ignored; recovered %d validated entries",
            bucket_path,
            pending_torn_line,
            len(validated),
        )

    return prev_hash, validated


def _validate_chain_for_recovery(bucket_path: Path) -> tuple[str, int]:
    """Return ``(tip_hash, validated_count)`` for writer tip recovery."""
    tip_hash, validated = _walk_validated_lines(bucket_path)
    return tip_hash, len(validated)


def validated_canonical_lines(ledger_dir: Path) -> tuple[list[str], str]:
    """Return the validated canonical lines and head hash for *ledger_dir*.

    The export surface (git-ref anchoring) uses this to capture exactly the
    bytes the chain verified over: a torn trailing line from a crash is
    excluded (matching writer recovery), while interior corruption raises
    :class:`LedgerError` so a broken chain is never exported.
    """
    bucket_path = ledger_dir / _DEFAULT_BUCKET
    if not bucket_path.exists():
        return [], GENESIS_HASH
    tip_hash, validated = _walk_validated_lines(bucket_path)
    return validated, tip_hash


def verify_entry_rows(rows: Iterable[dict[str, Any]]) -> LedgerVerification:
    """Verify a chain of already-parsed rows (e.g. read back from a git ref).

    Reuses the same per-row check the file verifier runs, so there is no
    second hashing scheme to drift against :meth:`LedgerReader.verify`.
    Positions are reported as ``entry <seq> (line <n>)`` where ``<n>`` is
    the 1-based index within the supplied sequence.
    """
    errors: list[str] = []
    prev_hash = GENESIS_HASH
    entries = 0
    for index, row in enumerate(rows):
        errors.extend(_check_row(row, line_no=index + 1, expected_seq=index, prev_hash=prev_hash))
        prev_hash = str(row.get("entry_hash", ""))
        entries += 1
    return LedgerVerification(ok=not errors, head_hash=prev_hash, entries=entries, errors=errors)


# ---------------------------------------------------------------------------
# WorkLedger (writer)
# ---------------------------------------------------------------------------


class WorkLedger:
    """Append-only hash-chained ledger for one run's task graph.

    Use :meth:`WorkLedger.open` to obtain an instance. Single-writer at the
    file level (the scheduler owns a run's ledger for its lifetime); the
    in-process lock makes concurrent :meth:`append` calls from fan-out
    threads safe, mirroring the replay journal's atomicity notes.
    """

    __slots__ = ("_bucket_path", "_closed", "_dir", "_lock", "_seq", "_tip_hash")

    def __init__(self, ledger_dir: Path) -> None:
        self._dir = ledger_dir
        self._bucket_path = ledger_dir / _DEFAULT_BUCKET
        self._lock = threading.Lock()
        self._tip_hash = GENESIS_HASH
        self._seq = 0
        self._closed = False

    # -- factories -----------------------------------------------------------

    @classmethod
    def open(cls, ledger_dir: Path) -> WorkLedger:
        """Open (or create) the ledger rooted at *ledger_dir*.

        Recovery revalidates every entry hash from genesis and fails closed
        on interior corruption; a torn trailing line degrades gracefully to
        the last validated entry.
        """
        ledger_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            # Owner-only directory; payloads may describe private work.
            with contextlib.suppress(OSError):
                ledger_dir.chmod(0o700)

        ledger = cls(ledger_dir)
        ledger._recover_tail()
        return ledger

    # -- properties ----------------------------------------------------------

    @property
    def head_hash(self) -> str:
        """Latest entry hash, or :data:`GENESIS_HASH` when empty."""
        return self._tip_hash

    @property
    def next_seq(self) -> int:
        """The ``seq`` the next :meth:`append` would assign."""
        return self._seq

    @property
    def ledger_dir(self) -> Path:
        """The on-disk directory backing this ledger."""
        return self._dir

    @property
    def bucket_path(self) -> Path:
        """Path to the current bucket file."""
        return self._bucket_path

    # -- write ----------------------------------------------------------------

    def append(
        self,
        *,
        kind: str,
        task_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        """Append a transition to the chain and return the persisted entry.

        The payload is passed through the redaction layer *before* the
        entry hash is computed, so the persisted chain verifies everywhere
        without ever carrying a secret.

        Raises:
            LedgerError: On a malformed kind/task id, a closed ledger, or a
                failed file write.
        """
        if self._closed:
            msg = f"ledger at {self._dir} is closed"
            raise LedgerError(msg)
        if not _KIND_RE.match(kind):
            msg = f"invalid ledger kind {kind!r}: must match {_KIND_RE.pattern}"
            raise LedgerError(msg)
        if not _TASK_ID_RE.match(task_id):
            msg = f"invalid task_id {task_id!r}: must match {_TASK_ID_RE.pattern}"
            raise LedgerError(msg)

        clean_payload, redactions = _redact_payload(dict(payload or {}))

        with self._lock:
            prev_hash = self._tip_hash
            seq = self._seq
            entry_hash = compute_entry_hash(
                prev_hash=prev_hash,
                kind=kind,
                task_id=task_id,
                payload=clean_payload,
            )
            entry = LedgerEntry(
                seq=seq,
                prev_hash=prev_hash,
                kind=kind,
                task_id=task_id,
                payload=clean_payload,
                entry_hash=entry_hash,
                ts=time.time(),
                redactions=redactions,
            )
            try:
                with self._bucket_path.open("a", encoding="utf-8", newline="") as fh:
                    fh.write(entry.canonical_line() + "\n")
                if os.name == "posix":
                    with contextlib.suppress(OSError):
                        self._bucket_path.chmod(0o600)
            except OSError as exc:
                msg = f"ledger append failed: {exc}"
                raise LedgerError(msg) from exc

            self._tip_hash = entry_hash
            self._seq = seq + 1
            return entry

    # -- close ----------------------------------------------------------------

    def close(self) -> None:
        """Mark the ledger as closed; future :meth:`append` calls raise."""
        self._closed = True

    def __enter__(self) -> WorkLedger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internal ---------------------------------------------------------------

    def _recover_tail(self) -> None:
        """Recover ``tip_hash`` and ``seq`` by revalidating the chain."""
        if not self._bucket_path.exists():
            return
        try:
            tip_hash, validated = _validate_chain_for_recovery(self._bucket_path)
        except OSError as exc:
            msg = f"ledger recovery failed: {exc}"
            raise LedgerError(msg) from exc
        self._tip_hash = tip_hash
        self._seq = validated


# ---------------------------------------------------------------------------
# LedgerReader (read-only)
# ---------------------------------------------------------------------------


class LedgerReader:
    """Read-only view over a persisted work ledger."""

    __slots__ = ("_bucket_path", "_dir")

    def __init__(self, ledger_dir: Path) -> None:
        self._dir = ledger_dir
        self._bucket_path = ledger_dir / _DEFAULT_BUCKET

    @property
    def ledger_dir(self) -> Path:
        return self._dir

    @property
    def bucket_path(self) -> Path:
        return self._bucket_path

    def exists(self) -> bool:
        """Return ``True`` when a bucket file is present on disk."""
        return self._bucket_path.exists()

    def entries(self) -> Iterator[LedgerEntry]:
        """Yield every well-formed entry in ``seq`` order."""
        if not self._bucket_path.exists():
            return
        with self._bucket_path.open(encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                row = _parse_ledger_row(stripped)
                if row is None:
                    # Torn tail from a crash mid-write; prior entries stay
                    # inspectable.
                    continue
                yield LedgerEntry.from_dict(row)

    def verify(self, expected_head: str | None = None) -> LedgerVerification:
        """Walk the chain and recompute every entry hash.

        Args:
            expected_head: When supplied, additionally require the tail's
                ``entry_hash`` to equal this value -- the check a resume
                runs against an anchored head.

        Returns:
            A :class:`LedgerVerification`; every error names the exact
            entry position (``entry <seq> (line <n>)``).
        """
        errors: list[str] = []
        prev_hash = GENESIS_HASH
        entries = 0
        expected_seq = 0

        if not self._bucket_path.exists():
            ok = expected_head in (None, GENESIS_HASH)
            if not ok:
                errors.append(f"no ledger file at {self._bucket_path}; expected head {expected_head!r}")
            return LedgerVerification(ok=ok, head_hash=GENESIS_HASH, entries=0, errors=errors)

        with self._bucket_path.open(encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                row = _parse_ledger_row(stripped)
                if row is None:
                    errors.append(f"entry {expected_seq} (line {line_no}): not a parseable ledger row")
                    continue
                errors.extend(_check_row(row, line_no=line_no, expected_seq=expected_seq, prev_hash=prev_hash))
                prev_hash = str(row.get("entry_hash", ""))
                entries += 1
                expected_seq += 1

        if expected_head is not None and expected_head != prev_hash:
            errors.append(f"head mismatch: expected {expected_head[:16]}..., got {prev_hash[:16]}...")

        return LedgerVerification(ok=not errors, head_hash=prev_hash, entries=entries, errors=errors)


# ---------------------------------------------------------------------------
# Deterministic replay projection
# ---------------------------------------------------------------------------

#: Task states the projection can assign, in lifecycle order.
_STATE_SCHEDULED = "scheduled"
_STATE_STARTED = "started"
_STATE_COMPLETED = "completed"
_STATE_FAILED = "failed"
_STATE_ABANDONED = "abandoned"
_STATE_SUSPENDED = "suspended"

_TASK_KIND_TO_STATE = {
    KIND_TASK_SCHEDULED: _STATE_SCHEDULED,
    KIND_TASK_STARTED: _STATE_STARTED,
    KIND_TASK_COMPLETED: _STATE_COMPLETED,
    KIND_TASK_FAILED: _STATE_FAILED,
    KIND_TASK_ABANDONED: _STATE_ABANDONED,
    # A durable park moves the task to ``suspended``; a durable resume moves it
    # back to ``started`` (it is in-flight again). ``attempts`` is only bumped
    # by ``task.started``, so a park/resume pair does not inflate the count.
    KIND_TASK_SUSPENDED: _STATE_SUSPENDED,
    KIND_TASK_RESUMED: _STATE_STARTED,
}


@dataclass
class TaskState:
    """Projected state of one task after replaying the chain."""

    task_id: str
    state: str
    attempts: int = 0
    last_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "attempts": self.attempts,
            "last_seq": self.last_seq,
        }


@dataclass
class LedgerState:
    """Deterministic projection of a work ledger onto scheduler state.

    Two replays of the same chain produce byte-identical canonical JSON
    (:meth:`to_canonical_json`): the projection depends only on hashed
    entry fields plus ``seq``, never on timestamps or host state.
    """

    run_id: str = ""
    head_hash: str = GENESIS_HASH
    entries: int = 0
    tasks: dict[str, TaskState] = field(default_factory=dict)
    run_open: bool = False
    run_closed: bool = False
    resumes: int = 0
    unknown_kinds: int = 0

    def _tasks_in_state(self, state: str) -> list[str]:
        return sorted(task_id for task_id, task in self.tasks.items() if task.state == state)

    @property
    def completed_tasks(self) -> list[str]:
        """Task ids whose latest transition is ``task.completed``."""
        return self._tasks_in_state(_STATE_COMPLETED)

    @property
    def in_flight_tasks(self) -> list[str]:
        """Task ids started but never completed/failed/abandoned."""
        return self._tasks_in_state(_STATE_STARTED)

    @property
    def scheduled_tasks(self) -> list[str]:
        """Task ids scheduled but never started."""
        return self._tasks_in_state(_STATE_SCHEDULED)

    @property
    def failed_tasks(self) -> list[str]:
        """Task ids whose latest transition is ``task.failed``."""
        return self._tasks_in_state(_STATE_FAILED)

    @property
    def suspended_tasks(self) -> list[str]:
        """Task ids whose latest transition is ``task.suspended`` (#2552).

        A parked task survives orchestrator restarts through this projection
        but is deliberately kept out of :meth:`resume_frontier`: it resumes
        only on an explicit ``bernstein task resume`` (or its ``--until``
        wake), never on an auto-restart.
        """
        return self._tasks_in_state(_STATE_SUSPENDED)

    def resume_frontier(self) -> list[str]:
        """Task ids a resume should (re)start: in-flight, then scheduled.

        Suspended tasks are excluded on purpose (see :attr:`suspended_tasks`):
        an operator park is woken explicitly, not by an orchestrator restart.
        """
        return self.in_flight_tasks + self.scheduled_tasks

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "head_hash": self.head_hash,
            "entries": self.entries,
            "run_open": self.run_open,
            "run_closed": self.run_closed,
            "resumes": self.resumes,
            "unknown_kinds": self.unknown_kinds,
            "tasks": {task_id: task.to_dict() for task_id, task in sorted(self.tasks.items())},
            "completed_tasks": self.completed_tasks,
            "in_flight_tasks": self.in_flight_tasks,
            "scheduled_tasks": self.scheduled_tasks,
            "failed_tasks": self.failed_tasks,
            "suspended_tasks": self.suspended_tasks,
            "resume_frontier": self.resume_frontier(),
        }

    def to_canonical_json(self) -> str:
        """Return the canonical (sorted, compact) JSON form of the state."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def replay_state(entries: Iterable[LedgerEntry], *, run_id: str = "") -> LedgerState:
    """Rebuild scheduler state by replaying *entries* in chain order.

    The projection is a pure function of the chain: run-level kinds toggle
    the run flags, task-level kinds move the named task through its
    lifecycle, and unknown kinds are counted but otherwise ignored (forward
    compatibility with newer writers).
    """
    state = LedgerState(run_id=run_id)
    for entry in entries:
        state.head_hash = entry.entry_hash
        state.entries += 1
        if entry.kind == KIND_RUN_OPEN:
            state.run_open = True
            raw_run_id = entry.payload.get("run_id")
            if not state.run_id and isinstance(raw_run_id, str):
                state.run_id = raw_run_id
            continue
        if entry.kind == KIND_RUN_RESUMED:
            state.resumes += 1
            continue
        if entry.kind == KIND_RUN_CLOSED:
            state.run_closed = True
            continue
        task_state = _TASK_KIND_TO_STATE.get(entry.kind)
        if task_state is None:
            state.unknown_kinds += 1
            continue
        task = state.tasks.get(entry.task_id)
        if task is None:
            task = TaskState(task_id=entry.task_id, state=task_state)
            state.tasks[entry.task_id] = task
        task.state = task_state
        task.last_seq = entry.seq
        if entry.kind == KIND_TASK_STARTED:
            task.attempts += 1
    return state


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainRelation:
    """How two chains sharing a genesis relate to each other.

    Attributes:
        relation: ``"identical"`` | ``"local-ahead"`` | ``"remote-ahead"``
            | ``"diverged"``.
        fork_seq: On divergence, the seq of the first differing entry (the
            two heads reference the same parent at ``fork_seq - 1``).
        local_head: Tail hash of the local chain.
        remote_head: Tail hash of the remote chain.
        local_entries: Entry count of the local chain.
        remote_entries: Entry count of the remote chain.
    """

    relation: str
    fork_seq: int | None
    local_head: str
    remote_head: str
    local_entries: int
    remote_entries: int


def compare_chains(
    local: Iterable[LedgerEntry],
    remote: Iterable[LedgerEntry],
) -> ChainRelation:
    """Compare two chains entry-by-entry and classify their relation.

    Divergence means both chains contain an entry at the same ``seq`` with
    different hashes -- two heads referencing the same parent entry. The
    fork position is exact, so the operator message can name it.
    """
    local_hashes = [entry.entry_hash for entry in local]
    remote_hashes = [entry.entry_hash for entry in remote]
    local_head = local_hashes[-1] if local_hashes else GENESIS_HASH
    remote_head = remote_hashes[-1] if remote_hashes else GENESIS_HASH

    shared = min(len(local_hashes), len(remote_hashes))
    for seq in range(shared):
        if local_hashes[seq] != remote_hashes[seq]:
            return ChainRelation(
                relation="diverged",
                fork_seq=seq,
                local_head=local_head,
                remote_head=remote_head,
                local_entries=len(local_hashes),
                remote_entries=len(remote_hashes),
            )

    if len(local_hashes) == len(remote_hashes):
        relation = "identical"
    elif len(local_hashes) > len(remote_hashes):
        relation = "local-ahead"
    else:
        relation = "remote-ahead"
    return ChainRelation(
        relation=relation,
        fork_seq=None,
        local_head=local_head,
        remote_head=remote_head,
        local_entries=len(local_hashes),
        remote_entries=len(remote_hashes),
    )


# ---------------------------------------------------------------------------
# Convenience: resolve ledger paths for an install
# ---------------------------------------------------------------------------


def default_ledger_root(sdd_dir: Path) -> Path:
    """Return ``<sdd_dir>/runtime/ledger`` (created on first use)."""
    root = sdd_dir / "runtime" / "ledger"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_ledger_dir(sdd_dir: Path, run_id: str) -> Path:
    """Return the per-run ledger directory under the default root."""
    return default_ledger_root(sdd_dir) / run_id


__all__ = [
    "GENESIS_HASH",
    "KIND_MISSION_DEFINED",
    "KIND_MISSION_PHASE_ENTERED",
    "KIND_MISSION_PHASE_HALTED",
    "KIND_MISSION_PHASE_PASSED",
    "KIND_RUN_CLOSED",
    "KIND_RUN_OPEN",
    "KIND_RUN_RESUMED",
    "KIND_TASK_ABANDONED",
    "KIND_TASK_COMPLETED",
    "KIND_TASK_FAILED",
    "KIND_TASK_RESUMED",
    "KIND_TASK_SCHEDULED",
    "KIND_TASK_STARTED",
    "KIND_TASK_SUSPENDED",
    "LEDGER_SCHEMA_VERSION",
    "ChainRelation",
    "LedgerEntry",
    "LedgerError",
    "LedgerReader",
    "LedgerState",
    "LedgerVerification",
    "TaskState",
    "WorkLedger",
    "canonical_entry_payload",
    "compare_chains",
    "compute_entry_hash",
    "default_ledger_root",
    "redact_ledger_payload",
    "replay_state",
    "run_ledger_dir",
    "validated_canonical_lines",
    "verify_entry_rows",
]
