"""Agent-posted task artifacts, journal-anchored and chain-receipted (#2553).

A worker in the middle of a run needs a channel to attach the substance of its
work to its own task: an audit summary it wrote, a comparison table it built,
the URL of a preview it deployed. This module is that channel, designed so the
artifact the reviewer sees **is** the verifiable receipt -- not narration with
a log bolted on the side.

Posting an artifact:

1. serialises the typed payload to canonical bytes and stores them
   content-addressed in the :class:`~bernstein.core.evidence.bundle.EvidenceStore`
   (reusing its per-blob cap and gc);
2. seals those bytes into the lineage spine -- the returned spine entry hash is
   the artifact's identity;
3. appends an ``artifact_posted`` row to the task's Merkle-chained
   :class:`~bernstein.core.replay.journal.EventJournal` carrying the content
   hash, key, version, and the prior version's hash; and
4. mirrors the record into the HMAC audit chain.

Rendering always re-checks the stored blob hash against the journal row, so a
tampered blob displays as *tampered*, not as content. Reposting an existing key
appends a new version whose record references the prior version's spine hash, so
history is a chain, never an overwrite, and every prior version stays
independently verifiable.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.evidence.bundle import DEFAULT_MAX_BLOB_BYTES, EvidenceStore
from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

# Per-(task, key) locks serialise the version-allocation -> spine-seal ->
# journal-append sequence so two concurrent posts in the same process cannot
# both pick version N+1 and reference the same predecessor. The task server is
# single-process, so a process-local lock is the correct scope; the spine and
# journal additionally hold cross-process file locks for their own appends.
_post_locks: dict[tuple[str, str], threading.Lock] = {}
_post_locks_guard = threading.Lock()


def _post_lock(task_id: str, key: str) -> threading.Lock:
    """Return the shared lock guarding version allocation for ``(task_id, key)``."""
    ident = (task_id, key)
    with _post_locks_guard:
        lock = _post_locks.get(ident)
        if lock is None:
            lock = threading.Lock()
            _post_locks[ident] = lock
        return lock


#: The journal event type for a posted artifact. Kept in lockstep with
#: :data:`bernstein.core.replay.progress.EVENT_ARTIFACT_POSTED` (progress must
#: prove it ignores this row).
JOURNAL_EVENT_ARTIFACT_POSTED = "artifact_posted"

#: The three artifact types a worker may post.
ARTIFACT_TYPE_REPORT = "report"
ARTIFACT_TYPE_TABLE = "table"
ARTIFACT_TYPE_LINK = "link"
ARTIFACT_TYPES: frozenset[str] = frozenset({ARTIFACT_TYPE_REPORT, ARTIFACT_TYPE_TABLE, ARTIFACT_TYPE_LINK})

#: The declared kinds a ``link`` artifact may carry.
LINK_KINDS: frozenset[str] = frozenset({"preview", "dashboard", "document"})

#: Artifact key alphabet: a single safe path-ish segment, no leading dot, no
#: separators, so it can be embedded in a spine artifact path unescaped.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: Task id alphabet accepted for artifact posting (matches the MCP tool schema).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class ArtifactError(ValueError):
    """Base class for run-artifact failures."""


class ArtifactValidationError(ArtifactError):
    """A payload, key, or task id failed validation."""


class ArtifactTooLargeError(ArtifactError):
    """A serialised payload exceeded the per-blob cap.

    The message names the cap so a caller can see the exact ceiling.
    """

    def __init__(self, size: int, cap: int) -> None:
        self.size = size
        self.cap = cap
        super().__init__(f"artifact payload is {size} bytes, exceeds the {cap}-byte per-blob cap")


class ArtifactClaimError(ArtifactError):
    """A caller tried to post against a task whose claim it does not hold."""


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """A typed, canonically-serialisable artifact body.

    One of three shapes, discriminated by :attr:`artifact_type`. Construct via
    :meth:`report`, :meth:`table`, or :meth:`link` so the shape is validated at
    the boundary; :meth:`canonical_bytes` yields the exact content-addressed
    bytes.
    """

    artifact_type: str
    body: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    url: str = ""
    link_kind: str = ""

    @staticmethod
    def report(body: str) -> ArtifactPayload:
        """A markdown report artifact."""
        if not isinstance(body, str) or not body:
            raise ArtifactValidationError("report artifact requires a non-empty markdown body")
        return ArtifactPayload(artifact_type=ARTIFACT_TYPE_REPORT, body=body)

    @staticmethod
    def table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ArtifactPayload:
        """A tabular artifact: column headers plus rows of cells."""
        cols = tuple(str(c) for c in columns)
        if not cols:
            raise ArtifactValidationError("table artifact requires at least one column")
        norm_rows: list[tuple[str, ...]] = []
        for i, row in enumerate(rows):
            cells = tuple(str(c) for c in row)
            if len(cells) != len(cols):
                raise ArtifactValidationError(
                    f"table row {i} has {len(cells)} cells, expected {len(cols)} to match the columns"
                )
            norm_rows.append(cells)
        return ArtifactPayload(artifact_type=ARTIFACT_TYPE_TABLE, columns=cols, rows=tuple(norm_rows))

    @staticmethod
    def link(url: str, kind: str) -> ArtifactPayload:
        """A link artifact: a URL with a declared kind."""
        if not isinstance(url, str) or not url:
            raise ArtifactValidationError("link artifact requires a non-empty url")
        if kind not in LINK_KINDS:
            raise ArtifactValidationError(f"link kind {kind!r} is not one of {sorted(LINK_KINDS)}")
        return ArtifactPayload(artifact_type=ARTIFACT_TYPE_LINK, url=url, link_kind=kind)

    def to_content_dict(self) -> dict[str, Any]:
        """Return the type-specific fields that define the artifact content."""
        if self.artifact_type == ARTIFACT_TYPE_REPORT:
            return {"type": ARTIFACT_TYPE_REPORT, "body": self.body}
        if self.artifact_type == ARTIFACT_TYPE_TABLE:
            return {
                "type": ARTIFACT_TYPE_TABLE,
                "columns": list(self.columns),
                "rows": [list(r) for r in self.rows],
            }
        if self.artifact_type == ARTIFACT_TYPE_LINK:
            return {"type": ARTIFACT_TYPE_LINK, "url": self.url, "kind": self.link_kind}
        raise ArtifactValidationError(f"unknown artifact type {self.artifact_type!r}")

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 JSON bytes that are content-addressed."""
        return json.dumps(self.to_content_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )


