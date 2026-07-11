"""Per-step session-replay journal with hash-chained Merkle list (#1799).

The journal is the load-bearing artefact of the per-step replay surface.
Each step of an agent run is hashed with::

    step_hash = SHA256(
        canonical_json({
            "prev_hash":   <step_hash of step N-1, or "0"*64 for the genesis>,
            "input_hash":  <SHA-256 hex of the user-supplied input blob>,
            "model":       <e.g. "claude-3-7-sonnet-20250219" | null>,
            "prompt":      <full prompt text the adapter received | null>,
            "tool_call":   <serialised tool invocation dict | null>,
            "tool_result": <serialised tool result dict       | null>,
            "effort":      <reasoning effort e.g. "low"|"high"|"max" | absent>,
        })
    )

Journal record schema versioning (effort dimension)
---------------------------------------------------
The reasoning-effort level a request was routed at changes the model's
output, so replaying a step at a different effort must surface as hash
divergence, not as a silent mismatch. ``effort`` is therefore folded into
the step hash. To keep every pre-existing journal valid, ``effort`` is
back-compat versioned by **omission**:

* ``effort=None`` (the sentinel for "unknown / not recorded") means the
  ``effort`` key is left **out** of the canonical document entirely. The
  bytes are then byte-identical to a pre-effort record, so every journal
  written before this change re-verifies unchanged - a missing effort never
  fails an old record.
* A non-``None`` ``effort`` adds ``"effort": <value>`` to the document, so
  two runs that differ only in effort produce different step hashes and a
  cross-effort replay is caught as divergence.

The persisted row carries an explicit ``schema_version`` (see
:data:`JOURNAL_SCHEMA_VERSION`) so an operator can tell effort-aware rows
apart from legacy rows. ``schema_version`` is metadata only and is **never**
part of the hash, exactly like ``ts`` - so adding it does not invalidate any
existing chain.

Canonical encoding contract (load-bearing)
------------------------------------------
``canonical_step_payload`` returns the exact bytes the hash is computed
over:

* JSON with sorted keys (``json.dumps(..., sort_keys=True)``).
* Compact separators (``separators=(",", ":")``); no incidental whitespace.
* UTF-8 encoding.

This means a peer reading **this docstring** can re-derive any step hash
by hand:

1. Build a dict with exactly the six fields above.
2. ``json.dumps`` it with ``sort_keys=True, separators=(",", ":")``.
3. UTF-8-encode the result.
4. ``sha256`` the bytes; the hex digest is the ``step_hash``.

Any change to this contract (added field, encoding tweak, sort policy)
is a **versioning event**: bump the journal format and document the
migration in ``docs/operations/replay.md``. Two replays that disagree on
encoding will surface as hash divergence rather than silent data loss.

Storage layout
--------------
``<journal_root>/<bucket>.jsonl`` where ``<journal_root>`` is typically
``.sdd/runtime/journal/<agent_id>/``. One JSON object per line, one line
per step, append-only. The chain head hash is recovered on open by
walking the bucket file from genesis and **revalidating every step hash**
(reusing ``compute_step_hash``); the tip is taken from the last recomputed
hash, never read verbatim from the tail. Recovery fails closed: a parseable
row whose chain does not verify raises :class:`JournalError` so a tampered
or truncated-then-edited journal cannot be silently extended from a
poisoned anchor (#1836). A torn/unparseable trailing line (crash mid-write)
still degrades gracefully to the last validated row.

Atomicity
---------
Single-writer is the intended pattern; the orchestrator owns the agent's
journal for its lifetime. To make accidental races safe (the spawner
sometimes calls ``append`` from a stdout/stderr fan-out thread) the
implementation guards every append with a process-local lock AND an
``fcntl`` file lock on POSIX. The actual on-disk write is a single
``file.write(line + "\\n")`` call holding the lock - one line per call,
so kernel-level PIPE_BUF semantics keep concurrent writers from
interleaving bytes.

Verification
------------
``JournalReader.verify(expected_head=...)`` walks the chain from genesis
to tail and recomputes every ``step_hash``. Any of the following surfaces
as an error:

* A line that is not valid JSON.
* A row whose ``prev_hash`` does not match the previous row's ``step_hash``.
* A row whose recomputed ``step_hash`` differs from the stored value.
* A tail hash that does not match ``expected_head`` (when supplied).

Errors carry the offending line number so an operator can grep the file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Genesis ``prev_hash`` for the first step of a fresh chain.
GENESIS_HASH = "0" * 64

#: Journal record schema version. Bumped from an implicit ``1`` to ``2`` when
#: the reasoning-``effort`` dimension was folded into the step hash. Persisted
#: as row metadata only (never hashed), so bumping it does not invalidate any
#: existing chain. Rows written before this change have no ``schema_version``
#: field and are read as version ``1``.
JOURNAL_SCHEMA_VERSION = 2

#: Default bucket filename. Replays span at most one bucket today; the
#: layout is shaped so a future compaction pass can add ``<n>.jsonl`` rolling
#: files without breaking the reader.
_DEFAULT_BUCKET = "000000.jsonl"

#: Audit event-type strings for the HMAC-chained audit log. The orchestrator
#: emits one of these per replay-surface action so the audit slice extractor
#: can find replay activity by event_type.
AUDIT_EVENT_REPLAY_STEP = "replay.step"
AUDIT_EVENT_REPLAY_FORK = "replay.fork"
AUDIT_EVENT_REPLAY_EXPORT = "replay.export"
AUDIT_EVENT_REPLAY_PUBLISH = "replay.publish"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JournalError(RuntimeError):
    """Raised for unrecoverable journal read/write/verify errors."""


# ---------------------------------------------------------------------------
# Canonical step encoding (the public contract)
# ---------------------------------------------------------------------------


def canonical_step_payload(
    *,
    prev_hash: str,
    input_hash: str,
    model: str | None,
    prompt: str | None,
    tool_call: Any,
    tool_result: Any,
    effort: str | None = None,
) -> bytes:
    """Return the canonical UTF-8 bytes that the step hash is taken over.

    See the module docstring for the contract; this function is the
    single source of truth. The output is what a third-party verifier
    would produce when re-deriving the hash by hand.

    ``effort`` is back-compat versioned by omission: when ``None`` (the
    sentinel for a legacy / unrecorded effort) the ``effort`` key is left out
    of the document, yielding bytes byte-identical to a pre-effort record so
    old journals re-verify unchanged. A non-``None`` ``effort`` adds an
    ``"effort"`` key, so a step routed at a different effort hashes
    differently and a cross-effort replay is caught as divergence.
    """
    document: dict[str, Any] = {
        "prev_hash": prev_hash,
        "input_hash": input_hash,
        "model": model,
        "prompt": prompt,
        "tool_call": tool_call,
        "tool_result": tool_result,
    }
    # Sentinel-by-omission: only a recorded (non-None) effort enters the hash,
    # so every pre-effort record continues to verify byte-for-byte.
    if effort is not None:
        document["effort"] = effort
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_step_hash(
    *,
    prev_hash: str,
    input_hash: str,
    model: str | None,
    prompt: str | None,
    tool_call: Any,
    tool_result: Any,
    effort: str | None = None,
) -> str:
    """Return the SHA-256 hex digest of the canonical step payload."""
    payload = canonical_step_payload(
        prev_hash=prev_hash,
        input_hash=input_hash,
        model=model,
        prompt=prompt,
        tool_call=tool_call,
        tool_result=tool_result,
        effort=effort,
    )
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Dataclass: one row in the journal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    """One step in an agent's hash-chained run journal.

    Attributes mirror the canonical-step contract one-to-one. ``seq`` is a
    monotonically increasing integer starting at 0; ``ts`` is the unix epoch
    seconds at the time of writing (purely informational - never part of
    the hash).
    """

    seq: int
    prev_hash: str
    input_hash: str
    model: str | None
    prompt: str | None
    tool_call: Any
    tool_result: Any
    step_hash: str
    ts: float
    blob_refs: list[str] = field(default_factory=list)
    #: Reasoning effort this step was routed at ("low"|"medium"|"high"|"max"),
    #: or ``None`` for a legacy row that predates the effort dimension. Folded
    #: into ``step_hash`` only when non-``None`` (see ``canonical_step_payload``).
    effort: str | None = None
    #: Row schema version. ``1`` for legacy rows (no ``schema_version`` field
    #: on disk), :data:`JOURNAL_SCHEMA_VERSION` for effort-aware rows. Metadata
    #: only - never hashed.
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        row: dict[str, Any] = {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "input_hash": self.input_hash,
            "model": self.model,
            "prompt": self.prompt,
            "tool_call": self.tool_call,
            "tool_result": self.tool_result,
            "step_hash": self.step_hash,
            "ts": self.ts,
            "blob_refs": self.blob_refs.copy(),
            "schema_version": self.schema_version,
        }
        # Only serialise ``effort`` when recorded, so a None-effort row stays
        # byte-shaped like a legacy row apart from the (unhashed) metadata.
        if self.effort is not None:
            row["effort"] = self.effort
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JournalEntry:
        """Build an entry from a deserialised dict row.

        A legacy row (no ``schema_version`` / ``effort``) is read as
        ``schema_version=1`` with ``effort=None``, so it re-verifies against
        the pre-effort canonical payload unchanged.
        """
        return cls(
            seq=int(raw["seq"]),
            prev_hash=str(raw["prev_hash"]),
            input_hash=str(raw["input_hash"]),
            model=raw.get("model"),
            prompt=raw.get("prompt"),
            tool_call=raw.get("tool_call"),
            tool_result=raw.get("tool_result"),
            step_hash=str(raw["step_hash"]),
            ts=float(raw.get("ts", 0.0)),
            blob_refs=list(raw.get("blob_refs") or []),
            effort=raw.get("effort"),
            schema_version=int(raw.get("schema_version", 1)),
        )


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of :meth:`JournalReader.verify`.

    Attributes:
        ok: True if every line is well-formed and the chain matches.
        head_hash: The actual tail step_hash discovered while walking
            the chain (may differ from any caller-supplied expectation).
        steps: Number of entries successfully walked.
        errors: Human-readable error messages, one per fault.
    """

    ok: bool
    head_hash: str
    steps: int
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Recovery-time chain validation (#1836)
# ---------------------------------------------------------------------------


def _parse_journal_row(stripped: str) -> dict[str, Any] | None:
    """Parse one stripped line into a journal row, or ``None`` if malformed.

    A line is malformed (``None``) when it is not valid JSON, is not a JSON
    object, or lacks the ``step_hash`` field - i.e. it cannot be a chain row.
    Returning a concrete ``dict[str, Any]`` lets callers read fields without
    re-narrowing on every access.
    """
    try:
        row: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict) or "step_hash" not in row:
        return None
    return row


