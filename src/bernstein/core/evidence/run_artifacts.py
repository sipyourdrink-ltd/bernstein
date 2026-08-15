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

import hashlib
import json
import logging
import os
import posixpath
import re
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from bernstein.core.defaults import (
    ARTIFACT_TYPE_FINDING,
    ARTIFACT_TYPE_LINK,
    ARTIFACT_TYPE_REPORT,
    ARTIFACT_TYPE_TABLE,
    ARTIFACT_TYPES,
    JOURNAL_EVENT_ARTIFACT_POSTED,
    LINK_KINDS,
)
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


#: Artifact key alphabet: a single safe path-ish segment, no leading dot, no
#: separators, so it can be embedded in a spine artifact path unescaped.
#: Anchored with ``\Z``, not ``$``: in Python ``$`` also matches immediately
#: before a trailing newline, which would let a control character through an
#: alphabet that exists precisely to exclude them.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

#: Task id alphabet accepted for artifact posting (matches the MCP tool schema).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}\Z")


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


class ReportArtifactContent(TypedDict):
    """Canonical wire payload for a markdown report artifact."""

    type: Literal["report"]
    body: str


class TableArtifactContent(TypedDict):
    """Canonical wire payload for a table artifact."""

    type: Literal["table"]
    columns: list[str]
    rows: list[list[str]]


class LinkArtifactContent(TypedDict):
    """Canonical wire payload for a link artifact."""

    type: Literal["link"]
    url: str
    kind: str


class FindingArtifactContent(TypedDict):
    """Canonical wire payload for a normalized SARIF finding artifact."""

    type: Literal["finding"]
    address: str
    identity: dict[str, Any]
    location: dict[str, Any]
    provenance: dict[str, str]
    sarif_result: dict[str, Any]


type ArtifactContent = ReportArtifactContent | TableArtifactContent | LinkArtifactContent | FindingArtifactContent


def _required_mapping(value: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, Mapping):
        raise ArtifactValidationError(f"finding SARIF result is missing required field {path}.{key}")
    return child


def _required_string(value: Mapping[str, Any], key: str, path: str) -> str:
    child = value.get(key)
    if not isinstance(child, str) or not child:
        raise ArtifactValidationError(f"finding SARIF result is missing required field {path}.{key}")
    return child


