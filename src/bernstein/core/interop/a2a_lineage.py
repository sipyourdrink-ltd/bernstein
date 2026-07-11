"""Lineage chain interop: carry a signed Bernstein chain through A2A.

When a Bernstein run delegates work to a peer agent over A2A, the run's
signed lineage chain travels inside the A2A evidence envelope under the
``bernstein.lineage_v2`` field. The receiving side appends the delegated
work to its *own* chain with a cross-org boundary marker so an auditor can
see exactly where one organisation's chain hands off to another.

This module reuses the existing HMAC chain in
:mod:`bernstein.core.lineage.tracker_audit`: the envelope payload is the
canonical bytes of the source chain's tracker-audit entries plus a chain
digest, and the receiving side records the handoff as a normal signed entry
(``action="comment"``) whose body is the cross-org boundary marker. No new
signing primitive is introduced -- the boundary entry is verifiable by the
receiver's existing ``bernstein lineage tracker-audit verify``.

Envelope shape (the value of ``bernstein.lineage_v2``)::

    {
      "schema_version": 1,
      "source_issuer": "<issuer id from the sender's capability card>",
      "chain_digest": "sha256:...",        # over the canonical entry bytes
      "entries": [ <tracker-audit entry dict>, ... ]
    }

The ``chain_digest`` lets the receiver bind the boundary entry it appends to
the exact source chain it received: the digest is recorded in the boundary
marker, so tampering with the carried chain after the fact is detectable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.lineage.tracker_audit import (
    TrackerActor,
    entry_from_payload,
    entry_to_body,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from bernstein.core.interop.a2a_card import SignedCapabilityCard
    from bernstein.core.interop.a2a_consume import PolicyRequirements, PolicyVerdict
    from bernstein.core.lineage.tracker_audit import (
        TrackerAuditEntry,
        TrackerAuditLog,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "A2A_MESSAGE_RECEIPT_RUN_ID",
    "A2A_MESSAGE_RECEIPT_SCHEMA_VERSION",
    "A2A_TASK_STATES",
    "CROSS_ORG_BOUNDARY_MARKER",
    "JOURNAL_TERMINAL_STATES",
    "LINEAGE_ENVELOPE_FIELD",
    "LINEAGE_ENVELOPE_SCHEMA_VERSION",
    "A2AMessageReceipt",
    "A2AThreadVerifyResult",
    "InboundTaskIsolation",
    "InboundTaskRejected",
    "LineageEnvelope",
    "TaskStateMapping",
    "accept_inbound_task",
    "append_cross_org_segment",
    "chain_digest",
    "compute_message_hash",
    "isolate_inbound_task",
    "map_task_state",
    "message_receipt_path",
    "read_message_receipt",
    "record_a2a_message",
    "verify_thread",
    "wrap_lineage_chain",
]

#: A2A envelope field that carries the signed Bernstein lineage chain.
LINEAGE_ENVELOPE_FIELD: str = "bernstein.lineage_v2"

#: Marker recorded in the boundary entry's tracker name so the cross-org
#: handoff is greppable in an audit export.
CROSS_ORG_BOUNDARY_MARKER: str = "a2a-cross-org-boundary"

#: Envelope schema version. Bumping requires a parallel reader.
LINEAGE_ENVELOPE_SCHEMA_VERSION: int = 1


def _canonical(payload: dict[str, Any]) -> bytes:
    """Return stable JCS-style bytes for ``payload``."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def chain_digest(entries: Sequence[TrackerAuditEntry]) -> str:
    """Return a ``sha256:`` digest over the canonical bytes of ``entries``.

    The digest is computed over the newline-joined JCS canonicalisation of
    each entry body, in order, so it binds both the entry contents and their
    sequence. Two chains with the same entries in a different order produce
    different digests.
    """
    hasher = hashlib.sha256()
    for entry in entries:
        hasher.update(_canonical(entry_to_body(entry)))
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()