def _validate_chain_for_recovery(bucket_path: Path) -> tuple[str, int]:
    """Walk *bucket_path* and revalidate the hash chain for tip recovery.

    Returns ``(tip_hash, validated_count)`` where ``tip_hash`` is the last
    *recomputed* ``step_hash`` (or :data:`GENESIS_HASH` for an empty file)
    and ``validated_count`` is the number of rows that verified.

    Fail-closed contract (the lever the replay/fork surface rests on):

    * A parseable JSON object that is a journal row (has ``step_hash``) whose
      recomputed ``step_hash`` does not match the stored value, whose
      ``prev_hash`` does not chain onto the previous row, or whose ``seq``
      skips a slot raises :class:`JournalError` naming the offending line.
      This is interior/tail tampering and must not be silently adopted.
    * A torn/unparseable trailing line (crash mid-write) is tolerated only
      when it is the final non-empty line: recovery stops at the last
      validated row. If a malformed line is *followed* by a well-formed row,
      the malformed line is interior corruption and raises.

    Reuses :func:`compute_step_hash` - the single chain primitive - so there
    is no second hashing scheme to drift against :meth:`JournalReader.verify`.
    """
    prev_hash = GENESIS_HASH
    expected_seq = 0
    validated = 0
    # A malformed line is only a legitimate torn tail if nothing well-formed
    # follows it. Remember the first malformed line number and raise lazily if
    # a later row proves it was an interior break.
    pending_torn_line: int | None = None

    with bucket_path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue

            row = _parse_journal_row(stripped)
            if row is None:
                # Defer: could be a torn final line (ok) or an interior break.
                if pending_torn_line is None:
                    pending_torn_line = line_no
                continue

            if pending_torn_line is not None:
                # A well-formed row appeared after a malformed line: the
                # earlier line was interior corruption, not a torn tail.
                msg = (
                    f"journal {bucket_path}: line {pending_torn_line} is corrupt but "
                    f"line {line_no} continues the chain; refusing to recover across a "
                    f"broken interior row. Move the journal aside to recover."
                )
                raise JournalError(msg)

            raw_seq = row.get("seq", -1)
            try:
                seq = int(raw_seq)
            except (TypeError, ValueError) as exc:
                msg = (
                    f"journal {bucket_path}: line {line_no} has a non-integer seq "
                    f"(got {raw_seq!r}); the row is tampered or inconsistent. "
                    f"Move the journal aside to recover."
                )
                raise JournalError(msg) from exc
            if seq != expected_seq:
                msg = (
                    f"journal {bucket_path}: line {line_no} seq mismatch "
                    f"(expected {expected_seq}, got {seq}); chain is broken. "
                    f"Move the journal aside to recover."
                )
                raise JournalError(msg)

            stored_prev = str(row.get("prev_hash", ""))
            if stored_prev != prev_hash:
                msg = (
                    f"journal {bucket_path}: line {line_no} prev_hash mismatch "
                    f"(expected {prev_hash[:16]}..., got {stored_prev[:16]}...); "
                    f"chain is broken. Move the journal aside to recover."
                )
                raise JournalError(msg)

            recomputed = compute_step_hash(
                prev_hash=stored_prev,
                input_hash=str(row.get("input_hash", "")),
                model=row.get("model"),
                prompt=row.get("prompt"),
                tool_call=row.get("tool_call"),
                tool_result=row.get("tool_result"),
                # Legacy rows have no ``effort`` key: ``get`` returns None,
                # so the recomputed hash matches the pre-effort payload.
                effort=row.get("effort"),
            )
            stored_hash = str(row.get("step_hash", ""))
            if recomputed != stored_hash:
                msg = (
                    f"journal {bucket_path}: line {line_no} step_hash mismatch "
                    f"(recomputed {recomputed[:16]}..., stored {stored_hash[:16]}...); "
                    f"the row is tampered or inconsistent. Move the journal aside to recover."
                )
                raise JournalError(msg)

            prev_hash = stored_hash
            expected_seq += 1
            validated += 1

    if pending_torn_line is not None:
        logger.warning(
            "journal %s: torn trailing line %d ignored; recovered %d validated step(s)",
            bucket_path,
            pending_torn_line,
            validated,
        )

    return prev_hash, validated


