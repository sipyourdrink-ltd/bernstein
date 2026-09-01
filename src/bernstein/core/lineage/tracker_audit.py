"""Signed audit log of every tracker state move.

This module records each agent-side tracker write (claim, comment,
transition, attach, fail) as a content-addressed, HMAC-signed JSONL
entry. The on-disk stream lives at ``.sdd/lineage/tracker_audit.jsonl``
and is the auditor-facing artefact for evidence requirements such as
SOX-2026 logging, SOC 2 Type II change-management evidence, and EU AI
Act Article 12 record-keeping.

Design notes:

* Entry shape is intentionally separate from
  :class:`bernstein.core.lineage.entry.LineageEntry` because the
  artefact-write log and the tracker-action log have disjoint fields and
  disjoint readers. The two streams live side-by-side under
  ``.sdd/lineage/``.
* Entries are RFC 8785 JCS canonicalised, then HMAC-SHA256 signed with
  the operator secret. The HMAC binds every field, including the
  ``prev_entry_hash`` link so a tampering attempt is detectable by
  re-running ``verify``.
* The file is append-only with an exclusive flock around each write so
  concurrent agents on the same host cannot interleave bytes.
* ``schema_version`` is recorded inside every entry. Bumping the schema
  requires a parallel reader for the old version (see
  ``docs/lineage/tracker-audit.md``).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac as _hmac
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from bernstein.core.security.canonical import canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

TRACKER_AUDIT_ACTIONS: frozenset[str] = frozenset({"claim", "comment", "transition", "attach", "fail"})

GENESIS_PREV_HASH = "sha256:" + "0" * 64

DEFAULT_LOG_PATH = Path(".sdd/lineage/tracker_audit.jsonl")

TrackerAuditAction = Literal["claim", "comment", "transition", "attach", "fail"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uuid7_hex() -> str:
    """Return a UUIDv7 hex string.

    The Python ``uuid`` module added ``uuid7`` in 3.13. We fall back to
    ``uuid4`` on older interpreters so the module loads everywhere; the
    fallback is still globally unique, just not time-sortable.
    """

    uuid7 = getattr(uuid, "uuid7", None)
    if uuid7 is not None:
        return uuid7().hex
    return uuid.uuid4().hex


def content_hash(blob: bytes) -> str:
    """Return the content-addressed identifier for ``blob``."""

    return "sha256:" + hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackerActor:
    """Identifies the agent that performed the tracker action."""

    session_id: str
    role: str
    model: str


@dataclass(frozen=True, slots=True)
class TrackerAuditEntry:
    """A single signed audit record of a tracker state move.

    Field order is irrelevant on the wire because the canonicaliser
    sorts keys; the dataclass groups related fields for human reading.
    """

    schema_version: int
    id: str
    ts_ns: int
    prev_entry_hash: str
    entry_hash: str
    tracker_name: str
    ticket_id: str
    etag_before: str | None
    etag_after: str | None
    action: str
    actor: TrackerActor
    input_prompt_hash: str
    output_blob_hash: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    idempotency_key: str | None
    lifecycle_event_id: str | None
    signature: str
    failure_category: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            msg = f"unsupported tracker-audit schema_version: {self.schema_version}"
            raise ValueError(msg)
        if self.action not in TRACKER_AUDIT_ACTIONS:
            msg = f"unknown tracker-audit action: {self.action!r}"
            raise ValueError(msg)
        for hash_field, label in (
            (self.prev_entry_hash, "prev_entry_hash"),
            (self.entry_hash, "entry_hash"),
            (self.input_prompt_hash, "input_prompt_hash"),
            (self.output_blob_hash, "output_blob_hash"),
        ):
            if not hash_field.startswith("sha256:"):
                msg = f"{label} must start with 'sha256:', got {hash_field!r}"
                raise ValueError(msg)


def _entry_body(entry: TrackerAuditEntry) -> dict[str, Any]:
    """Return ``entry`` as a plain dict ready for JCS canonicalisation."""

    body = asdict(entry)
    # ``actor`` becomes a nested dict via ``asdict``; flat keys keep the
    # canonical form simple enough for the JCS helper's contract.
    return body


def canonicalise_entry(entry: TrackerAuditEntry) -> bytes:
    """Return the RFC 8785 JCS bytes of ``entry``.

    The ``signature`` field is included verbatim; callers that need the
    bytes the HMAC ran over should use :func:`_signing_payload`.
    """

    return canonical_bytes(_entry_body(entry))


def _signing_payload(entry: TrackerAuditEntry) -> bytes:
    """Return JCS bytes of ``entry`` with ``signature`` blanked.

    The HMAC covers every field except the signature itself, which is
    what makes a substitution attack detectable when the operator key
    is supplied during verification.
    """

    body = _entry_body(entry)
    body["signature"] = ""
    body["entry_hash"] = ""
    return canonical_bytes(body)


def compute_entry_hash(entry: TrackerAuditEntry) -> str:
    """Return the content-addressed entry hash for ``entry``.

    ``entry_hash`` is derived from JCS bytes with the ``signature`` and
    ``entry_hash`` fields blanked so the digest is reproducible from the
    same payload during replay or verification.
    """

    return "sha256:" + hashlib.sha256(_signing_payload(entry)).hexdigest()


def compute_signature(entry: TrackerAuditEntry, key: bytes) -> str:
    """Return the operator-HMAC for ``entry`` under ``key``.

    The HMAC is computed over the same canonical bytes as
    :func:`compute_entry_hash`, so a verifier can replay both checks
    from one canonicalisation pass.
    """

    return _hmac.new(key, _signing_payload(entry), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Locking helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _exclusive_lock(fp: IO[bytes]) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``fp`` for the block body.

    Uses ``fcntl.flock`` on POSIX. On Windows ``fcntl`` is absent so the
    helper degrades to a no-op; multi-writer hosts there are out of
    scope for v1 of this module.
    """

    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return

    fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppendResult:
    """Outcome of appending an entry."""

    entry: TrackerAuditEntry
    line_number: int