@dataclass(frozen=True)
class LineageEnvelope:
    """The ``bernstein.lineage_v2`` payload carried in an A2A envelope.

    Attributes:
        source_issuer: Issuer id from the sender's capability card.
        chain_digest: ``sha256:`` digest over the carried entries.
        entries: The source chain's tracker-audit entries.
        schema_version: Envelope schema version.
    """

    source_issuer: str
    chain_digest: str
    entries: list[TrackerAuditEntry] = field(default_factory=list)
    schema_version: int = LINEAGE_ENVELOPE_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-compatible envelope payload."""
        return {
            "schema_version": self.schema_version,
            "source_issuer": self.source_issuer,
            "chain_digest": self.chain_digest,
            "entries": [entry_to_body(entry) for entry in self.entries],
        }

    def to_envelope_field(self) -> dict[str, dict[str, Any]]:
        """Return ``{LINEAGE_ENVELOPE_FIELD: payload}`` for splicing in."""
        return {LINEAGE_ENVELOPE_FIELD: self.to_payload()}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LineageEnvelope:
        """Rebuild an envelope from a parsed payload.

        Raises:
            ValueError: If required keys are missing or malformed.
        """
        if not isinstance(payload, dict):
            raise ValueError("lineage envelope payload must be an object")
        source_issuer = payload.get("source_issuer")
        digest = payload.get("chain_digest")
        raw_entries = payload.get("entries")
        if not isinstance(source_issuer, str) or not source_issuer:
            raise ValueError("lineage envelope missing 'source_issuer'")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("lineage envelope missing valid 'chain_digest'")
        if not isinstance(raw_entries, list):
            raise ValueError("lineage envelope 'entries' must be a list")
        entries = [entry_from_payload(item) for item in raw_entries]
        recomputed = chain_digest(entries)
        if recomputed != digest:
            raise ValueError(f"lineage envelope chain_digest mismatch (carried {digest}, recomputed {recomputed})")
        return cls(
            source_issuer=source_issuer,
            chain_digest=digest,
            entries=entries,
            schema_version=int(payload.get("schema_version", LINEAGE_ENVELOPE_SCHEMA_VERSION)),
        )

    @classmethod
    def from_envelope_field(cls, envelope: dict[str, Any]) -> LineageEnvelope:
        """Extract and rebuild the envelope from a full A2A envelope dict.

        Raises:
            ValueError: If the ``bernstein.lineage_v2`` field is absent.
        """
        payload = envelope.get(LINEAGE_ENVELOPE_FIELD)
        if not isinstance(payload, dict):
            raise ValueError(f"A2A envelope missing '{LINEAGE_ENVELOPE_FIELD}' field")
        return cls.from_payload(payload)


def wrap_lineage_chain(
    log: TrackerAuditLog,
    *,
    source_issuer: str,
    tracker_name: str | None = None,
    ticket_id: str | None = None,
) -> LineageEnvelope:
    """Read a signed chain from ``log`` and wrap it in an A2A envelope.

    Args:
        log: The source :class:`TrackerAuditLog` to read entries from.
        source_issuer: Issuer id from the sender's capability card; recorded
            so the receiver can attribute the carried chain.
        tracker_name: Optional filter to wrap only one tracker's entries.
        ticket_id: Optional filter to wrap only one ticket's entries.

    Returns:
        A :class:`LineageEnvelope` ready to splice into an A2A payload via
        :meth:`LineageEnvelope.to_envelope_field`.
    """
    if tracker_name is not None or ticket_id is not None:
        entries = log.filter(tracker_name=tracker_name, ticket_id=ticket_id)
    else:
        entries = log.read()
    return LineageEnvelope(
        source_issuer=source_issuer,
        chain_digest=chain_digest(entries),
        entries=list(entries),
    )


def append_cross_org_segment(
    receiver_log: TrackerAuditLog,
    envelope: LineageEnvelope,
    *,
    actor: TrackerActor,
    ticket_id: str,
    lifecycle_event_id: str | None = None,
) -> TrackerAuditEntry:
    """Append a cross-org boundary marker to the receiver's own chain.

    The receiving side records the handoff as a normal signed tracker-audit
    entry whose body captures the source issuer and the carried chain's
    digest. The entry's ``tracker_name`` is :data:`CROSS_ORG_BOUNDARY_MARKER`
    so the boundary is greppable, and its ``output_blob`` is the canonical
    envelope payload so the receiver's chain binds the exact bytes received.
    The entry is signed and chained by the receiver's existing
    :class:`TrackerAuditLog`, so it verifies under the receiver's operator
    HMAC key with no new primitive.

    Args:
        receiver_log: The receiver's :class:`TrackerAuditLog`.
        envelope: The lineage envelope extracted from the A2A payload.
        actor: The receiving actor (session/role/model) recording the entry.
        ticket_id: The receiver-side ticket the delegated work attaches to.
        lifecycle_event_id: Optional lifecycle correlation id.

    Returns:
        The appended, signed :class:`TrackerAuditEntry` boundary marker.
    """
    marker_body = _canonical(
        {
            "marker": CROSS_ORG_BOUNDARY_MARKER,
            "source_issuer": envelope.source_issuer,
            "source_chain_digest": envelope.chain_digest,
            "source_entry_count": len(envelope.entries),
        }
    )

    payload_bytes = _canonical(envelope.to_payload())

    result = receiver_log.append(
        tracker_name=CROSS_ORG_BOUNDARY_MARKER,
        ticket_id=ticket_id,
        action="comment",
        actor=actor,
        input_prompt=marker_body,
        output_blob=payload_bytes,
        lifecycle_event_id=lifecycle_event_id,
    )
    return result.entry


# ===========================================================================
# Signed A2A message receipts (#2304)
# ---------------------------------------------------------------------------
# Bernstein already signs A2A agent and capability cards, but the cross-agent
# task messages themselves are not attestable, so a reviewer cannot prove a
# given cross-agent call happened with the exact inputs claimed. Here every
# inbound/outbound A2A message becomes a signed lineage receipt binding
# ``{message_hash, peer_card_fingerprint, task_uuid, journal_entry_hash}`` and
# anchored to a dedicated message-receipt spine. The A2A v1.0 task lifecycle
# maps 1:1 to journal terminal states with reason codes, keyed by ``task_uuid``
# as the trace root, and ``verify_thread`` proves the visible cross-agent
# thread equals the executed actions offline.
#
# The receipt is the artefact: strip the spine, the Ed25519 install-identity
# signature, and the seeded per-task journal and the receipts are just files;
# anchored and signed they are chain-verifiable attestations that a cross-agent
# collaboration ran exactly as recorded, without trusting either agent's logs.
# ===========================================================================


#: Run id under which every A2A message receipt is anchored. One dedicated
#: spine keeps message receipts from interleaving with per-task run journals.
A2A_MESSAGE_RECEIPT_RUN_ID: str = "a2a-messages"

#: Version stamped into every message-receipt binding preimage.
A2A_MESSAGE_RECEIPT_SCHEMA_VERSION: int = 1

_MESSAGE_SUBPATH = (".sdd", "a2a-messages")

_IDENTITY_PRIVATE_NAME = "a2a-message-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "a2a-message-identity-public.pem"

#: Actor recorded on the message-receipt spine entries.
_MESSAGE_ACTOR = "bernstein.a2a_message"

#: Model string recorded on spine entries (no model runs at anchor time).
_MESSAGE_MODEL = "none"

#: Event type recorded on the seeded per-task journal for each message.
_MESSAGE_JOURNAL_EVENT = "a2a.message"


# ---------------------------------------------------------------------------
# A2A task lifecycle -> journal terminal state mapping (AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStateMapping:
    """One A2A task state mapped to a journal state with a reason code.

    Attributes:
        a2a_state: The A2A v1.0 task state name.
        journal_state: The journal event name the state maps to.
        reason_code: The default reason code recorded when no explicit reason
            is supplied by the caller.
        terminal: ``True`` when the journal state is terminal (the trace root
            for the task closes here).
    """

    a2a_state: str
    journal_state: str
    reason_code: str
    terminal: bool


#: Journal states considered terminal for a task's trace root.
JOURNAL_TERMINAL_STATES: frozenset[str] = frozenset({"task_completed", "task_failed", "task_canceled"})

#: The A2A v1.0 task lifecycle states, mapped 1:1 to journal states with reason
#: codes. ``submitted`` / ``working`` / ``input-required`` are non-terminal;
#: ``completed`` / ``failed`` / ``canceled`` are terminal. The mapping is total
#: and injective so a verifier can reconstruct the exact lifecycle a task
#: traversed from its journal states alone.
_TASK_STATE_MAP: dict[str, TaskStateMapping] = {
    "submitted": TaskStateMapping("submitted", "task_submitted", "received", terminal=False),
    "working": TaskStateMapping("working", "task_working", "in_progress", terminal=False),
    "input-required": TaskStateMapping("input-required", "task_input_required", "awaiting_input", terminal=False),
    "completed": TaskStateMapping("completed", "task_completed", "success", terminal=True),
    "failed": TaskStateMapping("failed", "task_failed", "error", terminal=True),
    "canceled": TaskStateMapping("canceled", "task_canceled", "canceled", terminal=True),
}

#: The A2A v1.0 task states, in lifecycle order.
A2A_TASK_STATES: tuple[str, ...] = tuple(_TASK_STATE_MAP)


def map_task_state(state: str) -> TaskStateMapping:
    """Return the :class:`TaskStateMapping` for an A2A task ``state`` (AC5).

    Raises:
        ValueError: When ``state`` is not one of the A2A v1.0 task states.
    """
    mapping = _TASK_STATE_MAP.get(state)
    if mapping is None:
        raise ValueError(f"unknown A2A task state: {state!r} (expected one of {A2A_TASK_STATES})")
    return mapping


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def compute_message_hash(
    *,
    task_uuid: str,
    direction: str,
    state: str,
    seq: int,
    body: bytes,
) -> str:
    """Return the content hash of an A2A message.

    Binds the task uuid, direction, lifecycle state, sequence index, and the
    raw message body so a verifier presented the same message recomputes the
    same hash. A single-byte edit to any field diverges the hash.
    """
    preimage = _canonical_bytes(
        {
            "v": A2A_MESSAGE_RECEIPT_SCHEMA_VERSION,
            "task_uuid": task_uuid,
            "direction": direction,
            "state": state,
            "seq": seq,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
    )
    return _sha256_bytes(preimage)


def _safe_component(value: str) -> str:
    """Return a filesystem-safe basename for a task uuid.

    The value is content-hashed so the name is portable and cannot introduce a
    path separator regardless of the uuid's shape.
    """
    if not value:
        raise ValueError("empty task_uuid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Install identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def _load_or_create_message_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 message identity.

    The keypair is persisted under ``identity_dir`` so the same install signs
    every message receipt and a verifier can check the signature offline
    against the embedded public key. The private key file is ``0600``.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    from bernstein.core.lineage.identity import generate_keypair

    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        # Read the raw signing key verbatim -- never strip, so a key whose PEM
        # legitimately ends without a trailing newline still round-trips.
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# The signed, spine-anchored message receipt (AC1) -- the primary artefact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AMessageReceipt:
    """Signed receipt for one inbound/outbound A2A message.

    The receipt binds ``{message_hash, peer_card_fingerprint, task_uuid,
    journal_entry_hash}``: a peer holding the receipt and the spine can prove
    the message happened with the exact inputs claimed, without trusting either
    agent's logs.

    Attributes:
        task_uuid: The A2A task the message belongs to (the trace root).
        direction: ``inbound`` (from a peer) or ``outbound`` (to a peer).
        state: The A2A v1.0 task state carried by the message.
        journal_state: The journal state the A2A state maps to.
        reason_code: The reason code recorded on the journal terminal state.
        seq: The message index within the task thread (0-based).
        message_hash: Content hash of the message (task + direction + body).
        peer_card_fingerprint: ``sha256:`` fingerprint of the peer's signing
            key -- ties the message to the peer's verified capability card.
        run_id: The per-task journal run id (trace root keyed by task_uuid).
        journal_root: The seeded per-task journal head that references the
            message hash.
        journal_events: The message hashes recorded on the seeded journal.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The message-receipt spine entry hash anchoring the
            receipt.
    """

    task_uuid: str
    direction: str
    state: str
    journal_state: str
    reason_code: str
    seq: int
    message_hash: str
    peer_card_fingerprint: str
    run_id: str
    journal_root: str
    journal_events: tuple[str, ...] = ()
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": A2A_MESSAGE_RECEIPT_SCHEMA_VERSION,
            "task_uuid": self.task_uuid,
            "direction": self.direction,
            "state": self.state,
            "journal_state": self.journal_state,
            "reason_code": self.reason_code,
            "seq": self.seq,
            "message_hash": self.message_hash,
            "peer_card_fingerprint": self.peer_card_fingerprint,
            "run_id": self.run_id,
            "journal_root": self.journal_root,
            "journal_events": list(self.journal_events),
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + anchored)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> A2AMessageReceipt:
        row = json.loads(raw)
        return cls(
            task_uuid=str(row["task_uuid"]),
            direction=str(row["direction"]),
            state=str(row["state"]),
            journal_state=str(row["journal_state"]),
            reason_code=str(row["reason_code"]),
            seq=int(row["seq"]),
            message_hash=str(row["message_hash"]),
            peer_card_fingerprint=str(row["peer_card_fingerprint"]),
            run_id=str(row["run_id"]),
            journal_root=str(row["journal_root"]),
            journal_events=tuple(str(e) for e in row.get("journal_events", [])),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def _message_run_id(task_uuid: str) -> str:
    """Return the per-task journal run id (trace root keyed by task_uuid)."""
    return f"a2a-{_safe_component(task_uuid)}-{_slug(task_uuid)}"


def _slug(value: str) -> str:
    """Return a short readable slug of ``value`` safe for a run-id suffix."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in value)
    return safe[:24].strip("-") or "task"