@dataclass(frozen=True, slots=True)
class RunArtifactRecord:
    """A posted artifact's chain-anchored record (the receipt).

    Attributes:
        task_id: The task the artifact is bound to.
        key: The artifact slot; reposting a key appends a new version.
        artifact_type: One of :data:`ARTIFACT_TYPES`.
        content_hash: ``sha256:`` hash of the stored canonical bytes.
        version: 1-based version number within the key.
        prev_version_hash: The prior version's :attr:`spine_entry_hash`, or ``""``.
        spine_entry_hash: This version's lineage-spine entry hash -- its identity.
        journal_index: 0-based index of the anchoring ``artifact_posted`` row.
        journal_event_hash: The anchoring journal row's Merkle ``event_hash``.
        link_kind: For link artifacts, the declared kind; else ``""``.
        size: Bytes stored for this version's content.
    """

    task_id: str
    key: str
    artifact_type: str
    content_hash: str
    version: int
    prev_version_hash: str
    spine_entry_hash: str
    journal_index: int
    journal_event_hash: str
    link_kind: str = ""
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON body served by the API / SSE / CLI."""
        return {
            "task_id": self.task_id,
            "key": self.key,
            "artifact_type": self.artifact_type,
            "content_hash": self.content_hash,
            "version": self.version,
            "prev_version_hash": self.prev_version_hash,
            "spine_entry_hash": self.spine_entry_hash,
            "journal_index": self.journal_index,
            "journal_event_hash": self.journal_event_hash,
            "link_kind": self.link_kind,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ArtifactVerifyResult:
    """The verification verdict for one artifact version."""

    ok: bool
    task_id: str
    key: str
    version: int
    journal_index: int
    content_hash: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _task_run_id(task_id: str) -> str:
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    return task_run_id(task_id)


def _validate_ids(task_id: str, key: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise ArtifactValidationError(f"task id {task_id!r} is not a valid task identifier")
    if not _KEY_RE.match(key):
        raise ArtifactValidationError(f"artifact key {key!r} must match {_KEY_RE.pattern} (a single safe segment)")


# ---------------------------------------------------------------------------
# Reading artifact rows from the task journal
# ---------------------------------------------------------------------------


def _artifact_journal_path(sdd_dir: Path, task_id: str) -> Path:
    """Return the task's journal path, refusing any id that escapes ``runs/``.

    Every reader here derives a filesystem path from a task id that reached it
    from a request path, a CLI argument, or a journal row. ``task_run_id`` maps
    separators to ``-`` and prefixes the segment, which makes traversal
    unreachable today, but that is an incidental property of a helper in
    another module and nothing pins it. The reader therefore holds the property
    on its own terms: the id must match the same alphabet ``post_run_artifact``
    enforces, and the resolved path must still sit inside the resolved runs
    directory.

    Raises:
        ArtifactValidationError: If the id is not a valid task identifier, or
            if it resolves outside the runs directory.
    """
    if not _TASK_ID_RE.match(task_id):
        raise ArtifactValidationError(f"task id {task_id!r} is not a valid task identifier")
    base = (sdd_dir / "runs").resolve()
    path = (base / _task_run_id(task_id) / "journal.jsonl").resolve()
    if not path.is_relative_to(base):
        raise ArtifactValidationError(f"task id {task_id!r} resolves outside the runs directory")
    return path


def _row_to_record(row: dict[str, Any]) -> RunArtifactRecord:
    return RunArtifactRecord(
        task_id=str(row.get("task_id", "")),
        key=str(row.get("key", "")),
        artifact_type=str(row.get("artifact_type", "")),
        content_hash=str(row.get("content_hash", "")),
        version=int(row.get("version", 0)),
        prev_version_hash=str(row.get("prev_version_hash", "")),
        spine_entry_hash=str(row.get("spine_entry_hash", "")),
        journal_index=int(row.get("index", 0)),
        journal_event_hash=str(row.get("event_hash", "")),
        link_kind=str(row.get("link_kind", "")),
        size=int(row.get("size", 0)),
    )


def read_artifact_rows(sdd_dir: Path, task_id: str, *, verify: bool = True) -> list[RunArtifactRecord]:
    """Return the task's ``artifact_posted`` records in journal (append) order.

    Args:
        sdd_dir: The ``.sdd`` directory of the run.
        task_id: The task whose artifacts to read.
        verify: When True (default), a task journal that fails Merkle
            verification yields no records (fail-closed).
    """
    from bernstein.core.replay.journal import load_events, verify_journal

    path = _artifact_journal_path(sdd_dir, task_id)
    if not path.is_file():
        return []
    if verify and not verify_journal(path).ok:
        return []
    records: list[RunArtifactRecord] = []
    for row in load_events(path):
        if str(row.get("event", "")) == JOURNAL_EVENT_ARTIFACT_POSTED:
            records.append(_row_to_record(row))
    return records


def latest_versions(sdd_dir: Path, task_id: str) -> dict[str, RunArtifactRecord]:
    """Return the newest record per key for a task (empty when none)."""
    latest: dict[str, RunArtifactRecord] = {}
    for record in read_artifact_rows(sdd_dir, task_id):
        current = latest.get(record.key)
        if current is None or record.version >= current.version:
            latest[record.key] = record
    return latest


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_run_artifact(
    *,
    sdd_dir: Path,
    task_id: str,
    key: str,
    payload: ArtifactPayload,
    actor: str,
    hmac_key: bytes,
    audit_chain: AuditChainStore | None = None,
    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    timestamp: int | None = None,
) -> RunArtifactRecord:
    """Post one artifact against ``task_id`` and return its chain-anchored record.

    Raises:
        ArtifactValidationError: The task id, key, or payload is invalid.
        ArtifactTooLargeError: The serialised payload exceeds ``max_blob_bytes``.
    """
    _validate_ids(task_id, key)
    content = payload.canonical_bytes()
    if len(content) > max_blob_bytes:
        raise ArtifactTooLargeError(len(content), max_blob_bytes)

    # 1. Content-addressed storage (reuses the evidence per-blob cap + gc).
    store = EvidenceStore(sdd_dir / "evidence", max_blob_bytes=max_blob_bytes)
    blob = store.put(content)

    # Serialise version allocation and anchoring for this (task, key). Holding
    # one lock across the version lookup, the spine seal, and the journal append
    # stops two concurrent posts from both selecting version N+1 and referencing
    # the same predecessor (a corrupt version chain).
    with _post_lock(task_id, key):
        # 2. Version chaining by key: the new version references the prior one.
        prior = latest_versions(sdd_dir, task_id).get(key)
        version = (prior.version + 1) if prior is not None else 1
        prev_version_hash = prior.spine_entry_hash if prior is not None else ""

        # 3. Seal the canonical bytes into the lineage spine. The returned entry
        #    hash IS the artifact's identity.
        ts = int(time.time()) if timestamp is None else int(timestamp)
        spine = LineageSpine(sdd_dir / "lineage", run_id=_task_run_id(task_id), hmac_key=hmac_key)
        spine_entry_hash = spine.record(
            artifact_path=f"run-artifacts/{_task_run_id(task_id)}/{key}/v{version}",
            content=content,
            actor=actor,
            step_id=f"artifact:{key}:v{version}",
            model="",
            timestamp=ts,
        )

        # 4. Append the artifact_posted row to the task's Merkle-chained journal.
        from bernstein.core.replay.journal import EventJournal

        journal = EventJournal.resume(_task_run_id(task_id), sdd_dir)
        journal.record(
            JOURNAL_EVENT_ARTIFACT_POSTED,
            task_id=task_id,
            key=key,
            artifact_type=payload.artifact_type,
            content_hash=blob.content_hash,
            version=version,
            prev_version_hash=prev_version_hash,
            spine_entry_hash=spine_entry_hash,
            link_kind=payload.link_kind,
            size=blob.size,
        )
        record = RunArtifactRecord(
            task_id=task_id,
            key=key,
            artifact_type=payload.artifact_type,
            content_hash=blob.content_hash,
            version=version,
            prev_version_hash=prev_version_hash,
            spine_entry_hash=spine_entry_hash,
            journal_index=journal.event_count() - 1,
            journal_event_hash=journal.head(),
            link_kind=payload.link_kind,
            size=blob.size,
        )

    # 5. Mirror into the HMAC audit chain (best-effort: never blocks the post).
    #    The journal + spine records are the primary, independently-verifiable
    #    receipt; the mirror is a convenience for chain-only auditors. A mirror
    #    failure is logged with context so it is never silently swallowed.
    if audit_chain is not None:
        try:
            from bernstein.core.security.audit_chain import record_run_artifact

            record_run_artifact(
                chain=audit_chain,
                task_id=record.task_id,
                key=record.key,
                artifact_type=record.artifact_type,
                content_hash=record.content_hash,
                version=record.version,
                prev_version_hash=record.prev_version_hash,
                spine_entry_hash=record.spine_entry_hash,
                journal_index=record.journal_index,
                journal_event_hash=record.journal_event_hash,
                actor=actor,
            )
        except Exception as exc:  # intentional-broad-except: audit mirror is best-effort
            logger.warning(
                "run_artifact: audit chain mirror failed for task=%s key=%s v%d: %s",
                record.task_id,
                record.key,
                record.version,
                type(exc).__name__,
            )

    return record


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_run_artifacts(sdd_dir: Path, task_id: str, *, hmac_key: bytes) -> list[ArtifactVerifyResult]:
    """Recompute every artifact's blob hash, journal chain, and spine anchor.

    For each ``artifact_posted`` row: the task journal must Merkle-verify (a
    flipped journal byte breaks the chain), the stored blob must rehash to the
    row's ``content_hash`` (a flipped blob byte is caught), and the row's spine
    entry hash must appear in a verified lineage spine binding the same content.
    A failure names the artifact key and its exact journal position.
    """
    from bernstein.core.replay.journal import verify_journal

    path = _artifact_journal_path(sdd_dir, task_id)
    if not path.is_file():
        return []

    journal_ok = verify_journal(path).ok
    # Read rows WITHOUT the fail-closed filter: a tampered journal must still be
    # walked so verification can report tampering rather than "no artifacts".
    rows = read_artifact_rows(sdd_dir, task_id, verify=False)
    if not rows:
        if journal_ok:
            return []
        # Tampering can rename or drop every artifact_posted row so none remain.
        # An invalid journal must fail verification explicitly, never silently
        # read as an empty (clean) artifact set.
        return [
            ArtifactVerifyResult(
                ok=False,
                task_id=task_id,
                key="",
                version=0,
                journal_index=-1,
                reason="task journal Merkle chain does not verify; artifact rows may be hidden by tampering",
            )
        ]

    store = EvidenceStore(sdd_dir / "evidence")
    spine = LineageSpine(sdd_dir / "lineage", run_id=_task_run_id(task_id), hmac_key=hmac_key)
    spine_ok = spine.verify().ok
    spine_by_hash: dict[str, str] = {e.entry_hash: e.content_hash for e in spine.iter_entries()}

    results: list[ArtifactVerifyResult] = []
    for record in rows:
        reason = _verify_one_artifact(record, journal_ok, store, spine_ok, spine_by_hash)
        results.append(
            ArtifactVerifyResult(
                ok=not reason,
                task_id=record.task_id,
                key=record.key,
                version=record.version,
                journal_index=record.journal_index,
                content_hash=record.content_hash,
                reason=reason,
            )
        )
    return results


def _verify_one_artifact(
    record: RunArtifactRecord,
    journal_ok: bool,
    store: EvidenceStore,
    spine_ok: bool,
    spine_by_hash: dict[str, str],
) -> str:
    """Return an empty string when the artifact verifies, else the reason."""
    where = f"key={record.key!r} version={record.version} index={record.journal_index}"
    if not journal_ok:
        return f"task journal Merkle chain does not verify ({where})"
    blob = store.get(record.content_hash)
    if blob is None:
        return f"stored blob missing for {where}"
    actual = content_hash_of(blob)
    if actual != record.content_hash:
        return f"blob content hash {actual} does not match journal row {record.content_hash} ({where})"
    if not spine_ok:
        return f"lineage spine does not verify ({where})"
    anchored = spine_by_hash.get(record.spine_entry_hash)
    if anchored is None:
        return f"spine entry {record.spine_entry_hash} for {where} is not in the lineage spine"
    if anchored != record.content_hash:
        return f"spine anchor binds {anchored}, journal row says {record.content_hash} ({where})"
    return ""


def verify_all_run_artifacts(workdir: Path, *, hmac_key: bytes) -> list[ArtifactVerifyResult]:
    """Verify every task's artifacts under ``workdir/.sdd`` (for ``audit verify``)."""
    from bernstein.core.replay.journal import verify_journal

    sdd_dir = workdir / ".sdd"
    runs_root = sdd_dir / "runs"
    if not runs_root.is_dir():
        return []
    results: list[ArtifactVerifyResult] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        journal_path = run_dir / "journal.jsonl"
        if not journal_path.is_file():
            continue
        task_id = _task_id_from_rows(journal_path)
        if task_id is not None:
            # The row's task id must map back to the journal it was read from.
            # An id that fails validation, or that resolves to some other
            # journal, means the row was rewritten: verifying under it would
            # read a different (or absent) journal and report a clean result
            # for a tampered run.
            try:
                derived = _artifact_journal_path(sdd_dir, task_id)
            except ArtifactValidationError:
                derived = None
            if derived == journal_path.resolve():
                results.extend(verify_run_artifacts(sdd_dir, task_id, hmac_key=hmac_key))
            else:
                results.append(
                    ArtifactVerifyResult(
                        ok=False,
                        task_id=run_dir.name.removeprefix("task-"),
                        key="",
                        version=0,
                        journal_index=-1,
                        reason=(
                            f"artifact row task id does not resolve to its own journal ({run_dir.name}); "
                            "the journal has been tampered with"
                        ),
                    )
                )
            continue
        # No identifiable artifact rows. If this is an artifact journal
        # (``task-*``) whose Merkle chain does not verify, tampering may have
        # hidden every artifact row -- fail explicitly rather than skip.
        if run_dir.name.startswith("task-") and not verify_journal(journal_path).ok:
            results.append(
                ArtifactVerifyResult(
                    ok=False,
                    task_id=run_dir.name.removeprefix("task-"),
                    key="",
                    version=0,
                    journal_index=-1,
                    reason=f"task journal Merkle chain does not verify ({run_dir.name}); artifact rows may be hidden",
                )
            )
    return results