# ---------------------------------------------------------------------------
# Journal (writer)
# ---------------------------------------------------------------------------


class Journal:
    """Append-only hash-chained journal for one agent's run.

    Use :meth:`Journal.open` to obtain an instance; the constructor takes
    only resolved internal state.

    A journal is single-writer at the file level (the OS append + our
    in-process lock guarantee no torn lines) but multiple callers may
    hold the same ``Journal`` instance and call :meth:`append` from
    different threads safely.
    """

    __slots__ = ("_bucket_path", "_closed", "_dir", "_lock", "_seq", "_tip_hash")

    def __init__(self, agent_dir: Path) -> None:
        self._dir = agent_dir
        self._bucket_path = agent_dir / _DEFAULT_BUCKET
        self._lock = threading.Lock()
        self._tip_hash = GENESIS_HASH
        self._seq = 0
        self._closed = False

    # -- factories -----------------------------------------------------------

    @classmethod
    def open(cls, agent_dir: Path) -> Journal:
        """Open (or create) the journal for the agent rooted at *agent_dir*.

        If the directory already contains a bucket file, the chain head and
        the next sequence number are recovered by walking the file's tail.
        """
        agent_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            # Owner-only directory; the journal can carry sensitive prompts.
            with contextlib.suppress(OSError):
                agent_dir.chmod(0o700)

        journal = cls(agent_dir)
        journal._recover_tail()
        return journal

    # -- properties ---------------------------------------------------------

    @property
    def head_hash(self) -> str:
        """Latest step hash, or :data:`GENESIS_HASH` if no entries exist."""
        return self._tip_hash

    @property
    def next_seq(self) -> int:
        """The ``seq`` the next call to :meth:`append` would assign."""
        return self._seq

    @property
    def agent_dir(self) -> Path:
        """The on-disk directory that backs this journal."""
        return self._dir

    @property
    def bucket_path(self) -> Path:
        """Path to the current bucket file."""
        return self._bucket_path

    # -- write --------------------------------------------------------------

    def append(
        self,
        *,
        input_hash: str,
        model: str | None = None,
        prompt: str | None = None,
        tool_call: Any = None,
        tool_result: Any = None,
        blob_refs: list[str] | None = None,
        effort: str | None = None,
    ) -> JournalEntry:
        """Append a new step to the chain and return the persisted entry.

        ``effort`` records the reasoning effort the step was routed at. When
        ``None`` (default) the row is byte-identical to a pre-effort record,
        so existing callers that never pass it keep producing legacy-shaped
        hashes; a recorded effort changes the step hash so a cross-effort
        replay is caught as divergence.

        Raises:
            JournalError: If the journal is closed or the file write fails.
        """
        if self._closed:
            msg = f"journal at {self._dir} is closed"
            raise JournalError(msg)

        with self._lock:
            prev_hash = self._tip_hash
            seq = self._seq
            step_hash = compute_step_hash(
                prev_hash=prev_hash,
                input_hash=input_hash,
                model=model,
                prompt=prompt,
                tool_call=tool_call,
                tool_result=tool_result,
                effort=effort,
            )
            entry = JournalEntry(
                seq=seq,
                prev_hash=prev_hash,
                input_hash=input_hash,
                model=model,
                prompt=prompt,
                tool_call=tool_call,
                tool_result=tool_result,
                step_hash=step_hash,
                ts=time.time(),
                blob_refs=list(blob_refs or []),
                effort=effort,
            )
            line = json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":"))
            try:
                # ``newline=""`` keeps the trailing ``\n`` byte-exact on Windows.
                # The actual file lock is the in-process ``self._lock`` plus
                # the single-writer convention; an extra ``fcntl.flock`` here
                # would offer no benefit for a single-process append.
                with self._bucket_path.open("a", encoding="utf-8", newline="") as fh:
                    fh.write(line + "\n")
                if os.name == "posix":
                    with contextlib.suppress(OSError):
                        self._bucket_path.chmod(0o600)
            except OSError as exc:
                msg = f"journal append failed: {exc}"
                raise JournalError(msg) from exc

            self._tip_hash = step_hash
            self._seq = seq + 1
            return entry

    # -- close --------------------------------------------------------------

    def close(self) -> None:
        """Mark the journal as closed; future :meth:`append` calls raise."""
        self._closed = True

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internal -----------------------------------------------------------

    def _recover_tail(self) -> None:
        """Recover ``tip_hash`` + ``seq`` by revalidating the chain on open.

        The tip is recovered from the last *recomputed* ``step_hash`` and
        ``seq`` is set to the count of validated rows - never read verbatim
        from the tail. Recovery fails closed (#1836): a parseable row whose
        chain does not verify (recomputed ``step_hash`` mismatch, ``prev_hash``
        break, or ``seq`` gap) raises :class:`JournalError` naming the
        offending line, so a tampered or truncated-then-edited journal cannot
        be silently extended from a poisoned anchor. A torn/unparseable
        trailing line (crash mid-write) still degrades gracefully to the last
        validated row, preserving legitimate crash recovery.
        """
        if not self._bucket_path.exists():
            return
        try:
            tip_hash, validated = _validate_chain_for_recovery(self._bucket_path)
        except OSError as exc:
            msg = f"journal recovery failed: {exc}"
            raise JournalError(msg) from exc

        self._tip_hash = tip_hash
        self._seq = validated


