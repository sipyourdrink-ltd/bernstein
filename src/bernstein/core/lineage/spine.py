"""LineageSpine - the single always-on Merkle+HMAC lineage store.

Issue #2292. Bernstein historically carried three parallel lineage
stores (``core/lineage`` v1, ``core/lineage/v2_store``,
``core/persistence/lineage``) and none was wired into the live adapter
write path, so ``lineage verify`` passed trivially against near-empty
logs while real artifact writes emitted nothing.

The spine unifies them. It owns one append-only, Merkle-chained JSONL
store keyed by run id under ``.sdd/lineage/<run_id>/spine.jsonl`` plus a
``spine.head`` file that carries the head hash and its HMAC tag. Every
adapter artifact write routes through :meth:`LineageSpine.record` at the
single write boundary in :mod:`bernstein.adapters.base`, so provenance
is captured with no per-adapter opt-in.

Each entry is::

    entry_hash = H(prev_hash, artifact_path, content_hash, actor,
                   step_id, model, timestamp)

and carries an HMAC tag computed with the existing audit-chain key
(:func:`bernstein.core.security.audit.load_or_create_audit_key`). The
entry hash is the run's artifact-provenance identity: the head hash of
the chain is a single value a verifier can pin.

Determinism: the on-disk row is canonical JSON (sorted keys, minimal
separators, UTF-8) and every field is either caller-supplied or a pure
function of caller input, so two byte-identical runs against the same
fixtures produce byte-identical ``spine.jsonl`` including entry order
and hashes.

Verifiability: :meth:`verify` recomputes the whole hash chain and every
HMAC tag; any single-byte mutation of any entry (payload or tag) fails
the check. An empty run reports a distinct ``NO_ENTRIES`` status rather
than a trivial pass.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

from bernstein.core.lineage.artifact_uri import (
    REASON_ABSOLUTE,
    REASON_EMPTY,
    REASON_MALFORMED_URI,
    REASON_NON_CANONICAL,
    REASON_TRAVERSAL,
    REASON_UNKNOWN_SCHEME,
    artifact_key_rejection_reason,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Version stamped into every spine entry. Bump only on a wire-format
#: change; ``verify`` rejects unknown versions.
SPINE_ENTRY_VERSION = 1

#: ``step_id`` prefix of the internal journal-head seal written at run
#: finalization (see ``bernstein.core.replay.journal.seal_journal_into_spine``).
#: An entry with this prefix records the run's own journal file, not a produced
#: artifact, so ``verify`` treats a chain built only of these as having no
#: artifact provenance (issue #2789).
JOURNAL_SEAL_STEP_PREFIX = "replay-journal-head:"

_SPINE_LOG_NAME = "spine.jsonl"
_SPINE_HEAD_NAME = "spine.head"

_GENESIS_HASH = ""


# ---------------------------------------------------------------------------
# Run-id + path validation (mirrors persistence.lineage / recorder rules)
# ---------------------------------------------------------------------------


class SpineRunIdError(ValueError):
    """Raised when a ``run_id`` would escape its per-run directory."""


def _validate_run_id(run_id: str) -> str:
    if not run_id:
        raise SpineRunIdError("run_id must not be empty")
    if "/" in run_id or "\\" in run_id:
        raise SpineRunIdError(
            f"run_id contains a path separator ({run_id!r}); spine records would land in a sibling directory",
        )
    if run_id in {".", ".."} or run_id.startswith(".."):
        raise SpineRunIdError(f"run_id resolves to a parent path: {run_id!r}")
    if "\x00" in run_id:
        raise SpineRunIdError("run_id contains a NUL byte")
    return run_id


#: Boundary wording for each rejection code from
#: :func:`bernstein.core.lineage.artifact_uri.artifact_key_rejection_reason`.
#: The three legacy codes keep their pre-#2559 message verbatim so existing
#: callers and tests that match on the error text are unaffected.
_REJECTION_MESSAGES = {
    REASON_EMPTY: "empty artifact_path",
    REASON_ABSOLUTE: "absolute artifact_path not allowed",
    REASON_TRAVERSAL: "path traversal in artifact_path",
    REASON_UNKNOWN_SCHEME: "unknown artifact URI scheme in artifact_path",
    REASON_MALFORMED_URI: "malformed artifact URI in artifact_path",
    REASON_NON_CANONICAL: "non-canonical artifact URI in artifact_path",
}


def _reject_unsafe_artifact_path(artifact_path: str) -> None:
    """Reject anything that is not a canonical artifact key (issue #2559).

    A spine key is either a repo-relative POSIX path (the implicit scheme, and
    what every historical entry carries) or a canonical artifact URI from the
    closed scheme set in :mod:`bernstein.core.lineage.artifact_uri`.

    Repo paths take the same branch as before: an attacker controlling the call
    site still cannot smuggle ``../`` outside the repo or anchor an artifact at
    ``/etc/passwd``, and every path accepted before is accepted now.

    What changes is that a string carrying ``://`` is no longer treated as a
    filename. It used to slip past all three checks and be stored verbatim, so
    the chain could carry a key whose scheme meant nothing. It is now parsed,
    and rejected unless its scheme is known and its spelling is canonical.
    """
    reason = artifact_key_rejection_reason(artifact_path)
    if reason is None:
        return
    message = _REJECTION_MESSAGES[reason]
    if reason == REASON_EMPTY:
        raise ValueError(message)
    raise ValueError(f"{message}: {artifact_path!r}")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def content_hash_of(content: bytes) -> str:
    """Return the ``sha256:``-prefixed digest of ``content``."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


def compute_entry_hash(
    *,
    prev_hash: str,
    artifact_path: str,
    content_hash: str,
    actor: str,
    step_id: str,
    model: str,
    timestamp: int,
    traceparent: str | None = None,
    tracestate: str | None = None,
    baggage: str | None = None,
) -> str:
    """Return ``entry_hash = H(prev_hash, artifact_path, content_hash, ...)``.

    The pre-image is the canonical JSON of the ordered field tuple, so
    the digest is deterministic across processes and platforms.
    """
    fields = {
        "prev_hash": prev_hash,
        "artifact_path": artifact_path,
        "content_hash": content_hash,
        "actor": actor,
        "step_id": step_id,
        "model": model,
        "timestamp": timestamp,
    }
    if traceparent is not None:
        fields["traceparent"] = traceparent
    if tracestate is not None:
        fields["tracestate"] = tracestate
    if baggage is not None:
        fields["baggage"] = baggage
    preimage = json.dumps(
        fields,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpineEntry:
    """One immutable lineage-spine event.

    ``entry_hash`` and ``hmac`` are derived; they are materialised on the
    row so a reader can walk the chain without recomputing, and
    :meth:`LineageSpine.verify` recomputes both to detect tampering.
    """

    v: int
    prev_hash: str
    artifact_path: str
    content_hash: str
    actor: str
    step_id: str
    model: str
    timestamp: int
    entry_hash: str
    hmac: str
    traceparent: str | None = None
    tracestate: str | None = None
    baggage: str | None = None

    def body(self) -> dict[str, Any]:
        """Return the HMAC/hash-covered body (all fields except ``hmac``)."""
        res = {
            "v": self.v,
            "prev_hash": self.prev_hash,
            "artifact_path": self.artifact_path,
            "content_hash": self.content_hash,
            "actor": self.actor,
            "step_id": self.step_id,
            "model": self.model,
            "timestamp": self.timestamp,
            "entry_hash": self.entry_hash,
        }
        if self.traceparent is not None:
            res["traceparent"] = self.traceparent
        if self.tracestate is not None:
            res["tracestate"] = self.tracestate
        if self.baggage is not None:
            res["baggage"] = self.baggage
        return res

    def to_row(self) -> bytes:
        """Serialise the entry to its canonical single-line JSONL form."""
        row = self.body()
        row["hmac"] = self.hmac
        return (json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _canonical_body_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _compute_hmac(key: bytes, body: dict[str, Any]) -> str:
    return _hmac.new(key, _canonical_body_bytes(body), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Verify result
# ---------------------------------------------------------------------------


class SpineStatus(Enum):
    """Outcome of :meth:`LineageSpine.verify`."""

    OK = "ok"
    NO_ENTRIES = "no_entries"
    #: The chain is intact but contains only the internal journal-head seal:
    #: no produced-artifact provenance was captured (issue #2789). Distinct
    #: from ``OK`` so a run that recorded no deliverable cannot pass as
    #: "provenance confirmed".
    SEAL_ONLY = "seal_only"
    TAMPERED = "tampered"


@dataclass(frozen=True, slots=True)
class SpineVerifyResult:
    """Result of verifying a run's spine."""

    status: SpineStatus
    count: int
    errors: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        """True only when the chain is intact and carries artifact provenance.

        Neither an empty run (``NO_ENTRIES``) nor a chain that holds only the
        internal journal-head seal (``SEAL_ONLY``, issue #2789) is ``ok``: a
        distinct status keeps ``lineage verify`` from passing trivially against
        a run that emitted nothing or recorded no produced artifact.
        """
        return self.status is SpineStatus.OK


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Serialise appenders across processes via ``flock(LOCK_EX)``.

    Falls back to a no-op on platforms without ``fcntl`` (Windows); the
    per-store in-process append is still ordered by the caller.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".spine.lock"
    if fcntl is None:  # pragma: no cover - Windows path
        yield
        return
    fd = None
    try:
        fd = lock_path.open("a")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            finally:
                fd.close()


# ---------------------------------------------------------------------------
# LineageSpine
# ---------------------------------------------------------------------------


class LineageSpine:
    """Append-only Merkle-chained, HMAC-tagged lineage store for one run.

    Layout under ``<root>/<run_id>/``:

    * ``spine.jsonl`` - one canonical JSON row per artifact write.
    * ``spine.head`` - JSON ``{head_hash, hmac, count}`` snapshot of the
      chain head, refreshed atomically on every append.

    The store is safe across threads and processes; appends take an
    exclusive ``flock`` over the run directory.
    """

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        hmac_key: bytes,
    ) -> None:
        self._root = Path(root)
        self._run_id = _validate_run_id(run_id)
        self._hmac_key = hmac_key

    # -- paths --------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self._root / self._run_id

    @property
    def spine_path(self) -> Path:
        return self.run_dir / _SPINE_LOG_NAME

    @property
    def head_path(self) -> Path:
        return self.run_dir / _SPINE_HEAD_NAME

    # -- head ---------------------------------------------------------------

    def _read_head(self) -> tuple[str, int]:
        """Return ``(head_hash, count)`` from the last spine row.

        The ``spine.head`` file is a convenience snapshot; the JSONL is
        the source of truth, so the head is recovered from the log tail
        to stay correct even if a crash left ``spine.head`` stale.
        """
        if not self.spine_path.exists():
            return _GENESIS_HASH, 0
        raw = self.spine_path.read_bytes().rstrip(b"\n")
        if not raw:
            return _GENESIS_HASH, 0
        lines = raw.split(b"\n")
        try:
            last = json.loads(lines[-1])
        except json.JSONDecodeError:
            return _GENESIS_HASH, len(lines)
        head = last.get("entry_hash", _GENESIS_HASH)
        return (str(head) if isinstance(head, str) else _GENESIS_HASH), len(lines)

    def _write_head(self, head_hash: str, count: int) -> None:
        body = {"head_hash": head_hash, "count": count}
        head_hmac = _compute_hmac(self._hmac_key, body)
        payload = body | {"hmac": head_hmac}
        tmp = self.head_path.with_suffix(".head.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.head_path)

    # -- record -------------------------------------------------------------

    def record(
        self,
        *,
        artifact_path: str,
        content: bytes,
        actor: str,
        step_id: str,
        model: str,
        timestamp: int,
        traceparent: str | None = None,
        tracestate: str | None = None,
        baggage: str | None = None,
    ) -> str:
        """Append one entry for an artifact write. Returns its entry hash.

        A thin wrapper over :meth:`record_entry` for the common caller that
        only needs the entry hash; see that method for argument semantics.
        """
        return self.record_entry(
            artifact_path=artifact_path,
            content=content,
            actor=actor,
            step_id=step_id,
            model=model,
            timestamp=timestamp,
            traceparent=traceparent,
            tracestate=tracestate,
            baggage=baggage,
        ).entry_hash

    def record_entry(
        self,
        *,
        artifact_path: str,
        content: bytes,
        actor: str,
        step_id: str,
        model: str,
        timestamp: int,
        traceparent: str | None = None,
        tracestate: str | None = None,
        baggage: str | None = None,
    ) -> SpineEntry:
        """Append one entry for an artifact write and return it.

        Returns the fully-materialised :class:`SpineEntry`, so a caller that
        needs the entry's HMAC tag (not just its hash) reads it off the
        return value instead of re-scanning the whole spine to find the entry
        it just appended.

        Args:
            artifact_path: Repo-relative POSIX path of the artifact.
            content: The bytes that landed on disk; hashed into
                ``content_hash``.
            actor: Producing agent / adapter identifier.
            step_id: Cross-link to the originating step / tool call.
            model: Model string recorded for provenance.
            timestamp: Integer timestamp (ns or s; caller-chosen but
                stable, so identical fixtures replay byte-identically).
            traceparent: Optional W3C traceparent context.
            tracestate: Optional W3C tracestate context.
            baggage: Optional W3C baggage context.

        Raises:
            ValueError: When ``artifact_path`` is absolute or contains a
                traversal segment.
        """
        _reject_unsafe_artifact_path(artifact_path)
        c_hash = content_hash_of(content)

        with _exclusive_lock(self.spine_path):
            prev_hash, count = self._read_head()
            e_hash = compute_entry_hash(
                prev_hash=prev_hash,
                artifact_path=artifact_path,
                content_hash=c_hash,
                actor=actor,
                step_id=step_id,
                model=model,
                timestamp=timestamp,
                traceparent=traceparent,
                tracestate=tracestate,
                baggage=baggage,
            )
            body = {
                "v": SPINE_ENTRY_VERSION,
                "prev_hash": prev_hash,
                "artifact_path": artifact_path,
                "content_hash": c_hash,
                "actor": actor,
                "step_id": step_id,
                "model": model,
                "timestamp": timestamp,
                "entry_hash": e_hash,
            }
            if traceparent is not None:
                body["traceparent"] = traceparent
            if tracestate is not None:
                body["tracestate"] = tracestate
            if baggage is not None:
                body["baggage"] = baggage
            tag = _compute_hmac(self._hmac_key, body)
            entry = SpineEntry(
                v=SPINE_ENTRY_VERSION,
                prev_hash=prev_hash,
                artifact_path=artifact_path,
                content_hash=c_hash,
                actor=actor,
                step_id=step_id,
                model=model,
                timestamp=timestamp,
                entry_hash=e_hash,
                hmac=tag,
                traceparent=traceparent,
                tracestate=tracestate,
                baggage=baggage,
            )
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with self.spine_path.open("ab") as fh:
                fh.write(entry.to_row())
                fh.flush()
                os.fsync(fh.fileno())
            self._write_head(e_hash, count + 1)
        return entry

    # -- read ---------------------------------------------------------------

    def iter_entries(self) -> Iterator[SpineEntry]:
        """Yield every entry in append order.

        Malformed rows are skipped with a debug log; ``verify`` is the
        surface that turns corruption into a hard failure.
        """
        if not self.spine_path.exists():
            return
        raw = self.spine_path.read_bytes().rstrip(b"\n")
        if not raw:
            return
        for line in raw.split(b"\n"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("spine: skipping malformed row in %s", self.spine_path)
                continue
            try:
                yield SpineEntry(
                    v=int(row["v"]),
                    prev_hash=str(row["prev_hash"]),
                    artifact_path=str(row["artifact_path"]),
                    content_hash=str(row["content_hash"]),
                    actor=str(row["actor"]),
                    step_id=str(row["step_id"]),
                    model=str(row["model"]),
                    timestamp=int(row["timestamp"]),
                    entry_hash=str(row["entry_hash"]),
                    hmac=str(row["hmac"]),
                    traceparent=row.get("traceparent"),
                    tracestate=row.get("tracestate"),
                    baggage=row.get("baggage"),
                )
            except (KeyError, TypeError, ValueError):
                logger.debug("spine: skipping row with bad shape in %s", self.spine_path)
                continue

    def head_hash(self) -> str:
        """Return the current chain head hash (empty for an empty run)."""
        head, _ = self._read_head()
        return head

    # -- verify -------------------------------------------------------------

    def verify(self) -> SpineVerifyResult:
        """Recompute the full hash chain and every HMAC tag.

        Returns a :class:`SpineVerifyResult`. ``NO_ENTRIES`` is a
        distinct status: an empty run must not trivially pass (AC5). A chain
        whose only entries are the internal journal-head seal carries no
        produced-artifact provenance and returns the distinct ``SEAL_ONLY``
        status rather than ``OK`` (issue #2789). Any single-byte mutation of
        any entry - payload byte or HMAC tag - yields ``TAMPERED`` (AC2).
        """
        if not self.spine_path.exists():
            return SpineVerifyResult(status=SpineStatus.NO_ENTRIES, count=0)
        raw = self.spine_path.read_bytes().rstrip(b"\n")
        if not raw:
            return SpineVerifyResult(status=SpineStatus.NO_ENTRIES, count=0)

        errors: list[str] = []
        prev_hash = _GENESIS_HASH
        count = 0
        # True until an entry that is *not* the internal journal-head seal is
        # seen; a chain built only of seals has no artifact provenance (#2789).
        seal_only = True
        for line_no, line in enumerate(raw.split(b"\n"), start=1):
            count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"line {line_no}: malformed JSON")
                continue
            if not isinstance(row, dict):
                errors.append(f"line {line_no}: row is not an object")
                continue
            missing = {
                "v",
                "prev_hash",
                "artifact_path",
                "content_hash",
                "actor",
                "step_id",
                "model",
                "timestamp",
                "entry_hash",
                "hmac",
            } - set(row)
            if missing:
                errors.append(f"line {line_no}: missing fields {sorted(missing)}")
                continue
            if row.get("v") != SPINE_ENTRY_VERSION:
                errors.append(f"line {line_no}: unsupported entry version {row.get('v')!r}")
                continue
            if row["prev_hash"] != prev_hash:
                errors.append(
                    f"line {line_no}: prev_hash break (expected {prev_hash[:16]!r}, got {str(row['prev_hash'])[:16]!r})"
                )
            try:
                expected_hash = compute_entry_hash(
                    prev_hash=str(row["prev_hash"]),
                    artifact_path=str(row["artifact_path"]),
                    content_hash=str(row["content_hash"]),
                    actor=str(row["actor"]),
                    step_id=str(row["step_id"]),
                    model=str(row["model"]),
                    timestamp=int(row["timestamp"]),
                    traceparent=row.get("traceparent"),
                    tracestate=row.get("tracestate"),
                    baggage=row.get("baggage"),
                )
            except (TypeError, ValueError):
                errors.append(f"line {line_no}: unhashable field types")
                continue
            if not _hmac.compare_digest(str(row["entry_hash"]), expected_hash):
                errors.append(f"line {line_no}: entry_hash mismatch")
            body = {k: row[k] for k in row if k != "hmac"}
            expected_hmac = _compute_hmac(self._hmac_key, body)
            if not _hmac.compare_digest(str(row["hmac"]), expected_hmac):
                errors.append(f"line {line_no}: hmac mismatch")
            if not str(row["step_id"]).startswith(JOURNAL_SEAL_STEP_PREFIX):
                seal_only = False
            prev_hash = str(row["entry_hash"])

        if errors:
            return SpineVerifyResult(status=SpineStatus.TAMPERED, count=count, errors=errors)
        if seal_only:
            return SpineVerifyResult(status=SpineStatus.SEAL_ONLY, count=count)
        return SpineVerifyResult(status=SpineStatus.OK, count=count)


__all__ = [
    "JOURNAL_SEAL_STEP_PREFIX",
    "SPINE_ENTRY_VERSION",
    "LineageSpine",
    "SpineEntry",
    "SpineRunIdError",
    "SpineStatus",
    "SpineVerifyResult",
    "compute_entry_hash",
    "content_hash_of",
]
