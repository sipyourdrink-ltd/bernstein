"""``TransactionReceipt`` - the chain-anchored artefact of every transaction attempt.

A receipt is emitted for *every* attempt to spend under a mandate, whether it is
authorized or refused. Its identity is the ``sha256`` of its canonical body, and
that body is appended to the lineage store (landing as a lineage record with a
detached-JWS ``.jws`` sidecar) and mirrored as an HMAC-chained audit event. Strip
the lineage signature or the audit chain and a receipt is just a JSON file; with
them it is an offline-verifiable proof that a specific transaction was authorized
(or refused, and why) under a specific mandate at a specific chain position.

The receipt records which presence mode authorized it and, when refused, a
closed-enum :class:`RefusalReason` that is hash-bound to the mandate (the body
carries the ``mandate_hash``), so a denied attempt is as reconstructable as an
approved one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.lineage.entry import LineageEntry, canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.identity import AgentCard, verify_detached
from bernstein.core.lineage.signed_write import seal_write
from bernstein.core.lineage.store import LineageStore
from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.audit_chain import (
    EVENT_PAYMENT_AUTHORIZED,
    EVENT_PAYMENT_REFUSED,
    AuditChainStore,
    record_payment_authorized,
    record_payment_refused,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.payments._identity import OperatorIdentity
    from bernstein.core.payments.mandate import SpendMandate

__all__ = [
    "Decision",
    "ReceiptVerification",
    "RefusalReason",
    "TransactionReceipt",
    "anchor_receipt",
    "load_receipt",
    "receipt_artefact_path",
    "receipts_dir",
    "verify_receipt",
]

#: Receipt schema version.
RECEIPT_VERSION: int = 1

#: Span id recorded on the receipt's lineage entry. Receipts are not produced
#: inside an OTel span, so a fixed all-zero span keeps the entry deterministic.
_RECEIPT_SPAN_ID: str = "0" * 16

#: Lineage ``artefact_kind`` for a receipt: an ``.sdd`` runtime artefact.
_RECEIPT_ARTEFACT_KIND: str = "sdd-runtime"


class Decision(StrEnum):
    """Whether a transaction attempt was admitted."""

    AUTHORIZED = "authorized"
    REFUSED = "refused"


class RefusalReason(StrEnum):
    """Closed set of reasons a transaction attempt is refused.

    The enum is deliberately closed: enforcement may only refuse for one of
    these reasons, so a refusal receipt is machine-classifiable and a verifier
    can reason about the full denial surface.
    """

    OVER_MAX_AMOUNT = "over_max_amount"
    WRONG_RECIPIENT = "wrong_recipient"
    EXPIRED = "expired"
    CUMULATIVE_EXCEEDED = "cumulative_exceeded"
    BAD_SIGNATURE = "bad_signature"
    WRONG_PRESENCE_MODE = "wrong_presence_mode"


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    """A single transaction attempt's chain-anchored record.

    The *body* fields (everything except the anchor metadata) are what the
    ``receipt_hash`` addresses and what the lineage entry's content hash covers.
    The anchor metadata (``lineage_entry_hash``, ``prev_chain_digest``) is filled
    in by :func:`anchor_receipt` after the append and is intentionally excluded
    from the content hash -- it references the append, so it cannot be an input
    to it.
    """

    v: int
    mandate_hash: str
    amount_nanos: str
    currency: str
    recipient: str
    category: str
    presence_mode: str
    decision: str
    now: int
    nonce: str
    refusal_reason: str | None = None
    # Anchor metadata (post-append; not part of the content hash).
    lineage_entry_hash: str | None = None
    prev_chain_digest: str | None = None

    # -- canonical forms ----------------------------------------------------

    def body(self) -> dict[str, object]:
        """Return the content-addressed body dict (no anchor metadata).

        ``refusal_reason`` is included only for a refusal so an authorized
        receipt canonicalises without a null field.
        """
        out: dict[str, object] = {
            "v": self.v,
            "mandate_hash": self.mandate_hash,
            "amount_nanos": self.amount_nanos,
            "currency": self.currency,
            "recipient": self.recipient,
            "category": self.category,
            "presence_mode": self.presence_mode,
            "decision": self.decision,
            "now": self.now,
            "nonce": self.nonce,
        }
        if self.refusal_reason is not None:
            out["refusal_reason"] = self.refusal_reason
        return out

    def body_bytes(self) -> bytes:
        """Return the JCS-canonical bytes of the body (the lineage content)."""
        return canonicalize_jcs(self.body())

    def receipt_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of the body."""
        return "sha256:" + hashlib.sha256(self.body_bytes()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """Return the full persisted dict: body + anchor metadata + receipt hash."""
        out = self.body()
        out["receipt_hash"] = self.receipt_hash()
        if self.lineage_entry_hash is not None:
            out["lineage_entry_hash"] = self.lineage_entry_hash
        if self.prev_chain_digest is not None:
            out["prev_chain_digest"] = self.prev_chain_digest
        return out

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> TransactionReceipt:
        """Reconstruct a receipt from its persisted dict (inverse of :meth:`to_dict`)."""
        rr = row.get("refusal_reason")
        leh = row.get("lineage_entry_hash")
        pcd = row.get("prev_chain_digest")
        return cls(
            v=int(str(row["v"])),
            mandate_hash=str(row["mandate_hash"]),
            amount_nanos=str(row["amount_nanos"]),
            currency=str(row["currency"]),
            recipient=str(row["recipient"]),
            category=str(row["category"]),
            presence_mode=str(row["presence_mode"]),
            decision=str(row["decision"]),
            now=int(str(row["now"])),
            nonce=str(row["nonce"]),
            refusal_reason=None if rr is None else str(rr),
            lineage_entry_hash=None if leh is None else str(leh),
            prev_chain_digest=None if pcd is None else str(pcd),
        )

    def with_anchor(self, *, lineage_entry_hash: str, prev_chain_digest: str) -> TransactionReceipt:
        """Return a copy carrying the anchor metadata from the append."""
        return TransactionReceipt(
            v=self.v,
            mandate_hash=self.mandate_hash,
            amount_nanos=self.amount_nanos,
            currency=self.currency,
            recipient=self.recipient,
            category=self.category,
            presence_mode=self.presence_mode,
            decision=self.decision,
            now=self.now,
            nonce=self.nonce,
            refusal_reason=self.refusal_reason,
            lineage_entry_hash=lineage_entry_hash,
            prev_chain_digest=prev_chain_digest,
        )


# ---------------------------------------------------------------------------
# On-disk layout
# ---------------------------------------------------------------------------


def receipts_dir(workdir: Path) -> Path:
    """Return the directory holding persisted receipt JSON files."""
    return workdir / ".sdd" / "payments" / "receipts"


def _safe_hash_stem(content_hash: str) -> str:
    """Return the ``sha256:``-stripped hex of *content_hash* for a filename."""
    return content_hash.split(":", 1)[1] if ":" in content_hash else content_hash


def receipt_artefact_path(receipt_hash: str) -> str:
    """Return the repo-relative POSIX lineage artefact path for a receipt."""
    return f".sdd/payments/receipts/{_safe_hash_stem(receipt_hash)}.json"


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def anchor_receipt(
    receipt: TransactionReceipt,
    *,
    workdir: Path,
    hmac_key: bytes,
    identity: OperatorIdentity,
    chain: AuditChainStore,
) -> TransactionReceipt:
    """Append *receipt* to the lineage store, mirror it into the audit chain, persist it.

    Returns the anchored receipt carrying ``lineage_entry_hash`` and the
    ``prev_chain_digest`` captured at decision time (read back from the audit
    event so it equals the value embedded in the chain exactly).

    The lineage content is the receipt *body* bytes, so the lineage entry's
    ``content_hash`` equals ``receipt.receipt_hash()``; a verifier recomputing
    the body from the persisted file detects any post-hoc mutation.
    """
    receipt_hash = receipt.receipt_hash()
    body_bytes = receipt.body_bytes()

    store = LineageStore(workdir / ".sdd" / "lineage")
    lineage_entry_hash = seal_write(
        store,
        hmac_key,
        artefact_path=receipt_artefact_path(receipt_hash),
        new_content=body_bytes,
        agent_id=identity.agent_card.agent_id,
        agent_card=identity.agent_card,
        private_key_pem=identity.private_pem,
        tool_call_id=receipt_hash,
        span_id=_RECEIPT_SPAN_ID,
        artefact_kind=_RECEIPT_ARTEFACT_KIND,
    )

    if receipt.decision == Decision.AUTHORIZED.value:
        event = record_payment_authorized(
            chain=chain,
            mandate_hash=receipt.mandate_hash,
            receipt_hash=receipt_hash,
            lineage_entry_hash=lineage_entry_hash,
            amount_nanos=receipt.amount_nanos,
            currency=receipt.currency,
            recipient=receipt.recipient,
            presence_mode=receipt.presence_mode,
        )
    else:
        event = record_payment_refused(
            chain=chain,
            mandate_hash=receipt.mandate_hash,
            receipt_hash=receipt_hash,
            lineage_entry_hash=lineage_entry_hash,
            amount_nanos=receipt.amount_nanos,
            currency=receipt.currency,
            recipient=receipt.recipient,
            presence_mode=receipt.presence_mode,
            refusal_reason=str(receipt.refusal_reason),
        )

    prev_chain_digest = str(event.details["prev_chain_digest"])
    anchored = receipt.with_anchor(
        lineage_entry_hash=lineage_entry_hash,
        prev_chain_digest=prev_chain_digest,
    )

    out_dir = receipts_dir(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{_safe_hash_stem(receipt_hash)}.json").write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


def load_receipt(workdir: Path, receipt_hash: str) -> TransactionReceipt:
    """Load a persisted receipt by its content hash.

    Raises:
        FileNotFoundError: When no receipt with that hash is stored.
    """
    path = receipts_dir(workdir) / f"{_safe_hash_stem(receipt_hash)}.json"
    if not path.exists():
        raise FileNotFoundError(f"no receipt stored at {path}")
    return TransactionReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`.

    ``ok`` is the conjunction of every named check. ``scope`` reports the bound
    amount / recipient / expiry the receipt was checked against so an operator
    reads exactly the envelope the decision applied.
    """

    ok: bool
    checks: dict[str, bool]
    errors: list[str]
    scope: dict[str, str]
    decision: str
    refusal_reason: str | None


def _find_lineage_entry(store: LineageStore, receipt: TransactionReceipt) -> tuple[LineageEntry, str] | None:
    """Return the ``(entry, jws)`` for *receipt*'s artefact, or ``None``.

    Matches on the artefact path derived from the receipt's content hash so a
    receipt whose body was mutated (changing its hash) no longer resolves.
    """
    target_path = receipt_artefact_path(receipt.receipt_hash())
    for entry, jws in store.read_log():
        if entry.artefact_path == target_path:
            return entry, jws
    return None


def verify_receipt(
    *,
    workdir: Path,
    hmac_key: bytes,
    receipt: TransactionReceipt,
    mandate: SpendMandate,
) -> ReceiptVerification:
    """Verify a receipt entirely offline against the lineage and audit substrate.

    Runs five independent checks and reports the bound scope:

    * ``mandate_signature`` -- the mandate's Ed25519 signature verifies.
    * ``mandate_binding`` -- the receipt's ``mandate_hash`` equals this mandate's.
    * ``lineage_signature`` -- the receipt's lineage entry exists, its content
      hash equals the recomputed receipt hash, its operator HMAC matches, and its
      detached JWS verifies against the operator public key. Stripping the
      ``.jws`` sidecar or mutating the receipt body fails this check.
    * ``audit_chain`` -- the whole HMAC chain recomputes; any mutated byte fails.
    * ``audit_event`` -- the payment event for this receipt exists and its
      embedded ``decision``, ``refusal_reason``, ``lineage_entry_hash``,
      ``prev_chain_digest`` and ``mandate_hash`` match the receipt. A removed
      event or a tampered chain digest fails this check.

    A receipt cannot be replayed as an authorization: stripping the chain or the
    signature makes ``ok`` false, so a bare receipt file proves nothing.
    """
    checks: dict[str, bool] = {}
    errors: list[str] = []

    receipt_hash = receipt.receipt_hash()

    # 1. Mandate signature.
    checks["mandate_signature"] = mandate.verify_signature()
    if not checks["mandate_signature"]:
        errors.append("mandate signature does not verify")

    # 2. Receipt is bound to this mandate.
    mandate_hash = mandate.mandate_hash()
    checks["mandate_binding"] = receipt.mandate_hash == mandate_hash
    if not checks["mandate_binding"]:
        errors.append(f"receipt mandate_hash {receipt.mandate_hash} != mandate hash {mandate_hash}")

    # 3. Lineage entry + detached-JWS sidecar.
    lineage_ok = False
    store = LineageStore(workdir / ".sdd" / "lineage")
    found = _find_lineage_entry(store, receipt)
    if found is None:
        errors.append("no lineage entry for receipt artefact (signature substrate missing)")
    else:
        entry, jws = found
        card = AgentCard(agent_id="operator", kid=mandate.kid, public_key_pem=mandate.public_key_pem)
        content_matches = entry.content_hash == receipt_hash
        hmac_matches = compute_operator_hmac(entry, hmac_key) == entry.operator_hmac
        sig_matches = bool(jws) and verify_detached(canonicalise(entry), jws, card)
        lineage_ok = content_matches and hmac_matches and sig_matches
        if not content_matches:
            errors.append("lineage content hash does not match recomputed receipt hash")
        if not hmac_matches:
            errors.append("lineage entry operator HMAC does not verify")
        if not sig_matches:
            errors.append("lineage detached JWS does not verify (missing or invalid)")
    checks["lineage_signature"] = lineage_ok

    # 4. Full audit-chain HMAC recomputation.
    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    chain_ok, chain_errors = chain.verify()
    checks["audit_chain"] = chain_ok
    if not chain_ok:
        errors.extend(chain_errors)

    # 5. The payment event mirroring this receipt.
    event_ok = False
    lineage_entry_hash = None if found is None else entry_hash(found[0])
    events = chain.query(resource_id=receipt_hash)
    payment_events = [e for e in events if e.event_type in {EVENT_PAYMENT_AUTHORIZED, EVENT_PAYMENT_REFUSED}]
    if not payment_events:
        errors.append("no payment audit event for receipt (chain anchor missing)")
    else:
        ev = payment_events[-1]
        d = ev.details
        expected_decision = receipt.decision
        expected_reason = receipt.refusal_reason
        checks_pass = (
            d.get("decision") == expected_decision
            and d.get("mandate_hash") == receipt.mandate_hash
            and d.get("refusal_reason") == expected_reason
            and (receipt.prev_chain_digest is None or d.get("prev_chain_digest") == receipt.prev_chain_digest)
            and (receipt.lineage_entry_hash is None or d.get("lineage_entry_hash") == receipt.lineage_entry_hash)
            and (lineage_entry_hash is None or d.get("lineage_entry_hash") == lineage_entry_hash)
        )
        event_ok = bool(checks_pass)
        if not event_ok:
            errors.append("payment audit event details do not match the receipt")
    checks["audit_event"] = event_ok

    scope = {
        "max_amount_nanos": mandate.max_amount_nanos,
        "recipient": mandate.recipient,
        "not_after": str(mandate.not_after),
        "currency": mandate.currency,
        "presence_mode": mandate.presence_mode,
    }
    ok = all(checks.values())
    return ReceiptVerification(
        ok=ok,
        checks=checks,
        errors=errors,
        scope=scope,
        decision=receipt.decision,
        refusal_reason=receipt.refusal_reason,
    )
