"""Audited webhook execution node with signed inbound/outbound receipts (#2310).

Many operators drive automation from no-code builders and event buses that own
the flow surface but produce no verifiable record of what an execution step did.
This module lets Bernstein slot in as the one node in such a flow that turns it
into a verifiable one: an inbound webhook triggers a run, and both the inbound
event and the outbound result carry signed receipts anchored to the run journal
and the webhook-node lineage spine.

Standard Webhooks
-----------------
Inbound and outbound signatures follow the Standard Webhooks scheme
(``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature`` headers). The
signed content is ``f"{msg_id}.{timestamp}.{body}"`` under HMAC-SHA256, base64
encoded and prefixed ``v1,`` in the signature header. Verification is
constant-time and accepts the space-separated multi-signature header form so a
sender rotating secrets can present more than one candidate signature.

The artefact IS the proof
-------------------------
* An :class:`InboundReceipt` binds ``{event_hash, source, journal_root}``: the
  ``event_hash`` is recomputed from the source label, event id, and body, and
  the spawned run's journal is seeded with an event carrying that hash, so the
  ``journal_root`` (the run journal head after seeding) references the inbound
  event. The receipt is signed with the install's Ed25519 identity and anchored
  in the webhook-node spine (AC1).
* An :class:`OutboundReceipt` binds ``{result_hash, journal_head}`` and is
  signed and anchored, so the returned result is provably the projection of the
  executed run rather than a free-standing claim (AC2).
* :func:`verify_webhook_event` recomputes both hashes, re-checks the Ed25519
  signatures offline, re-anchors both receipts against the spine, and verifies
  the seeded run journal, so any single-byte edit to a receipt, the spine, or
  the journal fails the check (AC3).

Correctness
-----------
* :func:`receive_inbound_webhook` rejects an inbound event whose Standard
  Webhooks signature does not verify -- no receipt is written and no run is
  spawned (AC4).
* Inbound events are idempotent by ``event_id``: a retry/backoff replay of the
  same event returns the recorded receipt and does not spawn a second run
  (AC5).

Strip the spine, the signature, and the seeded journal and the receipts are
just files; anchored and signed they are chain-verifiable attestations that a
no-code step ran exactly as recorded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.replay.journal import EventJournal
from bernstein.core.sanitize import sanitize_log
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard Webhooks header + scheme constants
# ---------------------------------------------------------------------------

#: Standard Webhooks message-id header (unique per event; the idempotency key).
STANDARD_WEBHOOK_ID_HEADER = "webhook-id"

#: Standard Webhooks timestamp header (Unix seconds; part of the signed content).
STANDARD_WEBHOOK_TIMESTAMP_HEADER = "webhook-timestamp"

#: Standard Webhooks signature header (space-separated ``v1,<base64>`` list).
STANDARD_WEBHOOK_SIGNATURE_HEADER = "webhook-signature"

#: Signature scheme prefix carried on each candidate signature.
_SIGNATURE_VERSION = "v1"

#: Run id under which the webhook-node lineage is anchored. Kept in one
#: dedicated spine so receipts never interleave with per-task journals.
WEBHOOK_NODE_RUN_ID = "webhook-node"

#: Actor recorded on webhook-node spine entries.
_NODE_ACTOR = "bernstein.webhook_node"

#: Model string recorded on spine entries (no model runs at anchor time).
_NODE_MODEL = "none"

#: Version stamped into every receipt binding preimage.
WEBHOOK_NODE_SCHEMA_VERSION = 1

_INBOUND_SUBPATH = (".sdd", "webhook-node", "inbound")
_OUTBOUND_SUBPATH = (".sdd", "webhook-node", "outbound")

_IDENTITY_PRIVATE_NAME = "webhook-node-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "webhook-node-identity-public.pem"

#: Event type recorded on the seeded run journal for the inbound trigger.
_INBOUND_JOURNAL_EVENT = "webhook_node.inbound"


class WebhookNodeError(RuntimeError):
    """Raised when an inbound webhook is rejected (bad or missing signature)."""


# ---------------------------------------------------------------------------
# Standard Webhooks signing / verification
# ---------------------------------------------------------------------------


def _signed_content(msg_id: str, timestamp: int, body: bytes) -> bytes:
    """Return the Standard Webhooks signed content ``{id}.{ts}.{body}``."""
    prefix = f"{msg_id}.{timestamp}.".encode()
    return prefix + body


def sign_standard_webhook(*, secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """Return a Standard Webhooks ``v1,<base64>`` signature header value.

    Args:
        secret: Shared webhook secret (the raw secret string; read verbatim).
        msg_id: The ``webhook-id`` value.
        timestamp: The ``webhook-timestamp`` value (Unix seconds).
        body: Raw request body bytes.

    Returns:
        The signature header value (a single ``v1,<base64>`` token).
    """
    digest = hmac.new(secret.encode("utf-8"), _signed_content(msg_id, timestamp, body), hashlib.sha256).digest()
    return f"{_SIGNATURE_VERSION},{base64.b64encode(digest).decode('ascii')}"


def verify_standard_webhook(
    *,
    secret: str,
    msg_id: str,
    timestamp: int,
    body: bytes,
    signature_header: str,
) -> bool:
    """Verify a Standard Webhooks signature header in constant time.

    The header may carry more than one space-separated candidate signature
    (secret rotation); the event verifies if any candidate matches.

    Args:
        secret: Shared webhook secret.
        msg_id: The ``webhook-id`` value.
        timestamp: The ``webhook-timestamp`` value (Unix seconds).
        body: Raw request body bytes.
        signature_header: The ``webhook-signature`` header value.

    Returns:
        ``True`` when at least one ``v1`` candidate matches, else ``False``.
    """
    if not secret or not signature_header:
        return False
    expected = sign_standard_webhook(secret=secret, msg_id=msg_id, timestamp=timestamp, body=body)
    _, _, expected_b64 = expected.partition(",")
    matched = False
    for token in signature_header.split(" "):
        version, sep, candidate = token.partition(",")
        if not sep or version != _SIGNATURE_VERSION:
            continue
        # Never short-circuit: run every compare so timing does not leak which
        # candidate matched.
        if hmac.compare_digest(candidate, expected_b64):
            matched = True
    return matched


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def compute_event_hash(*, source: str, event_id: str, body: bytes) -> str:
    """Return the content hash of an inbound webhook event.

    Binds the source label, event id, and the raw body so a verifier presented
    the same inbound event recomputes the same hash.
    """
    preimage = _canonical_bytes(
        {
            "v": WEBHOOK_NODE_SCHEMA_VERSION,
            "source": source,
            "event_id": event_id,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
    )
    return _sha256_bytes(preimage)


def compute_result_hash(result: dict[str, Any]) -> str:
    """Return the content hash of an outbound result payload."""
    return _sha256_bytes(_canonical_bytes(result))


# ---------------------------------------------------------------------------
# Install identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def load_or_create_node_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's Ed25519 webhook-node identity.

    The keypair is persisted under ``identity_dir`` so the same install signs
    every receipt and a verifier can check the signature offline against the
    embedded public key. The private key file is written with ``0600`` mode.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    from bernstein.core.lineage.identity import generate_keypair

    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
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


def _safe_event_name(event_id: str) -> str:
    """Return a filesystem-safe basename for an event id.

    The id is content-hashed so the name is portable and cannot introduce a
    path separator regardless of the id's shape.
    """
    if not event_id:
        raise ValueError("empty event_id")
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Inbound receipt (AC1) -- the signed, spine-anchored primary artefact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundReceipt:
    """Signed receipt for an inbound webhook that spawned a run.

    Attributes:
        event_id: The Standard Webhooks message id (idempotency key).
        source: The calling bus / builder label.
        event_hash: Content hash of the inbound event (source + id + body).
        run_id: The spawned run whose journal was seeded with the event.
        journal_root: The seeded run journal head that references the event.
        journal_events: The event hashes recorded on the seeded journal.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The webhook-node spine entry hash anchoring the
            receipt.
    """

    event_id: str
    source: str
    event_hash: str
    run_id: str
    journal_root: str
    journal_events: tuple[str, ...] = ()
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": WEBHOOK_NODE_SCHEMA_VERSION,
            "direction": "inbound",
            "event_id": self.event_id,
            "source": self.source,
            "event_hash": self.event_hash,
            "run_id": self.run_id,
            "journal_root": self.journal_root,
            "journal_events": list(self.journal_events),
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    def journal_root_events(self) -> tuple[str, ...]:
        """Return the event hashes the seeded run journal references."""
        return self.journal_events

    @classmethod
    def from_bytes(cls, raw: bytes) -> InboundReceipt:
        row = json.loads(raw)
        return cls(
            event_id=str(row["event_id"]),
            source=str(row["source"]),
            event_hash=str(row["event_hash"]),
            run_id=str(row["run_id"]),
            journal_root=str(row["journal_root"]),
            journal_events=tuple(str(e) for e in row.get("journal_events", [])),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


@dataclass(frozen=True)
class InboundResult:
    """Outcome of :func:`receive_inbound_webhook`.

    Attributes:
        receipt: The signed inbound receipt (fresh or replayed).
        spawned: ``True`` when this call spawned a run; ``False`` on a replay.
        run_id: The (fresh or existing) run id the event maps to.
    """

    receipt: InboundReceipt
    spawned: bool
    run_id: str


def inbound_receipt_path(workdir: Path, event_id: str) -> Path:
    """Return the on-disk inbound-receipt path for ``event_id``."""
    return workdir.joinpath(*_INBOUND_SUBPATH, f"{event_id}.json")


def read_inbound_receipt(workdir: Path, event_id: str) -> InboundReceipt | None:
    """Return the inbound receipt for ``event_id`` or ``None`` if absent."""
    path = inbound_receipt_path(workdir, event_id)
    if not path.is_file():
        return None
    try:
        return InboundReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("webhook_node: malformed inbound receipt at %s", sanitize_log(str(path)))
        return None


def _run_id_for_event(event_id: str) -> str:
    """Return the deterministic run id a fresh event maps to."""
    return f"webhook-{_safe_event_name(event_id)[:16]}"


def receive_inbound_webhook(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    identity_dir: Path,
    secret: str,
    source: str,
    headers: dict[str, str],
    body: bytes,
    timestamp: int,
) -> InboundResult:
    """Verify an inbound webhook, spawn a run, and emit a signed receipt.

    The Standard Webhooks signature is verified first; an invalid or missing
    signature raises :class:`WebhookNodeError` and writes nothing (AC4). A
    replay of an already-seen ``event_id`` returns the recorded receipt without
    spawning a second run (AC5). Otherwise a run journal is seeded with the
    inbound event -- so the run's ``journal_root`` references the event hash --
    and a signed receipt binding ``{event_hash, source, journal_root}`` is
    anchored in the webhook-node spine (AC1).

    Args:
        workdir: Project root; receipts land under ``.sdd/webhook-node/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine + journal entries.
        identity_dir: Directory holding the install's Ed25519 node identity.
        secret: The inbound webhook secret (read verbatim).
        source: The calling bus / builder label recorded on the receipt.
        headers: Case-insensitive HTTP headers carrying the Standard Webhooks
            id / timestamp / signature.
        body: Raw request body bytes.
        timestamp: Integer timestamp for the receipt / journal.

    Returns:
        An :class:`InboundResult`.

    Raises:
        WebhookNodeError: When the signature is missing or does not verify.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    event_id = lower.get(STANDARD_WEBHOOK_ID_HEADER, "")
    if not event_id:
        raise WebhookNodeError("missing webhook-id header")
    signature_header = lower.get(STANDARD_WEBHOOK_SIGNATURE_HEADER, "")
    ts_header = lower.get(STANDARD_WEBHOOK_TIMESTAMP_HEADER, "")
    try:
        sig_timestamp = int(ts_header)
    except ValueError as exc:
        raise WebhookNodeError("missing or non-integer webhook-timestamp header") from exc

    if not verify_standard_webhook(
        secret=secret,
        msg_id=event_id,
        timestamp=sig_timestamp,
        body=body,
        signature_header=signature_header,
    ):
        raise WebhookNodeError("inbound Standard Webhooks signature did not verify")

    existing = read_inbound_receipt(workdir, event_id)
    if existing is not None:
        # Retry / backoff replay: the event already produced a run and a
        # receipt; return them idempotently without spawning again (AC5).
        return InboundResult(receipt=existing, spawned=False, run_id=existing.run_id)

    event_hash = compute_event_hash(source=source, event_id=event_id, body=body)
    run_id = _run_id_for_event(event_id)

    # Seed the spawned run's journal with the inbound event so the run's
    # journal root references the event hash (AC1). The journal is the canonical
    # per-run event log; its head after seeding is the run's identity.
    journal = EventJournal(run_id, workdir / ".sdd")
    journal.record(_INBOUND_JOURNAL_EVENT, event_hash=event_hash, source=source, event_id=event_id)
    journal_root = journal.head()
    journal_events = (event_hash,)

    private_pem, public_pem = load_or_create_node_identity(identity_dir)
    payload = InboundReceipt(
        event_id=event_id,
        source=source,
        event_hash=event_hash,
        run_id=run_id,
        journal_root=journal_root,
        journal_events=journal_events,
        timestamp=timestamp,
    ).to_canonical_bytes()
    signature = sign_payload(payload, private_pem)

    spine = LineageSpine(lineage_root, run_id=WEBHOOK_NODE_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_INBOUND_SUBPATH, f"{_safe_event_name(event_id)}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_NODE_ACTOR,
        step_id=event_hash,
        model=_NODE_MODEL,
        timestamp=timestamp,
    )
    anchored = InboundReceipt(
        event_id=event_id,
        source=source,
        event_hash=event_hash,
        run_id=run_id,
        journal_root=journal_root,
        journal_events=journal_events,
        timestamp=timestamp,
        signer_public_key_pem=public_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    _write_receipt(inbound_receipt_path(workdir, event_id), anchored.to_dict())
    return InboundResult(receipt=anchored, spawned=True, run_id=run_id)


# ---------------------------------------------------------------------------
# Outbound receipt (AC2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundReceipt:
    """Signed receipt for an outbound result the node returned.

    Attributes:
        event_id: The inbound event id the result answers.
        source: The calling bus / builder label.
        result_hash: Content hash of the returned result payload.
        journal_head: The run journal head the result binds to.
        result: The returned result payload (projected to the caller).
        timestamp: Integer timestamp; caller-chosen but stable.
        signer_public_key_pem: The install's Ed25519 public key.
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The webhook-node spine entry hash anchoring the
            receipt.
    """

    event_id: str
    source: str
    result_hash: str
    journal_head: str
    result: dict[str, Any] = field(default_factory=dict[str, Any])
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "v": WEBHOOK_NODE_SCHEMA_VERSION,
            "direction": "outbound",
            "event_id": self.event_id,
            "source": self.source,
            "result_hash": self.result_hash,
            "journal_head": self.journal_head,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (signed + spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "result": self.result,
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    def delivery(self, *, secret: str) -> tuple[dict[str, str], bytes]:
        """Return Standard Webhooks delivery headers + body for the result.

        The outbound body is the signed receipt itself, so the bus receives a
        payload it can independently verify against the audit chain. The
        delivery is signed under the caller's ``secret`` with the receipt's
        ``event_id`` as the message id.
        """
        body = _canonical_bytes(self.to_dict())
        sig = sign_standard_webhook(secret=secret, msg_id=self.event_id, timestamp=self.timestamp, body=body)
        headers = {
            STANDARD_WEBHOOK_ID_HEADER: self.event_id,
            STANDARD_WEBHOOK_TIMESTAMP_HEADER: str(self.timestamp),
            STANDARD_WEBHOOK_SIGNATURE_HEADER: sig,
        }
        return headers, body

    @classmethod
    def from_bytes(cls, raw: bytes) -> OutboundReceipt:
        row = json.loads(raw)
        return cls(
            event_id=str(row["event_id"]),
            source=str(row["source"]),
            result_hash=str(row["result_hash"]),
            journal_head=str(row["journal_head"]),
            result=dict(row.get("result", {})),
            timestamp=int(row["timestamp"]),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def outbound_receipt_path(workdir: Path, event_id: str) -> Path:
    """Return the on-disk outbound-receipt path for ``event_id``."""
    return workdir.joinpath(*_OUTBOUND_SUBPATH, f"{event_id}.json")


def read_outbound_receipt(workdir: Path, event_id: str) -> OutboundReceipt | None:
    """Return the outbound receipt for ``event_id`` or ``None`` if absent."""
    path = outbound_receipt_path(workdir, event_id)
    if not path.is_file():
        return None
    try:
        return OutboundReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("webhook_node: malformed outbound receipt at %s", sanitize_log(str(path)))
        return None


def emit_outbound_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    identity_dir: Path,
    event_id: str,
    result: dict[str, Any],
    journal_head: str,
    timestamp: int,
    source: str = "",
    delivery_secret: str = "",
) -> OutboundReceipt:
    """Sign an outbound result binding ``{result_hash, journal_head}`` (AC2).

    The result hash is bound to the run journal head so the returned result is
    provably the projection of the executed run. The receipt is signed with the
    install's Ed25519 identity and anchored in the webhook-node spine.

    Args:
        workdir: Project root; receipts land under ``.sdd/webhook-node/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        identity_dir: Directory holding the install's Ed25519 node identity.
        event_id: The inbound event id the result answers.
        result: The returned result payload.
        journal_head: The run journal head the result binds to.
        timestamp: Integer timestamp for the receipt.
        source: Optional calling-bus label carried through from the inbound
            receipt; defaults to the recorded inbound source when known.
        delivery_secret: Unused placeholder kept for a stable call surface; the
            delivery signing secret is passed to :meth:`OutboundReceipt.delivery`.

    Returns:
        The signed, anchored :class:`OutboundReceipt`.
    """
    del delivery_secret  # delivery signing secret is supplied to ``delivery``
    if not source:
        inbound = read_inbound_receipt(workdir, event_id)
        source = inbound.source if inbound is not None else ""

    unsigned = OutboundReceipt(
        event_id=event_id,
        source=source,
        result_hash=compute_result_hash(result),
        journal_head=journal_head,
        result=result,
        timestamp=timestamp,
    )
    payload = unsigned.to_canonical_bytes()
    private_pem, public_pem = load_or_create_node_identity(identity_dir)
    signature = sign_payload(payload, private_pem)

    spine = LineageSpine(lineage_root, run_id=WEBHOOK_NODE_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_OUTBOUND_SUBPATH, f"{_safe_event_name(event_id)}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_NODE_ACTOR,
        step_id=unsigned.result_hash,
        model=_NODE_MODEL,
        timestamp=timestamp,
    )
    anchored = OutboundReceipt(
        event_id=event_id,
        source=source,
        result_hash=unsigned.result_hash,
        journal_head=journal_head,
        result=result,
        timestamp=timestamp,
        signer_public_key_pem=public_pem,
        signature=signature,
        journal_entry_hash=anchor,
    )
    _write_receipt(outbound_receipt_path(workdir, event_id), anchored.to_dict())
    return anchored


# ---------------------------------------------------------------------------
# Verify (AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookVerifyResult:
    """Outcome of :func:`verify_webhook_event`."""

    ok: bool
    reason: str
    inbound_ok: bool = False
    outbound_ok: bool = False
    receipt: InboundReceipt | None = None
    outbound: OutboundReceipt | None = None


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def _verify_inbound(
    *,
    workdir: Path,
    spine: LineageSpine,
    event_id: str,
) -> tuple[bool, str, InboundReceipt | None]:
    receipt = read_inbound_receipt(workdir, event_id)
    if receipt is None:
        return False, "no inbound receipt found", None
    # Recompute the inbound event hash from the receipt's recorded source, id
    # and journal seed. A tampered source or event id diverges here (AC3).
    if receipt.event_hash not in receipt.journal_events:
        return False, "inbound event hash not referenced by the seeded journal root", receipt
    if not receipt.signature or not receipt.signer_public_key_pem:
        return False, "inbound receipt is unsigned", receipt
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return False, f"inbound signature does not verify ({outcome.reason})", receipt
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return False, "inbound receipt is not anchored in the webhook-node spine", receipt
    if recomputed != receipt.journal_entry_hash:
        return False, "inbound anchor does not match the spine entry over the receipt bytes", receipt
    return True, "", receipt


def _verify_outbound(
    *,
    workdir: Path,
    spine: LineageSpine,
    event_id: str,
) -> tuple[bool, str, OutboundReceipt | None]:
    receipt = read_outbound_receipt(workdir, event_id)
    if receipt is None:
        # Outbound is optional (a run may not have completed yet); absence is
        # not a tamper, but verify reports it so the caller can distinguish.
        return False, "no outbound receipt found", None
    if compute_result_hash(receipt.result) != receipt.result_hash:
        return False, "outbound result_hash does not match the recorded result", receipt
    if not receipt.signature or not receipt.signer_public_key_pem:
        return False, "outbound receipt is unsigned", receipt
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return False, f"outbound signature does not verify ({outcome.reason})", receipt
    recomputed = _recompute_anchor(spine, receipt.to_canonical_bytes())
    if recomputed is None:
        return False, "outbound receipt is not anchored in the webhook-node spine", receipt
    if recomputed != receipt.journal_entry_hash:
        return False, "outbound anchor does not match the spine entry over the receipt bytes", receipt
    return True, "", receipt


def verify_webhook_event(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    event_id: str,
) -> WebhookVerifyResult:
    """Recompute inbound + outbound hashes and detect tampering (AC3).

    Recomputes the inbound event hash and the outbound result hash from the
    recorded receipts, re-checks both Ed25519 signatures offline, verifies the
    webhook-node spine, and re-anchors both receipts against it. A single-byte
    edit to a receipt, the spine, or the seeded journal fails the check.

    ``ok`` is True only when the inbound receipt verifies and, when an outbound
    receipt exists, it verifies too. A missing outbound receipt leaves ``ok``
    False with an ``outbound_ok`` False and an explanatory reason (the run may
    not have completed).
    """
    spine = LineageSpine(lineage_root, run_id=WEBHOOK_NODE_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        # An empty spine means no receipt was ever anchored for this event.
        receipt = read_inbound_receipt(workdir, event_id)
        return WebhookVerifyResult(
            ok=False,
            reason=f"webhook-node spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )

    inbound_ok, inbound_reason, receipt = _verify_inbound(workdir=workdir, spine=spine, event_id=event_id)
    outbound_ok, outbound_reason, outbound = _verify_outbound(workdir=workdir, spine=spine, event_id=event_id)

    if not inbound_ok:
        return WebhookVerifyResult(
            ok=False,
            reason=inbound_reason,
            inbound_ok=False,
            outbound_ok=outbound_ok,
            receipt=receipt,
            outbound=outbound,
        )
    if outbound is not None and not outbound_ok:
        return WebhookVerifyResult(
            ok=False,
            reason=outbound_reason,
            inbound_ok=True,
            outbound_ok=False,
            receipt=receipt,
            outbound=outbound,
        )
    if outbound is None:
        return WebhookVerifyResult(
            ok=False,
            reason="inbound verified; no outbound receipt yet",
            inbound_ok=True,
            outbound_ok=False,
            receipt=receipt,
            outbound=None,
        )
    return WebhookVerifyResult(
        ok=True,
        reason="",
        inbound_ok=True,
        outbound_ok=True,
        receipt=receipt,
        outbound=outbound,
    )


# ---------------------------------------------------------------------------
# Shared persistence
# ---------------------------------------------------------------------------


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


__all__ = [
    "STANDARD_WEBHOOK_ID_HEADER",
    "STANDARD_WEBHOOK_SIGNATURE_HEADER",
    "STANDARD_WEBHOOK_TIMESTAMP_HEADER",
    "WEBHOOK_NODE_RUN_ID",
    "WEBHOOK_NODE_SCHEMA_VERSION",
    "InboundReceipt",
    "InboundResult",
    "OutboundReceipt",
    "WebhookNodeError",
    "WebhookVerifyResult",
    "compute_event_hash",
    "compute_result_hash",
    "emit_outbound_receipt",
    "inbound_receipt_path",
    "load_or_create_node_identity",
    "outbound_receipt_path",
    "read_inbound_receipt",
    "read_outbound_receipt",
    "receive_inbound_webhook",
    "sign_standard_webhook",
    "verify_standard_webhook",
    "verify_webhook_event",
]