class TrackerAuditLog:
    """Append-only signed audit log.

    The log is keyed off a single JSONL path. The constructor does not
    create the file; the first append does, with the parent directory
    materialised on demand.
    """

    def __init__(self, path: Path, *, hmac_key: bytes) -> None:
        self.path: Path = Path(path)
        self._hmac_key: bytes = hmac_key

    # -- append -------------------------------------------------------

    def append(
        self,
        *,
        tracker_name: str,
        ticket_id: str,
        action: TrackerAuditAction,
        actor: TrackerActor,
        input_prompt: bytes,
        output_blob: bytes,
        etag_before: str | None = None,
        etag_after: str | None = None,
        cost_usd: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        idempotency_key: str | None = None,
        lifecycle_event_id: str | None = None,
        failure_category: str | None = None,
        failure_detail: str | None = None,
        ts_ns: int | None = None,
        entry_id: str | None = None,
    ) -> AppendResult:
        """Append a single signed entry. Returns the materialised entry.

        Callers pass the raw ``input_prompt`` and ``output_blob`` bytes;
        the log hashes them on the way in so the operator never has to
        commit the secret-bearing payload to disk. Persisting the
        underlying blobs is the orchestrator's responsibility via the
        content-addressed store.
        """

        if action not in TRACKER_AUDIT_ACTIONS:
            msg = f"unknown tracker-audit action: {action!r}"
            raise ValueError(msg)

        prev_hash = self._tail_hash()
        unsigned = TrackerAuditEntry(
            schema_version=SCHEMA_VERSION,
            id=entry_id or _uuid7_hex(),
            ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
            prev_entry_hash=prev_hash,
            entry_hash=GENESIS_PREV_HASH,  # placeholder; recomputed below
            tracker_name=tracker_name,
            ticket_id=ticket_id,
            etag_before=etag_before,
            etag_after=etag_after,
            action=action,
            actor=actor,
            input_prompt_hash=content_hash(input_prompt),
            output_blob_hash=content_hash(output_blob),
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            idempotency_key=idempotency_key,
            lifecycle_event_id=lifecycle_event_id,
            signature="",
            failure_category=failure_category,
            failure_detail=failure_detail,
        )

        digest = compute_entry_hash(unsigned)
        signed = replace(unsigned, entry_hash=digest)
        signature = compute_signature(signed, self._hmac_key)
        final = replace(signed, signature=signature)

        line = canonical_bytes(_entry_body(final)) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as fp, _exclusive_lock(fp):
            fp.write(line)
            fp.flush()
            os.fsync(fp.fileno())

        return AppendResult(entry=final, line_number=self._count_lines())

    # -- read ---------------------------------------------------------

    def read(self) -> list[TrackerAuditEntry]:
        """Return every entry on disk, in insertion order."""

        return list(self.iter_entries())

    def iter_entries(self) -> Iterator[TrackerAuditEntry]:
        """Yield each entry without holding the whole file in memory."""

        if not self.path.exists():
            return
        with self.path.open("rb") as fp:
            for raw in fp:
                stripped = raw.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped.decode("utf-8"))
                yield _entry_from_payload(payload)

    def filter(
        self,
        *,
        tracker_name: str | None = None,
        ticket_id: str | None = None,
        since_ns: int | None = None,
        until_ns: int | None = None,
    ) -> list[TrackerAuditEntry]:
        """Return entries matching every supplied filter (AND semantics)."""

        out: list[TrackerAuditEntry] = []
        for entry in self.iter_entries():
            if tracker_name is not None and entry.tracker_name != tracker_name:
                continue
            if ticket_id is not None and entry.ticket_id != ticket_id:
                continue
            if since_ns is not None and entry.ts_ns < since_ns:
                continue
            if until_ns is not None and entry.ts_ns > until_ns:
                continue
            out.append(entry)
        return out

    # -- verify -------------------------------------------------------

    def verify(self) -> VerifyResult:
        """Walk the file and check chain integrity + signatures.

        Returns a :class:`VerifyResult` describing the first offending
        line if any. The CLI surface treats ``ok = False`` as a non-zero
        exit code so this method is the single source of truth for
        tampering detection.
        """

        if not self.path.exists():
            return VerifyResult(ok=True, entry_count=0, failures=[])

        failures: list[str] = []
        prev_hash = GENESIS_PREV_HASH
        count = 0

        with self.path.open("rb") as fp:
            for line_no, raw in enumerate(fp, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    failures.append(f"line {line_no}: invalid JSON ({exc.msg})")
                    return VerifyResult(ok=False, entry_count=count, failures=failures)
                try:
                    entry = _entry_from_payload(payload)
                except (TypeError, ValueError, KeyError) as exc:
                    failures.append(f"line {line_no}: schema invalid ({exc})")
                    return VerifyResult(ok=False, entry_count=count, failures=failures)

                if entry.prev_entry_hash != prev_hash:
                    failures.append(
                        f"line {line_no}: prev_entry_hash mismatch (expected {prev_hash}, got {entry.prev_entry_hash})"
                    )
                    return VerifyResult(ok=False, entry_count=count, failures=failures)

                expected_hash = compute_entry_hash(entry)
                if expected_hash != entry.entry_hash:
                    failures.append(f"line {line_no}: entry_hash mismatch (tampered payload)")
                    return VerifyResult(ok=False, entry_count=count, failures=failures)

                expected_sig = compute_signature(entry, self._hmac_key)
                if not _hmac.compare_digest(expected_sig, entry.signature):
                    failures.append(f"line {line_no}: signature mismatch (HMAC failed)")
                    return VerifyResult(ok=False, entry_count=count, failures=failures)

                prev_hash = entry.entry_hash
                count += 1

        return VerifyResult(ok=True, entry_count=count, failures=failures)

    # -- export -------------------------------------------------------

    def export_bundle(
        self,
        out_path: Path,
        *,
        tracker_name: str | None = None,
        ticket_id: str | None = None,
        since_ns: int | None = None,
        until_ns: int | None = None,
    ) -> int:
        """Write a filtered, signed JSONL bundle for an auditor.

        Returns the number of entries written. The bundle preserves the
        on-disk byte form so a third party can verify the chain with
        only the operator HMAC key.
        """

        entries = self.filter(
            tracker_name=tracker_name,
            ticket_id=ticket_id,
            since_ns=since_ns,
            until_ns=until_ns,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as fp:
            for entry in entries:
                fp.write(canonical_bytes(_entry_body(entry)))
                fp.write(b"\n")
        return len(entries)

    # -- internals ----------------------------------------------------

    def _tail_hash(self) -> str:
        """Return the ``entry_hash`` of the last entry, or genesis."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS_PREV_HASH
        # Read tail efficiently: walk lines but only retain the last.
        last_hash = GENESIS_PREV_HASH
        with self.path.open("rb") as fp:
            for raw in fp:
                stripped = raw.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped.decode("utf-8"))
                last_hash = payload["entry_hash"]
        return last_hash

    def _count_lines(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("rb") as fp:
            return sum(1 for raw in fp if raw.strip())


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :meth:`TrackerAuditLog.verify`."""

    ok: bool
    entry_count: int
    failures: list[str] = field(default_factory=list)


def _entry_from_payload(payload: dict[str, Any]) -> TrackerAuditEntry:
    """Reconstruct a :class:`TrackerAuditEntry` from on-disk JSON."""

    actor_payload = payload.get("actor", {})
    actor = TrackerActor(
        session_id=actor_payload["session_id"],
        role=actor_payload["role"],
        model=actor_payload["model"],
    )
    return TrackerAuditEntry(
        schema_version=payload["schema_version"],
        id=payload["id"],
        ts_ns=payload["ts_ns"],
        prev_entry_hash=payload["prev_entry_hash"],
        entry_hash=payload["entry_hash"],
        tracker_name=payload["tracker_name"],
        ticket_id=payload["ticket_id"],
        etag_before=payload.get("etag_before"),
        etag_after=payload.get("etag_after"),
        action=payload["action"],
        actor=actor,
        input_prompt_hash=payload["input_prompt_hash"],
        output_blob_hash=payload["output_blob_hash"],
        cost_usd=payload["cost_usd"],
        tokens_in=payload["tokens_in"],
        tokens_out=payload["tokens_out"],
        idempotency_key=payload.get("idempotency_key"),
        lifecycle_event_id=payload.get("lifecycle_event_id"),
        signature=payload["signature"],
        failure_category=payload.get("failure_category"),
        failure_detail=payload.get("failure_detail"),
    )


def entry_to_body(entry: TrackerAuditEntry) -> dict[str, Any]:
    """Return ``entry`` as a plain dict ready for JCS canonicalisation.

    Public wrapper over the internal serialiser so cross-module callers
    (for instance the A2A lineage envelope) can read an entry's wire body
    without reaching into module privates.
    """

    return _entry_body(entry)


def entry_from_payload(payload: dict[str, Any]) -> TrackerAuditEntry:
    """Reconstruct a :class:`TrackerAuditEntry` from a parsed JSON dict.

    Public wrapper over the internal parser. Validates the entry shape via
    the dataclass ``__post_init__`` invariants on construction.
    """

    return _entry_from_payload(payload)


# ---------------------------------------------------------------------------
# Tracker contract integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageCtx:
    """Per-call lineage context threaded through tracker adapters.

    Adapters that opt into tracker-audit emission accept this object on
    the relevant write methods. The orchestrator constructs it from the
    active session, role, and model. The ``log`` reference lets the
    adapter call :meth:`TrackerAuditLog.append` directly so tests can
    inject a temporary file.
    """

    log: TrackerAuditLog
    actor: TrackerActor
    lifecycle_event_id: str | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


def emit_audit_entry(
    ctx: LineageCtx,
    *,
    tracker_name: str,
    ticket_id: str,
    action: TrackerAuditAction,
    input_prompt: bytes,
    output_blob: bytes,
    etag_before: str | None = None,
    etag_after: str | None = None,
    idempotency_key: str | None = None,
    failure_category: str | None = None,
    failure_detail: str | None = None,
) -> TrackerAuditEntry:
    """Convenience helper to emit a single tracker-audit entry."""

    result = ctx.log.append(
        tracker_name=tracker_name,
        ticket_id=ticket_id,
        action=action,
        actor=ctx.actor,
        input_prompt=input_prompt,
        output_blob=output_blob,
        etag_before=etag_before,
        etag_after=etag_after,
        cost_usd=ctx.cost_usd,
        tokens_in=ctx.tokens_in,
        tokens_out=ctx.tokens_out,
        idempotency_key=idempotency_key,
        lifecycle_event_id=ctx.lifecycle_event_id,
        failure_category=failure_category,
        failure_detail=failure_detail,
    )
    return result.entry


# ---------------------------------------------------------------------------
# Adapter wrapping
# ---------------------------------------------------------------------------


def wrap_adapter(adapter: Any, ctx: LineageCtx) -> AuditingTrackerAdapter:
    """Return ``adapter`` wrapped so every write method emits an audit entry.

    The wrapper preserves the adapter's public surface (``name``,
    ``pull_open_tickets``, etc.) and only intercepts the write methods
    listed in the ticket spec: ``claim_ticket``, ``add_comment``,
    ``transition``, and ``attach_blob``. Other attribute access is
    forwarded transparently.
    """

    return AuditingTrackerAdapter(adapter, ctx)


class AuditingTrackerAdapter:
    """Decorator wrapping a tracker adapter to emit signed audit entries.

    The wrapper is a duck-typed proxy rather than a subclass so it can
    sit in front of any concrete adapter regardless of its inheritance
    chain. Every write method emits exactly one entry on success and
    one entry with ``action="fail"`` on exception, then re-raises.
    """

    def __init__(self, inner: Any, ctx: LineageCtx) -> None:
        self._inner = inner
        self._ctx = ctx

    # -- attribute forwarding ----------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Called only when ``name`` is not found on ``self`` directly,
        # so the wrapped methods below take precedence.
        return getattr(self._inner, name)

    # -- write surface -----------------------------------------------

    def claim_ticket(self, ticket_id: str, agent_id: str, **kwargs: Any) -> Any:
        return self._invoke(
            action="claim",
            ticket_id=ticket_id,
            inner=lambda: self._inner.claim_ticket(ticket_id, agent_id, **kwargs),
            input_prompt=f"claim:{agent_id}".encode(),
            etag_before=kwargs.get("etag"),
        )

    def add_comment(self, ticket_id: str, body: str, **kwargs: Any) -> Any:
        return self._invoke(
            action="comment",
            ticket_id=ticket_id,
            inner=lambda: self._inner.add_comment(ticket_id, body, **kwargs),
            input_prompt=body.encode("utf-8"),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    def transition(self, ticket_id: str, status_id: str, **kwargs: Any) -> Any:
        return self._invoke(
            action="transition",
            ticket_id=ticket_id,
            inner=lambda: self._inner.transition(ticket_id, status_id, **kwargs),
            input_prompt=f"transition:{status_id}".encode(),
            idempotency_key=kwargs.get("idempotency_key"),
            etag_before=kwargs.get("etag"),
        )

    def attach_blob(self, ticket_id: str, blob: bytes, mime: str, **kwargs: Any) -> Any:
        return self._invoke(
            action="attach",
            ticket_id=ticket_id,
            inner=lambda: self._inner.attach_blob(ticket_id, blob, mime, **kwargs),
            input_prompt=mime.encode("utf-8"),
            idempotency_key=kwargs.get("idempotency_key"),
            blob_override=blob,
        )

    # -- shared invocation pipeline -----------------------------------

    def _invoke(
        self,
        *,
        action: TrackerAuditAction,
        ticket_id: str,
        inner: Any,
        input_prompt: bytes,
        idempotency_key: str | None = None,
        etag_before: str | None = None,
        blob_override: bytes | None = None,
    ) -> Any:
        tracker_name = getattr(self._inner, "name", "unknown")
        try:
            result = inner()
        except Exception as exc:
            emit_audit_entry(
                self._ctx,
                tracker_name=tracker_name,
                ticket_id=ticket_id,
                action="fail",
                input_prompt=input_prompt,
                output_blob=str(exc).encode("utf-8"),
                etag_before=etag_before,
                idempotency_key=idempotency_key,
                failure_category=type(exc).__name__,
                failure_detail=str(exc)[:512],
            )
            raise
        # Use the result's repr (or the attached blob for attach actions)
        # as the output blob. The repr is stable across adapters because
        # the contract dataclasses are frozen with declared fields only.
        output_blob = blob_override if blob_override is not None else repr(result).encode("utf-8")
        etag_after = getattr(result, "etag", None)
        emit_audit_entry(
            self._ctx,
            tracker_name=tracker_name,
            ticket_id=ticket_id,
            action=action,
            input_prompt=input_prompt,
            output_blob=output_blob,
            etag_before=etag_before,
            etag_after=etag_after,
            idempotency_key=idempotency_key,
        )
        return result


__all__ = [
    "DEFAULT_LOG_PATH",
    "GENESIS_PREV_HASH",
    "SCHEMA_VERSION",
    "TRACKER_AUDIT_ACTIONS",
    "AppendResult",
    "AuditingTrackerAdapter",
    "LineageCtx",
    "TrackerActor",
    "TrackerAuditAction",
    "TrackerAuditEntry",
    "TrackerAuditLog",
    "VerifyResult",
    "canonicalise_entry",
    "compute_entry_hash",
    "compute_signature",
    "content_hash",
    "emit_audit_entry",
    "entry_from_payload",
    "entry_to_body",
    "wrap_adapter",
]