def _canonical_text(value: str) -> str:
    """Fold the platform-dependent spellings of the same text into one form.

    Two checkouts of the same source must address a finding identically, so the
    preimage may not carry anything the platform chose rather than the author:

    * line endings collapse to ``\\n`` -- a CRLF checkout on Windows and an LF
      checkout on Linux are the same snippet; and
    * the result is NFC-normalised -- macOS hands back decomposed (NFD) path
      and text bytes where Linux hands back composed (NFC) ones, and ``café``
      is one filename, not two.

    This repairs where ``core.tasks.artifacts._canonical_text_bytes`` rejects,
    and the difference is deliberate. There the text *is* the artifact, so a
    caller shipping two byte-different spellings of it should hear about it.
    Here the text is only a projection into an address -- the SARIF result is
    stored verbatim beside it -- and the normal form was chosen by the
    scanner's filesystem, not by anyone we can send an error to.
    """
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalise_artifact_uri(uri: str) -> str:
    normalised = posixpath.normpath(_canonical_text(uri).replace("\\", "/"))
    if normalised in {"", "."}:
        raise ArtifactValidationError("finding SARIF result has an empty normalized artifact URI")
    return normalised.removeprefix("./")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _build_finding_content(
    sarif_result: Mapping[str, Any],
    *,
    tool: str,
    tool_version: str,
    pinned_ruleset_or_feed_digest: str,
    invocation_argv_hash: str,
    target: str,
) -> FindingArtifactContent:
    """Normalize one SARIF 2.1.0 result into a provenance-bound finding."""
    rule_id = _required_string(sarif_result, "ruleId", "result")
    locations = sarif_result.get("locations")
    if not isinstance(locations, list) or not locations or not isinstance(locations[0], Mapping):
        raise ArtifactValidationError("finding SARIF result is missing required field result.locations[0]")
    physical = _required_mapping(locations[0], "physicalLocation", "result.locations[0]")
    artifact_location = _required_mapping(physical, "artifactLocation", "result.locations[0].physicalLocation")
    uri = _normalise_artifact_uri(
        _required_string(artifact_location, "uri", "result.locations[0].physicalLocation.artifactLocation")
    )
    region = _required_mapping(physical, "region", "result.locations[0].physicalLocation")
    snippet = _required_mapping(region, "snippet", "result.locations[0].physicalLocation.region")
    snippet_text = _required_string(snippet, "text", "result.locations[0].physicalLocation.region.snippet")

    start_line = region.get("startLine")
    end_line = region.get("endLine", start_line)
    if not isinstance(start_line, int) or start_line < 1:
        raise ArtifactValidationError(
            "finding SARIF result is missing required field result.locations[0].physicalLocation.region.startLine"
        )
    if not isinstance(end_line, int) or end_line < start_line:
        raise ArtifactValidationError(
            "finding SARIF result has invalid field result.locations[0].physicalLocation.region.endLine"
        )
    start_column = region.get("startColumn", 1)
    end_column = region.get("endColumn", start_column)
    if not isinstance(start_column, int) or start_column < 1:
        raise ArtifactValidationError(
            "finding SARIF result has invalid field result.locations[0].physicalLocation.region.startColumn"
        )
    if not isinstance(end_column, int) or end_column < start_column:
        raise ArtifactValidationError(
            "finding SARIF result has invalid field result.locations[0].physicalLocation.region.endColumn"
        )

    provenance = {
        "tool": tool,
        "tool_version": tool_version,
        "pinned_ruleset_or_feed_digest": pinned_ruleset_or_feed_digest,
        "invocation_argv_hash": invocation_argv_hash,
        "target": target,
    }
    for key, value in provenance.items():
        if not isinstance(value, str) or not value:
            raise ArtifactValidationError(f"finding provenance requires non-empty {key}")

    identity: dict[str, Any] = {
        "rule_id": rule_id,
        "artifact_uri": uri,
        # Absolute lines are deliberately excluded: inserting blank lines above
        # an unchanged finding must not change its content address.
        "region": {
            "line_span": end_line - start_line,
            "start_column": start_column,
            "end_column": end_column,
        },
        "snippet_hash": _sha256_bytes(_canonical_text(snippet_text).encode("utf-8")),
    }
    address_preimage = {"identity": identity, "provenance": provenance}
    address = _sha256_bytes(_canonical_json_bytes(address_preimage))
    return cast(
        FindingArtifactContent,
        {
            "type": ARTIFACT_TYPE_FINDING,
            "address": address,
            "identity": identity,
            "location": {
                "artifact_uri": uri,
                "start_line": start_line,
                "end_line": end_line,
                "start_column": start_column,
                "end_column": end_column,
            },
            "provenance": provenance,
            "sarif_result": dict(sarif_result),
        },
    )