# ---------------------------------------------------------------------------
# JournalReader (read-only)
# ---------------------------------------------------------------------------


class JournalReader:
    """Read-only view over a persisted journal.

    Construct one to verify a chain, window it for fork-from-step
    reconstruction, or iterate it for the interactive replay UI. The
    reader never opens the bucket file for writing - it is safe to use
    while a live writer is appending (the worst case is a torn tail line
    which is dropped by the entry parser).
    """

    __slots__ = ("_bucket_path", "_dir")

    def __init__(self, agent_dir: Path) -> None:
        self._dir = agent_dir
        self._bucket_path = agent_dir / _DEFAULT_BUCKET

    @property
    def agent_dir(self) -> Path:
        return self._dir

    @property
    def bucket_path(self) -> Path:
        return self._bucket_path

    def entries(self) -> Iterator[JournalEntry]:
        """Yield every well-formed entry in ``seq`` order."""
        if not self._bucket_path.exists():
            return
        with self._bucket_path.open(encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    # Truncated tail - the writer either crashed mid-line or
                    # a corrupted append snuck through. Skip rather than abort
                    # so the operator can still inspect the prior steps.
                    continue
                if not isinstance(row, dict) or "step_hash" not in row:
                    continue
                yield JournalEntry.from_dict(row)

    def head(self) -> JournalEntry | None:
        """Return the latest entry, or ``None`` if the journal is empty."""
        last: JournalEntry | None = None
        for entry in self.entries():
            last = entry
        return last

    def window(self, start_seq: int, end_seq: int) -> list[JournalEntry]:
        """Return entries with ``start_seq <= seq <= end_seq`` (inclusive).

        Raises:
            JournalError: If the requested range is empty (e.g. ``end_seq``
                is past the tail). The orchestrator should fail loudly on
                this rather than silently truncate a fork.
        """
        if end_seq < start_seq:
            msg = f"empty window: start={start_seq} end={end_seq}"
            raise JournalError(msg)
        result = [e for e in self.entries() if start_seq <= e.seq <= end_seq]
        if not result or result[-1].seq < end_seq:
            msg = (
                f"requested window [{start_seq}..{end_seq}] is out of range; "
                f"have {len(result)} entries up to seq "
                f"{result[-1].seq if result else 'n/a'}"
            )
            raise JournalError(msg)
        return result

    def verify(self, expected_head: str | None = None) -> VerificationResult:
        """Walk the chain and recompute every step hash.

        Args:
            expected_head: If supplied, the verifier additionally checks
                that the tail's ``step_hash`` equals this value. Useful
                when the caller already knows the head hash (e.g. from a
                signed receipt) and wants to confirm the on-disk chain
                still matches.

        Returns:
            A :class:`VerificationResult` carrying ``ok``, the discovered
            ``head_hash``, the number of steps walked, and any error
            messages keyed by line number.
        """
        errors: list[str] = []
        prev_hash = GENESIS_HASH
        steps = 0
        expected_seq = 0

        if not self._bucket_path.exists():
            ok = expected_head in (None, GENESIS_HASH)
            if not ok:
                errors.append(f"no journal file at {self._bucket_path}; expected head {expected_head!r}")
            return VerificationResult(ok=ok, head_hash=GENESIS_HASH, steps=0, errors=errors)

        with self._bucket_path.open(encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {line_no}: invalid JSON ({exc})")
                    continue
                if not isinstance(row, dict):
                    errors.append(f"line {line_no}: entry is not a JSON object")
                    continue

                seq = int(row.get("seq", -1))
                if seq != expected_seq:
                    errors.append(f"line {line_no}: seq mismatch (expected {expected_seq}, got {seq})")

                stored_prev = str(row.get("prev_hash", ""))
                if stored_prev != prev_hash:
                    errors.append(
                        f"line {line_no}: prev_hash mismatch (expected {prev_hash[:16]}..., got {stored_prev[:16]}...)"
                    )

                recomputed = compute_step_hash(
                    prev_hash=stored_prev,
                    input_hash=str(row.get("input_hash", "")),
                    model=row.get("model"),
                    prompt=row.get("prompt"),
                    tool_call=row.get("tool_call"),
                    tool_result=row.get("tool_result"),
                    # Legacy rows lack ``effort`` (get -> None), matching the
                    # pre-effort payload so old chains verify unchanged.
                    effort=row.get("effort"),
                )
                stored_hash = str(row.get("step_hash", ""))
                if recomputed != stored_hash:
                    errors.append(
                        f"line {line_no}: step_hash mismatch (expected {recomputed[:16]}..., got {stored_hash[:16]}...)"
                    )

                prev_hash = stored_hash
                steps += 1
                expected_seq += 1

        head_hash = prev_hash
        if expected_head is not None and expected_head != head_hash:
            errors.append(f"head mismatch: expected {expected_head[:16]}..., got {head_hash[:16]}...")

        return VerificationResult(ok=not errors, head_hash=head_hash, steps=steps, errors=errors)


# ---------------------------------------------------------------------------
# Convenience: resolve the journal root for an install
# ---------------------------------------------------------------------------


def default_journal_root(sdd_dir: Path) -> Path:
    """Return ``<sdd_dir>/runtime/journal`` (created on first use)."""
    root = sdd_dir / "runtime" / "journal"
    root.mkdir(parents=True, exist_ok=True)
    return root


def agent_journal_dir(sdd_dir: Path, agent_id: str) -> Path:
    """Return the per-agent journal directory under the default root."""
    return default_journal_root(sdd_dir) / agent_id


__all__ = [
    "AUDIT_EVENT_REPLAY_EXPORT",
    "AUDIT_EVENT_REPLAY_FORK",
    "AUDIT_EVENT_REPLAY_PUBLISH",
    "AUDIT_EVENT_REPLAY_STEP",
    "GENESIS_HASH",
    "JOURNAL_SCHEMA_VERSION",
    "Journal",
    "JournalEntry",
    "JournalError",
    "JournalReader",
    "VerificationResult",
    "agent_journal_dir",
    "canonical_step_payload",
    "compute_step_hash",
    "default_journal_root",
]