def _task_id_from_rows(journal_path: Path) -> str | None:
    """Return the task id from the first artifact row in a journal, if any."""
    from bernstein.core.replay.journal import load_events

    for row in load_events(journal_path):
        if str(row.get("event", "")) == JOURNAL_EVENT_ARTIFACT_POSTED:
            return str(row.get("task_id", "")) or None
    return None


# ---------------------------------------------------------------------------
# gc liveness
# ---------------------------------------------------------------------------


def live_artifact_content_hashes(sdd_dir: Path) -> set[str]:
    """Return every content hash referenced by a live artifact record.

    Feed this into :meth:`EvidenceStore.gc` so a blob any artifact version
    references -- including superseded prior versions -- is never collected.
    """
    runs_root = sdd_dir / "runs"
    if not runs_root.is_dir():
        return set()
    from bernstein.core.replay.journal import load_events

    live: set[str] = set()
    for run_dir in runs_root.iterdir():
        journal_path = run_dir / "journal.jsonl"
        if not journal_path.is_file():
            continue
        for row in load_events(journal_path):
            if str(row.get("event", "")) == JOURNAL_EVENT_ARTIFACT_POSTED:
                content_hash = str(row.get("content_hash", ""))
                if content_hash:
                    live.add(content_hash)
    return live


__all__ = [
    "ARTIFACT_TYPES",
    "ARTIFACT_TYPE_LINK",
    "ARTIFACT_TYPE_REPORT",
    "ARTIFACT_TYPE_TABLE",
    "JOURNAL_EVENT_ARTIFACT_POSTED",
    "LINK_KINDS",
    "ArtifactClaimError",
    "ArtifactError",
    "ArtifactPayload",
    "ArtifactTooLargeError",
    "ArtifactValidationError",
    "ArtifactVerifyResult",
    "RunArtifactRecord",
    "latest_versions",
    "live_artifact_content_hashes",
    "post_run_artifact",
    "read_artifact_rows",
    "verify_all_run_artifacts",
    "verify_run_artifacts",
]
