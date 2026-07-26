"""MCP Tasks extension: verifiable long-running run handles (issue #2364).

A recent MCP spec revision (see :data:`SPEC_REVISION`) adds an official
Tasks extension for long-running operations: a call returns a *task handle*
a client can poll rather than holding a session open for the whole run. For
a determinism-and-audit orchestrator the handle must not be free-standing
mutable state - it is a **pure projection of the run journal**. The handle
carries the run's Merkle journal head and embeds the run's audit-chain head,
so a client that watched a task can later prove the task it observed
corresponds to the audited run.

This module supplies the extension at the protocol level, deliberately with
no dependency on an unreleased MCP SDK. It provides:

* :func:`project_task_status` - a pure fold from the ordered run journal
  onto the Tasks extension status surface (``working`` / ``input_required``
  / ``completed`` / ``failed`` / ``cancelled``). The status is never mutated
  separately; it is recomputed from the journal on every read.

* :class:`RunHandle` - the verifiable receipt a client receives. Its
  ``receipt_hash`` is a content-addressed digest over the projected status,
  the journal head, and the embedded chain head, so the handle *is* the
  proof: strip the journal or the chain and the handle means nothing.

* :func:`verify_handle` - reprojects the handle from a journal and confirms
  the progress claim is faithful (a forged status fails).

* :func:`verify_handle_chain_head` - confirms the handle's embedded chain
  head equals the completed run's audit-chain head and the chain verifies
  (AC2). This is the offline verifier; ``bernstein audit verify`` walks the
  same chain.

* :class:`TraceContext` / :func:`ingest_trace_context` - parse W3C Trace
  Context arriving in a request ``_meta`` (AC3), and
  :func:`record_trace_context_into_lineage` records it into the lineage of
  an artefact the run produced, so a host's trace connects to the run's
  outputs.

* :func:`poll_task_handle` - the stateless polling fallback (AC1, AC4): any
  server instance reprojects the same handle from the on-disk journal and
  the current chain head, so a client that only polls (no session, no Tasks
  support) still drives the run to completion.

Determinism
-----------
Every function here is pure of clocks and sockets. The poll token and the
receipt hash are canonical (sorted-key, compact-separator JSON), so two
reprojections of the same run yield byte-identical wire values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from bernstein.core.protocols.mcp.stateless_core import (
    decode_request_state,
    encode_request_state,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from bernstein.core.replay.progress import ProgressVector
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "SPEC_REVISION",
    "TASK_CANCELLED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_INPUT_REQUIRED",
    "TASK_STATUSES",
    "TASK_WORKING",
    "TERMINAL_STATUSES",
    "RunHandle",
    "TraceContext",
    "decode_poll_token",
    "ingest_trace_context",
    "poll_task_handle",
    "project_task_status",
    "record_trace_context_into_lineage",
    "verify_handle",
    "verify_handle_chain_head",
]

#: The MCP Tasks extension revision this implementation pins. The extension
#: finalizes late July 2026; building at the protocol level against a pinned
#: revision (rather than an unreleased SDK) bounds the churn, and the polling
#: fallback keeps clients without Tasks support working (issue #2364 risks).
SPEC_REVISION = "2026-07-28"

# ---------------------------------------------------------------------------
# Task status surface (Tasks extension) + journal projection
# ---------------------------------------------------------------------------

#: A run is executing. The default status for a submitted run whose journal
#: carries no terminal lifecycle event yet.
TASK_WORKING = "working"

#: The run is paused awaiting client input (the Tasks-extension retry point).
TASK_INPUT_REQUIRED = "input_required"

#: The run finished successfully.
TASK_COMPLETED = "completed"

#: The run finished with a failure.
TASK_FAILED = "failed"

#: The run was cancelled.
TASK_CANCELLED = "cancelled"

#: Every valid Tasks-extension status, ordered from live to terminal.
TASK_STATUSES: frozenset[str] = frozenset(
    {TASK_WORKING, TASK_INPUT_REQUIRED, TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
)

#: Statuses a run cannot leave. Once a terminal event lands in the journal
#: the projection is monotone: a later non-terminal event never downgrades a
#: completed / failed / cancelled run.
TERMINAL_STATUSES: frozenset[str] = frozenset({TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

#: Journal event type -> Tasks-extension status. The keys are the run-level
#: lifecycle events the orchestrator journal emits; every other event type is
#: ignored by the projection (it does not move the task status). Adding a new
#: lifecycle event means adding one entry here.
_STATUS_BY_EVENT: dict[str, str] = {
    "run_started": TASK_WORKING,
    "task_claimed": TASK_WORKING,
    "task_in_progress": TASK_WORKING,
    "task_progress": TASK_WORKING,
    "input_required": TASK_INPUT_REQUIRED,
    "task_input_required": TASK_INPUT_REQUIRED,
    "task_completed": TASK_COMPLETED,
    "run_completed": TASK_COMPLETED,
    "task_failed": TASK_FAILED,
    "run_failed": TASK_FAILED,
    "task_cancelled": TASK_CANCELLED,
    "run_cancelled": TASK_CANCELLED,
}


def project_task_status(events: Iterable[Mapping[str, Any]]) -> str:
    """Fold an ordered run journal onto a Tasks-extension status.

    The status is a **pure projection** of the journal, never separately
    mutated: two reads of the same journal always agree, and a client cannot
    move a handle to ``completed`` without a matching terminal event in the
    chain. The fold is monotone at the terminal boundary - once a terminal
    event lands, a later non-terminal event (for example a stray progress
    row) does not downgrade the status.

    Args:
        events: Ordered journal rows (each a mapping with an ``event`` key,
            as produced by :func:`bernstein.core.replay.journal.load_events`).

    Returns:
        One of :data:`TASK_STATUSES`; :data:`TASK_WORKING` for a journal with
        no recognised lifecycle event.
    """
    status = TASK_WORKING
    for row in events:
        event_type = str(row.get("event", ""))
        mapped = _STATUS_BY_EVENT.get(event_type)
        if mapped is None:
            continue
        if status in TERMINAL_STATUSES:
            # Monotone terminal boundary: never leave a terminal state.
            continue
        status = mapped
    return status


def _journal_head(events: Iterable[Mapping[str, Any]]) -> str:
    """Return the Merkle head hash of a journal from its ordered rows.

    The head is the last row's ``event_hash``; an empty journal has the
    genesis (empty) head. Reading it from the rows keeps handle construction
    a pure projection with no second file read.
    """
    head = ""
    for row in events:
        candidate = str(row.get("event_hash", ""))
        if candidate:
            head = candidate
    return head


# ---------------------------------------------------------------------------
# Run handle (the verifiable receipt)
# ---------------------------------------------------------------------------


def _receipt_hash(
    *,
    task_id: str,
    run_id: str,
    status: str,
    journal_head: str,
    chain_head: str,
    spec_revision: str,
    trace_id: str,
    progress_hash: str,
) -> str:
    """Return the content-addressed digest that identifies a run handle.

    The pre-image is a canonical field tuple, so two handles projecting the
    same run state hash identically and any tampered field surfaces as a
    different receipt hash. The progress vector's hash is included so a client
    cannot swap the carried progress without invalidating the receipt. Wall
    clock is deliberately excluded: the receipt is a deterministic projection,
    not a timestamped record.
    """
    preimage = json.dumps(
        {
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "journal_head": journal_head,
            "chain_head": chain_head,
            "spec_revision": spec_revision,
            "trace_id": trace_id,
            "progress_hash": progress_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


@dataclass(frozen=True, slots=True)
class RunHandle:
    """A pollable, verifiable handle for a long-running MCP run.

    The handle is a projection of the run journal, not standalone state:
    :attr:`status` is recomputed from the journal, :attr:`journal_head` is the
    run's Merkle head, and :attr:`chain_head` is the audit-chain head embedded
    so a client can later verify the task it watched against the audited run.

    Attributes:
        task_id: The Tasks-extension task id (opaque to the client).
        run_id: The orchestration run whose journal this handle projects.
        status: One of :data:`TASK_STATUSES`, projected from the journal.
        journal_head: The run journal's Merkle head hash (the run identity).
        chain_head: The audit-chain head hash embedded at handle creation.
        spec_revision: The pinned Tasks-extension revision.
        trace_id: The ingested W3C trace id of the calling host, if any.
    """

    task_id: str
    run_id: str
    status: str
    journal_head: str
    chain_head: str
    spec_revision: str = SPEC_REVISION
    trace_id: str = ""
    #: The task's chain-computed progress vector (#2553), when the caller has
    #: projected one. Carried on the handle so a poller sees how far along the
    #: task is, not just its lifecycle status. It is a projection of journaled
    #: work, never self-reported: a worker moves it only by doing real work.
    progress: ProgressVector | None = None

    @property
    def progress_hash(self) -> str:
        """Stable hash of the carried progress vector, or ``""`` when absent."""
        return self.progress.vector_hash() if self.progress is not None else ""

    @property
    def receipt_hash(self) -> str:
        """The content-addressed digest of this handle (the proof anchor)."""
        return _receipt_hash(
            task_id=self.task_id,
            run_id=self.run_id,
            status=self.status,
            journal_head=self.journal_head,
            chain_head=self.chain_head,
            spec_revision=self.spec_revision,
            trace_id=self.trace_id,
            progress_hash=self.progress_hash,
        )

    @property
    def is_terminal(self) -> bool:
        """Whether the run has reached a terminal status."""
        return self.status in TERMINAL_STATUSES

    @property
    def poll_token(self) -> str:
        """An opaque, stateless token any server instance decodes to re-poll.

        The token carries only the run identity and the pinned revision - no
        session id and no server-side state - so a different instance answers
        a poll by reprojecting from the on-disk journal alone (AC1, AC4).
        """
        return encode_request_state(
            {
                "task_id": self.task_id,
                "run_id": self.run_id,
                "spec_revision": self.spec_revision,
            }
        )

    #: JSON-schema fragment per wire field, keyed by the exact key
    #: :meth:`to_wire` emits. :meth:`wire_schema` is generated from this
    #: table rather than written by hand, and a parity test asserts the
    #: table's keys equal a live ``to_wire`` body's keys, so the advertised
    #: output schema cannot drift from the emitted handle (#3086).
    _WIRE_FIELD_SCHEMAS: ClassVar[dict[str, dict[str, Any]]] = {
        "taskId": {"type": "string"},
        "runId": {"type": "string"},
        "status": {"type": "string", "enum": sorted(TASK_STATUSES)},
        "journalHead": {"type": "string"},
        "chainHead": {"type": "string"},
        "specRevision": {"type": "string"},
        "traceId": {"type": "string"},
        "receiptHash": {"type": "string"},
        "pollToken": {"type": "string"},
        "progress": {"type": ["object", "null"]},
        "progressHash": {"type": "string"},
    }

    @classmethod
    def wire_schema(cls) -> dict[str, Any]:
        """Return the JSON schema of :meth:`to_wire`, generated field-for-field.

        Every field is required and no extra field is permitted: the handle
        is a deterministic projection, so its wire shape is exact. The
        schema is built from :data:`_WIRE_FIELD_SCHEMAS` so a new wire field
        is advertised the moment it exists.
        """
        return {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(cls._WIRE_FIELD_SCHEMAS),
            "properties": {key: dict(value) for key, value in cls._WIRE_FIELD_SCHEMAS.items()},
        }

    def to_wire(self) -> dict[str, Any]:
        """Return the Tasks-extension task-handle body for a client."""
        return {
            "taskId": self.task_id,
            "runId": self.run_id,
            "status": self.status,
            "journalHead": self.journal_head,
            "chainHead": self.chain_head,
            "specRevision": self.spec_revision,
            "traceId": self.trace_id,
            "receiptHash": self.receipt_hash,
            "pollToken": self.poll_token,
            "progress": self.progress.to_wire() if self.progress is not None else None,
            "progressHash": self.progress_hash,
        }

    @staticmethod
    def from_journal(
        *,
        task_id: str,
        run_id: str,
        events: Iterable[Mapping[str, Any]],
        chain_head: str,
        trace_id: str = "",
        progress: ProgressVector | None = None,
    ) -> RunHandle:
        """Project a handle from a run journal and the current chain head.

        Args:
            task_id: The Tasks-extension task id.
            run_id: The run whose journal is projected.
            events: Ordered journal rows for the run.
            chain_head: The audit-chain head hash to embed.
            trace_id: Optional ingested W3C trace id.

        Returns:
            A :class:`RunHandle` whose status and journal head are derived
            from ``events``.
        """
        rows = list(events)
        return RunHandle(
            task_id=task_id,
            run_id=run_id,
            status=project_task_status(rows),
            journal_head=_journal_head(rows),
            chain_head=chain_head,
            trace_id=trace_id,
            progress=progress,
        )


def decode_poll_token(token: str) -> dict[str, Any]:
    """Return the run identity decoded from a handle poll token.

    Raises:
        ValueError: When the token is not a valid base64 JSON object (the
            message names ``requestState`` per the stateless-core contract).
    """
    return decode_request_state(token)


def verify_handle(
    handle: RunHandle,
    events: Iterable[Mapping[str, Any]],
    *,
    progress: ProgressVector | None = None,
) -> tuple[bool, str | None]:
    """Confirm a handle is a faithful projection of a run journal.

    Reprojects the status and journal head from ``events`` and recomputes the
    receipt hash. A handle whose status was forged (for example a client
    claiming ``completed`` while the journal shows only ``working``) fails,
    because the projected receipt hash will not match.

    When a handle carries a progress vector, the vector is **not** trusted from
    the handle: the caller must supply the authoritative ``progress`` (projected
    from the journal, ledger, and evidence via
    :func:`bernstein.core.replay.progress.project_task_progress`), and the
    presented vector must match it. Because ``progress_hash`` is part of the
    receipt pre-image, a client cannot swap the vector without also failing the
    receipt-hash check.

    Args:
        handle: The handle presented by a client.
        events: The authoritative ordered journal rows for the run.
        progress: The authoritative progress vector, required when the handle
            carries one; ignored when the handle has no progress.

    Returns:
        ``(True, None)`` when the handle matches the journal projection, or
        ``(False, reason)`` naming the first mismatch.
    """
    rows = list(events)
    expected_status = project_task_status(rows)
    if handle.status != expected_status:
        return False, f"status {handle.status!r} does not match journal projection {expected_status!r}"
    expected_head = _journal_head(rows)
    if handle.journal_head != expected_head:
        return False, "journal_head does not match the run journal"
    if handle.progress is not None:
        if progress is None:
            return False, "handle carries progress but no authoritative progress vector was supplied"
        if handle.progress.vector_hash() != progress.vector_hash():
            return False, "progress vector does not match the authoritative projection"
    expected = RunHandle(
        task_id=handle.task_id,
        run_id=handle.run_id,
        status=expected_status,
        journal_head=expected_head,
        chain_head=handle.chain_head,
        spec_revision=handle.spec_revision,
        trace_id=handle.trace_id,
        progress=progress if handle.progress is not None else None,
    )
    if expected.receipt_hash != handle.receipt_hash:
        return False, "receipt_hash does not match the projected handle"
    return True, None


def verify_handle_chain_head(handle: RunHandle, chain: AuditChainStore) -> tuple[bool, str | None]:
    """Confirm a handle's embedded chain head matches the audited run (AC2).

    Checks that the audit chain verifies end-to-end and that its current head
    equals the head the handle embedded. A client that watched a task can run
    this against the completed run's chain to prove the handle corresponds to
    the audited run - the same chain ``bernstein audit verify`` walks.

    Args:
        handle: The handle whose ``chain_head`` is checked.
        chain: The audit chain store for the run.

    Returns:
        ``(True, None)`` on a match against a verifying chain, else
        ``(False, reason)``.
    """
    ok, errors = chain.verify()
    if not ok:
        return False, f"audit chain does not verify: {'; '.join(errors) or 'unknown'}"
    if handle.chain_head != chain.prev_chain_digest:
        return False, "embedded chain_head does not match the audit chain head"
    return True, None


# ---------------------------------------------------------------------------
# W3C Trace Context ingestion + lineage anchoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceContext:
    """A W3C Trace Context parsed from a request ``_meta`` field.

    Attributes:
        trace_id: 16-byte trace id as 32 lowercase hex chars.
        parent_id: 8-byte parent span id as 16 lowercase hex chars.
        trace_flags: 1-byte flags as 2 hex chars (e.g. ``01`` sampled).
        tracestate: The vendor ``tracestate`` string, empty when absent.
        baggage: The W3C ``baggage`` string, empty when absent.
    """

    trace_id: str
    parent_id: str
    trace_flags: str = "01"
    tracestate: str = ""
    baggage: str = ""

    @property
    def traceparent(self) -> str:
        """Return the canonical ``traceparent`` header value."""
        return f"00-{self.trace_id}-{self.parent_id}-{self.trace_flags}"


def _is_hex(value: str, *, length: int) -> bool:
    """Whether ``value`` is exactly ``length`` lowercase hex chars."""
    if len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def ingest_trace_context(meta: Mapping[str, Any]) -> TraceContext | None:
    """Parse a W3C Trace Context from a request ``_meta`` mapping (AC3).

    Accepts a version-00 ``traceparent`` with a non-zero trace id and span id.
    A missing or malformed header yields ``None`` rather than raising, so a
    request without trace context is simply not anchored.

    Args:
        meta: The per-request ``_meta`` mapping arriving from the client.

    Returns:
        A :class:`TraceContext`, or ``None`` when no valid header is present.
    """
    raw = meta.get("traceparent")
    if not isinstance(raw, str):
        return None
    parts = raw.split("-")
    if len(parts) != 4:
        return None
    version, trace_id, parent_id, flags = parts
    if version != "00":
        return None
    if not _is_hex(trace_id, length=32) or not _is_hex(parent_id, length=16) or not _is_hex(flags, length=2):
        return None
    # The all-zero ids are invalid per the W3C spec.
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    tracestate = meta.get("tracestate", "")
    baggage = meta.get("baggage", "")
    return TraceContext(
        trace_id=trace_id,
        parent_id=parent_id,
        trace_flags=flags,
        tracestate=str(tracestate) if isinstance(tracestate, str) else "",
        baggage=str(baggage) if isinstance(baggage, str) else "",
    )


def record_trace_context_into_lineage(
    *,
    trace: TraceContext,
    artifact_path: str,
    content: bytes,
    actor: str,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    model: str = "",
    timestamp: int | None = None,
) -> str | None:
    """Record a run artefact with the calling host's trace in its lineage.

    The ingested ``traceparent`` is carried as the lineage entry's
    ``step_id`` cross-link, so a trace from the calling host connects to the
    artefacts the run produced (AC3): a verifier holding the lineage spine
    reads the host trace off the artefact's provenance row.

    Fail-closed with the lineage gate: a no-op returning ``None`` when
    ``BERNSTEIN_LINEAGE_ENABLED`` is disabled.

    Args:
        trace: The ingested W3C trace context.
        artifact_path: Repo-relative POSIX path of the artefact written.
        content: The bytes that landed on disk.
        actor: Producing agent / adapter identifier.
        run_id: The run whose lineage spine records the entry.
        lineage_root: ``.sdd/lineage`` root; per-run dirs live beneath it.
        hmac_key: Audit-chain HMAC key used to tag the entry.
        model: Optional model string recorded for provenance.
        timestamp: Optional explicit timestamp for deterministic callers.

    Returns:
        The lineage entry hash, or ``None`` when the gate is disabled.
    """
    # Local import keeps this module cheap to import and avoids a hard
    # dependency on the adapters layer at module load.
    from bernstein.adapters.base import record_artifact_write

    return record_artifact_write(
        artifact_path=artifact_path,
        content=content,
        actor=actor,
        step_id=f"mcp-trace:{trace.traceparent}",
        model=model,
        lineage_root=lineage_root,
        run_id=run_id,
        hmac_key=hmac_key,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Stateless polling fallback
# ---------------------------------------------------------------------------


def poll_task_handle(
    poll_token: str,
    *,
    events: Iterable[Mapping[str, Any]],
    chain_head: str,
    trace_id: str = "",
) -> RunHandle:
    """Reproject a run handle from a poll token and the current run state.

    This is the stateless polling fallback (AC1, AC4): a client that holds no
    session - and a server instance that never saw the original call - answer
    a poll by decoding the run identity from ``poll_token`` and reprojecting
    the handle from the on-disk journal ``events`` and the current
    ``chain_head``. Two instances reprojecting the same run agree byte for
    byte because the projection is pure.

    Args:
        poll_token: The opaque token from a prior handle's wire body.
        events: The current ordered journal rows for the run.
        chain_head: The current audit-chain head to embed.
        trace_id: Optional ingested W3C trace id to carry through.

    Returns:
        A freshly projected :class:`RunHandle` for the run.

    Raises:
        ValueError: When ``poll_token`` is malformed or missing ``run_id``.
    """
    decoded = decode_request_state(poll_token)
    run_id = decoded.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        msg = "poll token missing run_id"
        raise ValueError(msg)
    task_id = decoded.get("task_id")
    return RunHandle.from_journal(
        task_id=str(task_id) if isinstance(task_id, str) else run_id,
        run_id=run_id,
        events=events,
        chain_head=chain_head,
        trace_id=trace_id,
    )
