"""``MemoryChain`` -- the tamper-evident, content-addressed memory write chain.

Issue #2298. Bernstein historically kept cross-session memory in bespoke
episodic/semantic stores that prune and compact facts in place, with no
provenance on any stored fact: operators could not answer *when* a
remembered preference was learned or *by which actor*, and a fact could
be silently edited with no trace.

This module keeps only the durable, lineage-anchored part. Every memory
write becomes an append-only chained record attributing the claim to an
actor at a time::

    entry_hash = H(prev_hash, source_hash, actor, claim, model,
                   timestamp, scope, namespace, kind, tombstone_of)

The row is HMAC-tagged with the existing audit-chain key
(:func:`bernstein.core.security.audit.load_or_create_audit_key`), so a
verifier can prove a fact was written by the claimed actor at the claimed
time and never edited (AC1, AC2).

Each record's ``source_hash`` anchors to a :class:`LineageSpine` entry
(issue #2292) that produced it; :meth:`MemoryChain.verify` resolves every
``source_hash`` against the run's spine and fails the check when a
``source_hash`` dangles (AC5). :meth:`MemoryChain.why` walks back from a
claim to the originating run id and step (AC3).

Forgetting is non-destructive: :meth:`MemoryChain.forget` appends a
signed tombstone entry referencing the original's ``entry_hash`` rather
than deleting anything, so the original entry and the whole hash chain
stay intact and verifiable (AC4).

The four identity scopes (user / agent / run / app) are chain
namespaces: writes under different scopes live in disjoint chains, each
chained from its own genesis.

Determinism: the on-disk row is canonical JSON (sorted keys, minimal
separators, UTF-8) and every field is either caller-supplied or a pure
function of caller input, so two byte-identical writes against the same
fixtures produce byte-identical chain files including entry order and
hashes.

Context compaction is delegated to native model context management; this
module is intentionally provenance-only and carries no summarizer.
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

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Version stamped into every memory-chain entry. Bump only on a
#: wire-format change; ``verify`` rejects unknown versions.
MEMORY_CHAIN_ENTRY_VERSION = 1

#: Entry kinds. A write asserts a claim; a tombstone marks a prior
#: write's claim as forgotten without deleting it.
KIND_WRITE = "write"
KIND_TOMBSTONE = "tombstone"

_GENESIS_HASH = ""


# ---------------------------------------------------------------------------
# Identity scopes
# ---------------------------------------------------------------------------


class MemoryScope(Enum):
    """The four identity scopes, used as chain namespaces.

    * ``USER`` -- a durable operator/user preference.
    * ``AGENT`` -- an agent-local learning.
    * ``RUN`` -- a fact scoped to a single orchestration run.
    * ``APP`` -- an application-wide convention.
    """

    USER = "user"
    AGENT = "agent"
    RUN = "run"
    APP = "app"


# ---------------------------------------------------------------------------
# Namespace validation
# ---------------------------------------------------------------------------


class MemoryNamespaceError(ValueError):
    """Raised when a ``namespace`` would escape its per-scope directory."""


def _validate_namespace(namespace: str) -> str:
    if not namespace:
        raise MemoryNamespaceError("namespace must not be empty")
    if "/" in namespace or "\\" in namespace:
        raise MemoryNamespaceError(
            f"namespace contains a path separator ({namespace!r}); memory records would land in a sibling directory",
        )
    if namespace in {".", ".."} or namespace.startswith(".."):
        raise MemoryNamespaceError(f"namespace resolves to a parent path: {namespace!r}")
    if "\x00" in namespace:
        raise MemoryNamespaceError("namespace contains a NUL byte")
    return namespace


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def compute_entry_hash(
    *,
    prev_hash: str,
    source_hash: str,
    actor: str,
    claim: str,
    model: str,
    timestamp: int,
    scope: str,
    namespace: str,
    kind: str,
    tombstone_of: str,
) -> str:
    """Return the content-addressed entry hash for a memory record.

    The pre-image is the canonical JSON of the ordered field tuple, so
    the digest is deterministic across processes and platforms.
    """
    preimage = json.dumps(
        {
            "prev_hash": prev_hash,
            "source_hash": source_hash,
            "actor": actor,
            "claim": claim,
            "model": model,
            "timestamp": timestamp,
            "scope": scope,
            "namespace": namespace,
            "kind": kind,
            "tombstone_of": tombstone_of,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


def _canonical_body_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _compute_hmac(key: bytes, body: dict[str, Any]) -> str:
    return _hmac.new(key, _canonical_body_bytes(body), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryChainEntry:
    """One immutable memory-chain record.

    ``entry_hash`` and ``hmac`` are derived; they are materialised on the
    row so a reader can walk the chain without recomputing, and
    :meth:`MemoryChain.verify` recomputes both to detect tampering.
    """

    v: int
    prev_hash: str
    source_hash: str
    actor: str
    claim: str
    model: str
    timestamp: int
    scope: str
    namespace: str
    kind: str
    tombstone_of: str
    run_id: str
    step_id: str
    entry_hash: str
    hmac: str

    def body(self) -> dict[str, Any]:
        """Return the HMAC-covered body (all fields except ``hmac``)."""
        return {
            "v": self.v,
            "prev_hash": self.prev_hash,
            "source_hash": self.source_hash,
            "actor": self.actor,
            "claim": self.claim,
            "model": self.model,
            "timestamp": self.timestamp,
            "scope": self.scope,
            "namespace": self.namespace,
            "kind": self.kind,
            "tombstone_of": self.tombstone_of,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "entry_hash": self.entry_hash,
        }

    def to_row(self) -> bytes:
        """Serialise the entry to its canonical single-line JSONL form."""
        row = self.body()
        row["hmac"] = self.hmac
        return (json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Origin (why)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryOrigin:
    """The originating run and step for a stored claim (``why``)."""

    run_id: str
    step_id: str
    source_hash: str
    actor: str
    timestamp: int
    entry_hash: str


# ---------------------------------------------------------------------------
# Verify result
# ---------------------------------------------------------------------------


class MemoryChainStatus(Enum):
    """Outcome of :meth:`MemoryChain.verify`."""

    OK = "ok"
    NO_ENTRIES = "no_entries"
    TAMPERED = "tampered"


@dataclass(frozen=True, slots=True)
class MemoryVerifyResult:
    """Result of verifying a scope/namespace chain."""

    status: MemoryChainStatus
    count: int
    errors: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        """True only when the chain is intact and non-empty.

        An empty chain is *not* ``ok`` -- a distinct ``NO_ENTRIES``
        status keeps a namespace that stored nothing from being mistaken
        for a verified one.
        """
        return self.status is MemoryChainStatus.OK


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
    lock_path = path.parent / ".memory-chain.lock"
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
# Spine anchoring
# ---------------------------------------------------------------------------


def _spine_entry_hashes(spine_root: Path, run_id: str, hmac_key: bytes) -> set[str]:
    """Return every entry hash recorded in ``run_id``'s lineage spine.

    Returns an empty set when the run has no spine; the caller turns an
    unresolved ``source_hash`` into a tamper error.
    """
    from bernstein.core.lineage.spine import LineageSpine, SpineRunIdError

    try:
        spine = LineageSpine(spine_root, run_id=run_id, hmac_key=hmac_key)
    except SpineRunIdError:
        return set()
    return {entry.entry_hash for entry in spine.iter_entries()}


# ---------------------------------------------------------------------------
# MemoryChain
# ---------------------------------------------------------------------------


class MemoryChain:
    """Append-only, HMAC-tagged, actor-attributed memory chain.

    Layout under ``<root>/<scope>/<namespace>.jsonl``: one canonical JSON
    row per memory write or tombstone. Writes under different scopes live
    in disjoint chains, each chained from its own genesis.

    The store is safe across threads and processes; appends take an
    exclusive ``flock`` over the chain file's directory.
    """

    def __init__(self, root: Path, *, hmac_key: bytes) -> None:
        self._root = Path(root)
        self._hmac_key = hmac_key

    # -- paths --------------------------------------------------------------

    def chain_path(self, scope: MemoryScope, namespace: str) -> Path:
        """Return the JSONL path for ``scope``/``namespace``."""
        _validate_namespace(namespace)
        return self._root / scope.value / f"{namespace}.jsonl"

    # -- head ---------------------------------------------------------------

    def _read_head(self, path: Path) -> str:
        """Return the head entry hash for a chain (empty for a new chain)."""
        if not path.exists():
            return _GENESIS_HASH
        raw = path.read_bytes().rstrip(b"\n")
        if not raw:
            return _GENESIS_HASH
        try:
            last = json.loads(raw.split(b"\n")[-1])
        except json.JSONDecodeError:
            return _GENESIS_HASH
        head = last.get("entry_hash", _GENESIS_HASH)
        return str(head) if isinstance(head, str) else _GENESIS_HASH

    # -- append -------------------------------------------------------------

    def _append(
        self,
        *,
        scope: MemoryScope,
        namespace: str,
        claim: str,
        actor: str,
        source_hash: str,
        run_id: str,
        step_id: str,
        model: str,
        timestamp: int,
        kind: str,
        tombstone_of: str,
    ) -> MemoryChainEntry:
        _validate_namespace(namespace)
        path = self.chain_path(scope, namespace)

        with _exclusive_lock(path):
            prev_hash = self._read_head(path)
            e_hash = compute_entry_hash(
                prev_hash=prev_hash,
                source_hash=source_hash,
                actor=actor,
                claim=claim,
                model=model,
                timestamp=timestamp,
                scope=scope.value,
                namespace=namespace,
                kind=kind,
                tombstone_of=tombstone_of,
            )
            body = {
                "v": MEMORY_CHAIN_ENTRY_VERSION,
                "prev_hash": prev_hash,
                "source_hash": source_hash,
                "actor": actor,
                "claim": claim,
                "model": model,
                "timestamp": timestamp,
                "scope": scope.value,
                "namespace": namespace,
                "kind": kind,
                "tombstone_of": tombstone_of,
                "run_id": run_id,
                "step_id": step_id,
                "entry_hash": e_hash,
            }
            tag = _compute_hmac(self._hmac_key, body)
            entry = MemoryChainEntry(
                v=MEMORY_CHAIN_ENTRY_VERSION,
                prev_hash=prev_hash,
                source_hash=source_hash,
                actor=actor,
                claim=claim,
                model=model,
                timestamp=timestamp,
                scope=scope.value,
                namespace=namespace,
                kind=kind,
                tombstone_of=tombstone_of,
                run_id=run_id,
                step_id=step_id,
                entry_hash=e_hash,
                hmac=tag,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as fh:
                fh.write(entry.to_row())
                fh.flush()
                os.fsync(fh.fileno())
        return entry

    def write(
        self,
        *,
        scope: MemoryScope,
        namespace: str,
        claim: str,
        actor: str,
        source_hash: str,
        run_id: str,
        step_id: str,
        model: str,
        timestamp: int,
    ) -> MemoryChainEntry:
        """Append one actor-attributed memory write. Returns the entry.

        Args:
            scope: Identity scope (chain namespace).
            namespace: Chain key within the scope (e.g. a user handle).
            claim: The remembered fact, verbatim.
            actor: Producing agent / actor identifier.
            source_hash: Lineage-spine ``entry_hash`` that produced the
                fact; ``verify`` requires it to resolve to a real spine
                entry.
            run_id: Originating orchestration run id.
            step_id: Originating step / tool-call id.
            model: Model string recorded for provenance.
            timestamp: Stable integer timestamp (caller-chosen).

        Raises:
            MemoryNamespaceError: When ``namespace`` would escape its
                per-scope directory.
        """
        return self._append(
            scope=scope,
            namespace=namespace,
            claim=claim,
            actor=actor,
            source_hash=source_hash,
            run_id=run_id,
            step_id=step_id,
            model=model,
            timestamp=timestamp,
            kind=KIND_WRITE,
            tombstone_of="",
        )

    def forget(
        self,
        target_entry_hash: str,
        *,
        scope: MemoryScope,
        namespace: str,
        actor: str,
        source_hash: str,
        run_id: str,
        step_id: str,
        model: str,
        timestamp: int,
    ) -> MemoryChainEntry:
        """Append a signed tombstone for ``target_entry_hash``.

        Forgetting never deletes: the original write and the whole hash
        chain stay intact and verifiable. The tombstone's ``claim`` is
        the tombstoned entry hash so the record is self-describing.

        Args:
            target_entry_hash: ``entry_hash`` of the write being
                forgotten.
            (remaining args mirror :meth:`write`.)
        """
        return self._append(
            scope=scope,
            namespace=namespace,
            claim=target_entry_hash,
            actor=actor,
            source_hash=source_hash,
            run_id=run_id,
            step_id=step_id,
            model=model,
            timestamp=timestamp,
            kind=KIND_TOMBSTONE,
            tombstone_of=target_entry_hash,
        )

    # -- read ---------------------------------------------------------------

    def iter_entries(self, scope: MemoryScope, namespace: str) -> Iterator[MemoryChainEntry]:
        """Yield every entry in ``scope``/``namespace`` in append order.

        Malformed rows are skipped with a debug log; ``verify`` is the
        surface that turns corruption into a hard failure.
        """
        path = self.chain_path(scope, namespace)
        if not path.exists():
            return
        raw = path.read_bytes().rstrip(b"\n")
        if not raw:
            return
        for line in raw.split(b"\n"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("memory chain: skipping malformed row in %s", path)
                continue
            try:
                yield MemoryChainEntry(
                    v=int(row["v"]),
                    prev_hash=str(row["prev_hash"]),
                    source_hash=str(row["source_hash"]),
                    actor=str(row["actor"]),
                    claim=str(row["claim"]),
                    model=str(row["model"]),
                    timestamp=int(row["timestamp"]),
                    scope=str(row["scope"]),
                    namespace=str(row["namespace"]),
                    kind=str(row["kind"]),
                    tombstone_of=str(row["tombstone_of"]),
                    run_id=str(row["run_id"]),
                    step_id=str(row["step_id"]),
                    entry_hash=str(row["entry_hash"]),
                    hmac=str(row["hmac"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.debug("memory chain: skipping row with bad shape in %s", path)
                continue

    def forgotten_hashes(self, scope: MemoryScope, namespace: str) -> set[str]:
        """Return the set of entry hashes marked forgotten by a tombstone."""
        return {e.tombstone_of for e in self.iter_entries(scope, namespace) if e.kind == KIND_TOMBSTONE}

    def why(
        self,
        claim: str,
        *,
        scope: MemoryScope,
        namespace: str,
        spine_root: Path,
    ) -> MemoryOrigin | None:
        """Return the originating run and step for a stored ``claim``.

        Resolves the latest ``write`` entry whose claim equals ``claim``
        and corroborates it against the lineage spine the ``source_hash``
        anchors: the returned origin is only produced when the
        ``source_hash`` resolves to a real spine entry, so a fabricated
        provenance pointer never yields a ``why`` answer.

        Returns ``None`` when the claim was never written.
        """
        match: MemoryChainEntry | None = None
        for entry in self.iter_entries(scope, namespace):
            if entry.kind == KIND_WRITE and entry.claim == claim:
                match = entry
        if match is None:
            return None
        spine_hashes = _spine_entry_hashes(spine_root, match.run_id, self._hmac_key)
        if match.source_hash not in spine_hashes:
            return None
        return MemoryOrigin(
            run_id=match.run_id,
            step_id=match.step_id,
            source_hash=match.source_hash,
            actor=match.actor,
            timestamp=match.timestamp,
            entry_hash=match.entry_hash,
        )

    # -- verify -------------------------------------------------------------

    def verify(
        self,
        scope: MemoryScope,
        namespace: str,
        *,
        spine_root: Path,
    ) -> MemoryVerifyResult:
        """Recompute the hash chain, every HMAC tag, and spine anchoring.

        Returns a :class:`MemoryVerifyResult`. ``NO_ENTRIES`` is a
        distinct status: an empty namespace must not trivially pass. Any
        single-byte mutation of any entry -- payload byte or HMAC tag --
        yields ``TAMPERED`` (AC2), and every ``source_hash`` must resolve
        to a real spine entry for the record's run (AC5).
        """
        path = self.chain_path(scope, namespace)
        if not path.exists():
            return MemoryVerifyResult(status=MemoryChainStatus.NO_ENTRIES, count=0)
        raw = path.read_bytes().rstrip(b"\n")
        if not raw:
            return MemoryVerifyResult(status=MemoryChainStatus.NO_ENTRIES, count=0)

        errors: list[str] = []
        prev_hash = _GENESIS_HASH
        count = 0
        # Cache spine entry hashes per run so verify is one spine read per run.
        spine_cache: dict[str, set[str]] = {}
        required_fields = {
            "v",
            "prev_hash",
            "source_hash",
            "actor",
            "claim",
            "model",
            "timestamp",
            "scope",
            "namespace",
            "kind",
            "tombstone_of",
            "run_id",
            "step_id",
            "entry_hash",
            "hmac",
        }

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
            missing = required_fields - set(row)
            if missing:
                errors.append(f"line {line_no}: missing fields {sorted(missing)}")
                continue
            if row.get("v") != MEMORY_CHAIN_ENTRY_VERSION:
                errors.append(f"line {line_no}: unsupported entry version {row.get('v')!r}")
                continue
            if row["prev_hash"] != prev_hash:
                errors.append(
                    f"line {line_no}: prev_hash break (expected {prev_hash[:16]!r}, got {str(row['prev_hash'])[:16]!r})"
                )
            try:
                expected_hash = compute_entry_hash(
                    prev_hash=str(row["prev_hash"]),
                    source_hash=str(row["source_hash"]),
                    actor=str(row["actor"]),
                    claim=str(row["claim"]),
                    model=str(row["model"]),
                    timestamp=int(row["timestamp"]),
                    scope=str(row["scope"]),
                    namespace=str(row["namespace"]),
                    kind=str(row["kind"]),
                    tombstone_of=str(row["tombstone_of"]),
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

            run_id = str(row["run_id"])
            source_hash = str(row["source_hash"])
            if run_id not in spine_cache:
                spine_cache[run_id] = _spine_entry_hashes(spine_root, run_id, self._hmac_key)
            if source_hash not in spine_cache[run_id]:
                errors.append(f"line {line_no}: source_hash does not resolve to a spine entry for run {run_id!r}")

            prev_hash = str(row["entry_hash"])

        if errors:
            return MemoryVerifyResult(status=MemoryChainStatus.TAMPERED, count=count, errors=errors)
        return MemoryVerifyResult(status=MemoryChainStatus.OK, count=count)


__all__ = [
    "KIND_TOMBSTONE",
    "KIND_WRITE",
    "MEMORY_CHAIN_ENTRY_VERSION",
    "MemoryChain",
    "MemoryChainEntry",
    "MemoryChainStatus",
    "MemoryNamespaceError",
    "MemoryOrigin",
    "MemoryScope",
    "MemoryVerifyResult",
    "compute_entry_hash",
]