def _verify_finding_content(blob: bytes, where: str) -> str:
    try:
        content = json.loads(blob)
        if not isinstance(content, dict):
            raise ArtifactValidationError("finding payload must be a JSON object")
        provenance = content.get("provenance")
        sarif_result = content.get("sarif_result")
        if not isinstance(provenance, Mapping):
            raise ArtifactValidationError("finding payload is missing required field provenance")
        if not isinstance(sarif_result, Mapping):
            raise ArtifactValidationError("finding payload is missing required field sarif_result")
        expected = _build_finding_content(
            sarif_result,
            tool=str(provenance.get("tool", "")),
            tool_version=str(provenance.get("tool_version", "")),
            pinned_ruleset_or_feed_digest=str(provenance.get("pinned_ruleset_or_feed_digest", "")),
            invocation_argv_hash=str(provenance.get("invocation_argv_hash", "")),
            target=str(provenance.get("target", "")),
        )
    except (ArtifactValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"finding payload does not verify: {exc} ({where})"
    if content.get("address") != expected["address"]:
        return (
            f"recorded finding address {content.get('address')!r} does not match recomputed "
            f"address {expected['address']} ({where})"
        )
    if content.get("identity") != expected["identity"]:
        return f"recorded finding identity does not match normalized SARIF result ({where})"
    if content.get("location") != expected["location"]:
        return f"recorded finding location does not match normalized SARIF result ({where})"
    if content.get("type") != ARTIFACT_TYPE_FINDING:
        return f"recorded finding payload has wrong artifact type ({where})"
    return ""


class RunArtifactRecordDict(TypedDict):
    """JSON representation shared by artifact API, SSE, and CLI boundaries."""

    task_id: str
    key: str
    artifact_type: str
    content_hash: str
    version: int
    prev_version_hash: str
    spine_entry_hash: str
    journal_index: int
    journal_event_hash: str
    link_kind: str
    size: int


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """A typed, canonically-serialisable artifact body.

    One of four shapes, discriminated by :attr:`artifact_type`. Construct via
    :meth:`report`, :meth:`table`, :meth:`link`, or :meth:`finding` so the shape is validated at
    the boundary; :meth:`canonical_bytes` yields the exact content-addressed
    bytes.
    """

    artifact_type: str
    body: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    url: str = ""
    link_kind: str = ""
    finding_json: str = ""

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

    @staticmethod
    def finding(
        sarif_result: Mapping[str, Any],
        *,
        tool: str,
        tool_version: str,
        pinned_ruleset_or_feed_digest: str,
        invocation_argv_hash: str,
        target: str,
    ) -> ArtifactPayload:
        """A normalized SARIF 2.1.0 finding with provenance-bound identity."""
        content = _build_finding_content(
            sarif_result,
            tool=tool,
            tool_version=tool_version,
            pinned_ruleset_or_feed_digest=pinned_ruleset_or_feed_digest,
            invocation_argv_hash=invocation_argv_hash,
            target=target,
        )
        finding_json = _canonical_json_bytes(content).decode("utf-8")
        return ArtifactPayload(artifact_type=ARTIFACT_TYPE_FINDING, finding_json=finding_json)

    def to_content_dict(self) -> ArtifactContent:
        """Return the type-specific fields that define the artifact content."""
        if self.artifact_type == ARTIFACT_TYPE_REPORT:
            return cast(ReportArtifactContent, {"type": ARTIFACT_TYPE_REPORT, "body": self.body})
        if self.artifact_type == ARTIFACT_TYPE_TABLE:
            return cast(
                TableArtifactContent,
                {
                    "type": ARTIFACT_TYPE_TABLE,
                    "columns": list(self.columns),
                    "rows": [list(r) for r in self.rows],
                },
            )
        if self.artifact_type == ARTIFACT_TYPE_LINK:
            return cast(
                LinkArtifactContent,
                {"type": ARTIFACT_TYPE_LINK, "url": self.url, "kind": self.link_kind},
            )
        if self.artifact_type == ARTIFACT_TYPE_FINDING:
            try:
                content = json.loads(self.finding_json)
            except json.JSONDecodeError as exc:
                raise ArtifactValidationError(f"finding artifact is not valid JSON: {exc}") from exc
            if not isinstance(content, dict):
                raise ArtifactValidationError("finding artifact must be a JSON object")
            return cast(FindingArtifactContent, content)
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

    def to_dict(self) -> RunArtifactRecordDict:
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
    """Verification verdict for one artifact version.

    ``ok`` covers the exact artifact bytes plus their journal-row and lineage-
    spine binding. ``journal_identity`` is deliberately separate: those facts
    can verify even when no external terminal-head seal identifies the complete
    task journal.
    """

    ok: bool
    task_id: str
    key: str
    version: int
    journal_index: int
    content_hash: str = ""
    reason: str = ""
    journal_identity: str = "unverifiable"


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
    """Return the task journal path, proven to sit under ``<sdd>/runs``.

    Two checks, in order, because they answer different questions.

    First the id must match the alphabet ``post_run_artifact`` enforces.
    ``task_run_id`` maps every separator and control character to ``-``, so a
    malformed id is not a traversal risk - but it is a COLLISION risk: ``a/b``,
    ``a\\nb`` and ``a-b`` all normalise to the same run directory, and two
    tasks that address the same journal can read each other's artifacts.
    Refusing the id keeps distinct tasks distinct, which sanitising alone
    cannot do.

    Then the derived run id goes through the shared containment barrier and the
    *returned* value is the normalised, checked path. Readers open that value
    rather than the raw join, so a crafted id or a symlinked run directory
    cannot address a journal outside the runs root. The barrier is shared on
    purpose: a local copy of this check drifted from it once already.

    Raises:
        ArtifactValidationError: The id is not a valid task identifier.
        PathContainmentError: The derived run id escapes the runs root.
    """
    from bernstein.core.tasks.checkpoint_retry import task_journal_path

    if not _TASK_ID_RE.match(task_id):
        raise ArtifactValidationError(f"task id {task_id!r} is not a valid task identifier")
    return task_journal_path(sdd_dir, task_id)


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

    A task id too long to name a file reads as empty, matching the
    "no journal" case: ``_TASK_ID_RE`` accepts 256 characters and
    ``task_run_id`` adds a prefix, so the derived component can exceed
    ``NAME_MAX``. An id that is not a valid task identifier reads as empty for
    the same reason: it addresses no task, and every caller here (the CLI
    listing, the dashboard projection) already treats "addresses nothing" as
    "nothing to show". The typed refusal belongs on the write path, where
    :func:`post_run_artifact` validates before it anchors anything.

    A *containment* failure is deliberately not caught - a traversal or symlink
    escape must surface, never read as empty.
    """
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.path_containment import PathTooLongError

    try:
        path = _artifact_journal_path(sdd_dir, task_id)
    except (ArtifactValidationError, PathTooLongError):
        return []
    if not path.is_file():
        return []
    if verify:
        result = verify_journal(path)
        if not result.chain_consistent or result.discarded_line_indices:
            return []
    records: list[RunArtifactRecord] = []
    for row in load_events(path).events:
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

    An id that cannot name any task has nothing to verify, so it reads as
    empty rather than raising, matching :func:`read_artifact_rows`.
    """
    from bernstein.core.replay.journal import verify_journal

    try:
        path = _artifact_journal_path(sdd_dir, task_id)
    except ArtifactValidationError:
        return []
    if not path.is_file():
        return []

    journal_result = verify_journal(path)
    journal_ok = journal_result.chain_consistent and not journal_result.discarded_line_indices
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
                reason="task journal Merkle chain/reader coverage does not verify; artifact rows may be hidden",
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
                journal_identity=journal_result.identity.value,
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
        return f"task journal Merkle chain/reader coverage does not verify ({where})"
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
    if record.artifact_type == ARTIFACT_TYPE_FINDING:
        return _verify_finding_content(blob, where)
    return ""


def verify_all_run_artifacts(workdir: Path, *, hmac_key: bytes) -> list[ArtifactVerifyResult]:
    """Verify every task's artifacts under ``workdir/.sdd`` (for ``audit verify``)."""
    from bernstein.core.replay.journal import contained_run_journal, verify_journal

    sdd_dir = workdir / ".sdd"
    runs_root = sdd_dir / "runs"
    if not runs_root.is_dir():
        return []
    results: list[ArtifactVerifyResult] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        journal_path = contained_run_journal(runs_root, run_dir.name)
        if journal_path is None or not journal_path.is_file():
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
            # Compare under the same lexical normalisation the deriver uses,
            # so the equality is not decided by symlinks on either side.
            if derived is not None and str(derived) == os.path.normpath(str(journal_path)):
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
        # (``task-*``) whose chain/reader coverage does not verify, tampering may have
        # hidden every artifact row -- fail explicitly rather than skip.
        journal_result = verify_journal(journal_path)
        if run_dir.name.startswith("task-") and (
            not journal_result.chain_consistent or journal_result.discarded_line_indices
        ):
            results.append(
                ArtifactVerifyResult(
                    ok=False,
                    task_id=run_dir.name.removeprefix("task-"),
                    key="",
                    version=0,
                    journal_index=-1,
                    reason=f"task journal Merkle chain/reader coverage does not verify ({run_dir.name}); "
                    "artifact rows may be hidden",
                )
            )
    return results


def _task_id_from_rows(journal_path: Path) -> str | None:
    """Return the task id from the first artifact row in a journal, if any."""
    from bernstein.core.replay.journal import load_events

    for row in load_events(journal_path).events:
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
    from bernstein.core.replay.journal import contained_run_journal, load_events

    live: set[str] = set()
    for run_dir in runs_root.iterdir():
        journal_path = contained_run_journal(runs_root, run_dir.name)
        if journal_path is None or not journal_path.is_file():
            continue
        for row in load_events(journal_path).events:
            if str(row.get("event", "")) == JOURNAL_EVENT_ARTIFACT_POSTED:
                content_hash = str(row.get("content_hash", ""))
                if content_hash:
                    live.add(content_hash)
    return live


__all__ = [
    "ARTIFACT_TYPES",
    "ARTIFACT_TYPE_FINDING",
    "ARTIFACT_TYPE_LINK",
    "ARTIFACT_TYPE_REPORT",
    "ARTIFACT_TYPE_TABLE",
    "JOURNAL_EVENT_ARTIFACT_POSTED",
    "LINK_KINDS",
    "ArtifactClaimError",
    "ArtifactContent",
    "ArtifactError",
    "ArtifactPayload",
    "ArtifactTooLargeError",
    "ArtifactValidationError",
    "ArtifactVerifyResult",
    "RunArtifactRecord",
    "RunArtifactRecordDict",
    "latest_versions",
    "live_artifact_content_hashes",
    "post_run_artifact",
    "read_artifact_rows",
    "verify_all_run_artifacts",
    "verify_run_artifacts",
]