def message_receipt_path(workdir: Path, *, task_uuid: str, seq: int) -> Path:
    """Return the on-disk receipt path for message ``seq`` of ``task_uuid``."""
    return workdir.joinpath(*_MESSAGE_SUBPATH, _slug(task_uuid), f"{seq:04d}.json")


def read_message_receipt(workdir: Path, *, task_uuid: str, seq: int) -> A2AMessageReceipt | None:
    """Return the receipt for message ``seq`` of ``task_uuid`` or ``None``."""
    path = message_receipt_path(workdir, task_uuid=task_uuid, seq=seq)
    if not path.is_file():
        return None
    try:
        return A2AMessageReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("a2a_message: malformed receipt at %s", sanitize_log(str(path)))
        return None


def record_a2a_message(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    identity_dir: Path,
    task_uuid: str,
    direction: str,
    state: str,
    peer_card_fingerprint: str,
    body: bytes,
    seq: int,
    timestamp: int,
    reason: str = "",
) -> A2AMessageReceipt:
    """Write a signed, journal-anchored receipt for one A2A message (AC1, AC5).

    The A2A ``state`` is mapped 1:1 to a journal state with a reason code
    (:func:`map_task_state`), and the message is recorded on the per-task
    journal keyed by ``task_uuid`` so the task uuid is the trace root. A signed
    receipt binding ``{message_hash, peer_card_fingerprint, task_uuid,
    journal_entry_hash}`` is anchored in the message-receipt spine.

    Args:
        workdir: Project root; receipts land under ``.sdd/a2a-messages/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine + journal entries.
        identity_dir: Directory holding the install's Ed25519 identity.
        task_uuid: The A2A task uuid (the trace root).
        direction: ``inbound`` or ``outbound``.
        state: The A2A v1.0 task state (:data:`A2A_TASK_STATES`).
        peer_card_fingerprint: ``sha256:`` fingerprint of the peer's card key.
        body: The raw message body bytes (hashed, never stored on the receipt).
        seq: The 0-based message index within the task thread.
        timestamp: Integer timestamp for the receipt / journal.
        reason: Optional explicit reason code; the state's default reason is
            used when empty.

    Returns:
        The signed, anchored :class:`A2AMessageReceipt`.

    Raises:
        ValueError: When ``state`` is not an A2A v1.0 task state.
    """
    mapping = map_task_state(state)
    reason_code = reason or mapping.reason_code
    message_hash = compute_message_hash(task_uuid=task_uuid, direction=direction, state=state, seq=seq, body=body)
    run_id = _message_run_id(task_uuid)

    # Seed the per-task journal with the message so the task's journal root
    # references the message hash (AC1). The journal is keyed by task_uuid, so
    # the uuid is the trace root; the terminal A2A states map to terminal
    # journal states with reason codes (AC5).
    journal = EventJournal(run_id, workdir / ".sdd")
    journal.record(
        _MESSAGE_JOURNAL_EVENT,
        message_hash=message_hash,
        direction=direction,
        a2a_state=state,
        journal_state=mapping.journal_state,
        reason_code=reason_code,
        terminal=mapping.terminal,
        seq=seq,
    )
    journal_root = journal.head()
    journal_events = (message_hash,)

    private_pem, public_pem = _load_or_create_message_identity(identity_dir)
    payload = A2AMessageReceipt(
        task_uuid=task_uuid,
        direction=direction,
        state=state,
        journal_state=mapping.journal_state,
        reason_code=reason_code,
        seq=seq,
        message_hash=message_hash,
        peer_card_fingerprint=peer_card_fingerprint,
        run_id=run_id,
        journal_root=journal_root,
        journal_events=journal_events,
        timestamp=timestamp,
    ).to_canonical_bytes()
    signature = sign_payload(payload, private_pem)

    spine = LineageSpine(lineage_root, run_id=A2A_MESSAGE_RECEIPT_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_MESSAGE_SUBPATH, _slug(task_uuid), f"{seq:04d}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_MESSAGE_ACTOR,
        step_id=message_hash,
        model=_MESSAGE_MODEL,
        timestamp=timestamp,
    )
    anchored = A2AMessageReceipt(
        task_uuid=task_uuid,
        direction=direction,
        state=state,
        journal_state=mapping.journal_state,
        reason_code=reason_code,
        seq=seq,
        message_hash=message_hash,
        peer_card_fingerprint=peer_card_fingerprint,
        run_id=run_id,
        journal_root=journal_root,
        journal_events=journal_events,
        timestamp=timestamp,
        signer_public_key_pem=public_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    _write_message_receipt(message_receipt_path(workdir, task_uuid=task_uuid, seq=seq), anchored.to_dict())
    return anchored


# ---------------------------------------------------------------------------
# Thread verification (AC2): the thread equals the executed actions offline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2AThreadVerifyResult:
    """Outcome of :func:`verify_thread`.

    Attributes:
        ok: ``True`` only when every message in the thread verifies against the
            spine and the seeded journal, offline.
        reason: Human-readable failure reason; empty when ``ok``.
        message_count: The number of messages checked in the thread.
        task_uuid: The task uuid the thread belongs to.
    """

    ok: bool
    reason: str
    message_count: int = 0
    task_uuid: str = ""


def _iter_thread_receipts(workdir: Path, task_uuid: str) -> list[A2AMessageReceipt]:
    """Return the receipts for a task thread in ``seq`` order."""
    thread_dir = workdir.joinpath(*_MESSAGE_SUBPATH, _slug(task_uuid))
    if not thread_dir.is_dir():
        return []
    receipts: list[A2AMessageReceipt] = []
    for path in sorted(thread_dir.glob("*.json")):
        try:
            receipts.append(A2AMessageReceipt.from_bytes(path.read_bytes()))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("a2a_message: malformed receipt at %s", sanitize_log(str(path)))
            continue
    receipts.sort(key=lambda r: r.seq)
    return receipts


def _anchor_for(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def verify_thread(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    task_uuid: str,
) -> A2AThreadVerifyResult:
    """Prove a cross-agent thread equals the executed actions offline (AC2).

    For every message receipt in the ``task_uuid`` thread, this recomputes the
    binding, re-checks the Ed25519 signature offline, verifies the
    message-receipt spine, re-anchors the receipt against it, and confirms the
    message hash is referenced by the seeded per-task journal. A single-byte
    edit to any receipt, the spine, or a journal fails the check.

    Returns:
        An :class:`A2AThreadVerifyResult`. An empty thread is *not* ``ok``.
    """
    receipts = _iter_thread_receipts(workdir, task_uuid)
    if not receipts:
        return A2AThreadVerifyResult(
            ok=False,
            reason=f"no message receipts found for task {task_uuid!r}",
            task_uuid=task_uuid,
        )

    spine = LineageSpine(lineage_root, run_id=A2A_MESSAGE_RECEIPT_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return A2AThreadVerifyResult(
            ok=False,
            reason=f"message-receipt spine failed verification ({spine_result.status.value})",
            message_count=len(receipts),
            task_uuid=task_uuid,
        )

    for receipt in receipts:
        # The message body is deliberately not stored on the receipt (only its
        # hash), so verification binds the receipt's recorded message hash to
        # the seeded journal root and to the signed spine anchor rather than
        # recomputing the hash from the body. A verifier that also holds the
        # original body can independently recompute compute_message_hash and
        # compare it to receipt.message_hash.
        if receipt.message_hash not in receipt.journal_events:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: hash not referenced by the seeded journal root",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        try:
            expected_state = map_task_state(receipt.state)
        except ValueError:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: unknown A2A state {receipt.state!r}",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        if receipt.journal_state != expected_state.journal_state:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: state mapping mismatch",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        if not receipt.signature or not receipt.signer_public_key_pem:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: receipt is unsigned",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        outcome = verify_payload(
            receipt.to_canonical_bytes(),
            receipt.signature,
            receipt.signer_public_key_pem,
            allow_unverified=True,
        )
        if not outcome.verified:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: signature does not verify ({outcome.reason})",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        anchor = _anchor_for(spine, receipt.to_canonical_bytes())
        if anchor is None:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: receipt is not anchored in the message-receipt spine",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )
        if anchor != receipt.journal_entry_hash:
            return A2AThreadVerifyResult(
                ok=False,
                reason=f"message {receipt.seq}: anchor does not match the spine entry over the receipt bytes",
                message_count=len(receipts),
                task_uuid=task_uuid,
            )

    return A2AThreadVerifyResult(ok=True, reason="", message_count=len(receipts), task_uuid=task_uuid)


# ---------------------------------------------------------------------------
# Inbound task trust check (AC3) + worktree isolation (AC4)
# ---------------------------------------------------------------------------


class InboundTaskRejected(RuntimeError):
    """Raised when an inbound peer task cannot be trusted.

    Attributes:
        reason: Machine-readable reason code (``signature``,
            ``untrusted_issuer``, ``issuer_mismatch``, ``policy``).
        detail: Human-readable explanation.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"inbound A2A task rejected ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


def accept_inbound_task(
    *,
    signed_card: SignedCapabilityCard,
    trusted_issuer_fingerprints: Iterable[str],
    requirements: PolicyRequirements,
    expected_issuer: str,
    now: float | None = None,
) -> PolicyVerdict:
    """Verify an inbound peer card and gate the task on trust + policy (AC3).

    The inbound card is verified against its declared issuer domain and
    cross-checked against the operator's trusted-issuer set before any peer
    task is accepted: the signature must verify and be unexpired, the card's
    ``issuer`` must equal ``expected_issuer`` (the domain the message arrived
    from), the card key fingerprint must be in the trusted set, and the card's
    advertised policies must meet the operator's requirements.

    Args:
        signed_card: The peer's signed capability card.
        trusted_issuer_fingerprints: ``sha256:`` fingerprints the operator
            trusts.
        requirements: The operator's required policies.
        expected_issuer: The issuer id the inbound message claims to come from;
            a card whose ``issuer`` differs is rejected (domain cross-check).
        now: Optional timestamp override for expiry checks (testing).

    Returns:
        The :class:`PolicyVerdict` from the policy gate when every check passes.

    Raises:
        InboundTaskRejected: When any check fails; the ``reason`` distinguishes
            the cases.
    """
    from bernstein.core.interop.a2a_consume import PeerCardRejected, consume_peer_card

    if signed_card.card.issuer != expected_issuer:
        raise InboundTaskRejected(
            "issuer_mismatch",
            f"card issuer {signed_card.card.issuer!r} does not match the message origin {expected_issuer!r}",
        )
    try:
        return consume_peer_card(
            signed_card,
            trusted_issuer_fingerprints=trusted_issuer_fingerprints,
            requirements=requirements,
            now=now,
        )
    except PeerCardRejected as exc:
        raise InboundTaskRejected(exc.reason, exc.detail) from exc


@dataclass(frozen=True)
class InboundTaskIsolation:
    """Outcome of :func:`isolate_inbound_task`.

    Attributes:
        task_uuid: The A2A task uuid the worktree isolates.
        session_id: The worktree session id (derived from the task uuid).
        worktree_path: The path to the created worktree.
        isolation_ok: ``True`` when the worktree passes isolation validation.
        violations: Any isolation violations detected.
    """

    task_uuid: str
    session_id: str
    worktree_path: Path
    isolation_ok: bool
    violations: list[str] = field(default_factory=list[str])


def inbound_task_session_id(task_uuid: str) -> str:
    """Return the deterministic worktree session id for an inbound task."""
    return f"a2a-{_slug(task_uuid)}-{_safe_component(task_uuid)[:12]}"


def isolate_inbound_task(*, repo_root: Path, task_uuid: str) -> InboundTaskIsolation:
    """Create a dedicated, isolated worktree for one inbound peer task (AC4).

    Each inbound peer task runs in its own git worktree (f03 primitive) keyed by
    a session id derived from ``task_uuid`` so no two collaborations share
    mutable state. The worktree's isolation is validated (its ``.sdd`` is a real
    per-worktree directory, not a symlink into the parent repo), so a peer
    cannot clobber another peer's state.

    Args:
        repo_root: The repository root the worktree branches from.
        task_uuid: The A2A task uuid the worktree isolates.

    Returns:
        An :class:`InboundTaskIsolation` describing the created worktree.
    """
    from bernstein.core.git.worktree import WorktreeManager
    from bernstein.core.git.worktree_isolation import validate_worktree_isolation

    session_id = inbound_task_session_id(task_uuid)
    worktree_path = WorktreeManager(repo_root=repo_root).create(session_id)
    result = validate_worktree_isolation(worktree_path, repo_root)
    return InboundTaskIsolation(
        task_uuid=task_uuid,
        session_id=session_id,
        worktree_path=worktree_path,
        isolation_ok=result.passed,
        violations=list(result.violations),
    )


def _write_message_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
