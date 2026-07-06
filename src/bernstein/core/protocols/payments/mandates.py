"""AP2-style spending mandates as journal-anchored consent receipts.

Issue #2306. An agent that spends against an external service must leave
behind proof that a payment was authorized by a specific intent, and the
operator must be able to bound and revoke that authority. This module makes
the artefact the operator sees *be* the verifiable receipt: a spending
mandate is not "a payment plus an audit line", it is a consent receipt
whose identity is a lineage-spine entry hash. Strip the spine and the
receipt is just a file; anchored, it is a chain-verifiable attestation that
recomputes offline.

Shapes (AP2 protocol naming)
----------------------------
* :class:`IntentMandate` -- *what the operator wants*: a bounded authority
  (allowed tool calls, a per-task spend cap, an expiry). Signed with the
  audit-chain HMAC key.
* :class:`CartMandate` -- *the concrete action*: the specific tool calls a
  run proposes to execute under an intent. Signed, and refused unless its
  action set is a subset of what the intent authorizes.
* :class:`SettlementRef` -- the HTTP 402 pay-and-retry settlement reference
  (issue names the x402 / AP2 shapes): the ``402`` challenge digest, the
  payment reference returned on retry, and the retried request digest, bound
  into the receipt so the deterministic decision-to-pay and the settlement
  are cross-verifiable.
* :class:`ConsentReceipt` -- the journal-anchored record binding
  ``{mandate_hash, authorized_tool_calls_hash, settlement_ref,
  journal_entry_hash}`` (AC1). Its ``journal_entry_hash`` is the lineage-spine
  entry hash over the receipt's canonical bytes.

Determinism (AC3)
-----------------
:func:`authorized_action_set` is a pure projection of ``(mandate, state,
time)`` onto a canonical, sorted action set. Two operators with identical
intent, cart, ledger state, and clock authorize the byte-identical set --
never an LLM in the loop. Every mandate/receipt/revocation row is canonical
JSON (sorted keys, minimal separators, UTF-8), so identical inputs produce
byte-identical files and anchors.

Revocation (AC5)
----------------
:func:`revoke_mandate` appends a signed :class:`RevocationEntry` to an
append-only revocation ledger rather than mutating anything. Once a mandate
hash appears there, :func:`authorized_action_set` returns the empty set and
:func:`emit_consent_receipt` refuses, so subsequent actions under the revoked
mandate are refused while the original mandate stays provable.

Spend caps (AC4)
----------------
Each intent carries ``spend_cap_usd``. :func:`emit_consent_receipt` consults
a :class:`~bernstein.core.cost.spend_ledger.SpendLedger` for the cumulative
task spend and refuses to bind a settlement whose amount would breach the
cap, so the cost ledger is the single enforcement point.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bernstein.core.cost.spend_ledger import SpendLedger

logger = logging.getLogger(__name__)

#: Run id under which every consent receipt is anchored. Mandate lineage is
#: kept in one dedicated run so it never interleaves with per-task journals.
MANDATE_RUN_ID = "mandates"

#: Actor recorded on consent-receipt spine entries.
_MANDATE_ACTOR = "bernstein.payment_mandate"

#: Model string recorded on consent-receipt spine entries (no model runs at
#: settlement time; the field is part of the spine schema).
_MANDATE_MODEL = "none"

#: Version stamped into every mandate / receipt / revocation preimage. Bump
#: only on a wire-format change.
MANDATE_SCHEMA_VERSION = 1

_RECEIPT_SUBPATH = (".sdd", "mandates", "receipts")
_REVOCATION_SUBPATH = (".sdd", "mandates", "revocations.jsonl")


# ---------------------------------------------------------------------------
# Canonical hashing / signing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _sign(key: bytes, payload: dict[str, Any]) -> str:
    """Return the HMAC-SHA256 signature over ``payload``'s canonical bytes."""
    return _hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()


def _hash_tool_calls(tool_calls: tuple[str, ...]) -> str:
    """Return the content hash of an ordered, de-duplicated tool-call set.

    The set is sorted and de-duplicated first so the hash is a pure function
    of the *set* of authorized calls, not their listing order -- two callers
    that pass the same calls in a different order bind the identical hash.
    """
    canonical = sorted(set(tool_calls))
    return _sha256({"tool_calls": canonical})


def _safe_hash_name(mandate_hash: str) -> str:
    """Return a filesystem-safe basename for ``mandate_hash``.

    ``mandate_hash`` is a ``sha256:<hex>`` string; the colon is replaced so
    the name is portable and cannot introduce a path separator.
    """
    if not mandate_hash:
        raise ValueError("empty mandate_hash")
    if "/" in mandate_hash or "\\" in mandate_hash or "\x00" in mandate_hash:
        raise ValueError(f"mandate_hash contains an unsafe character: {mandate_hash!r}")
    return mandate_hash.replace(":", "_")


def receipt_path(workdir: Path, mandate_hash: str) -> Path:
    """Return the on-disk consent-receipt path for ``mandate_hash``."""
    return workdir.joinpath(*_RECEIPT_SUBPATH, f"{_safe_hash_name(mandate_hash)}.json")


def revocation_path(workdir: Path) -> Path:
    """Return the append-only revocation ledger path."""
    return workdir.joinpath(*_REVOCATION_SUBPATH)


# ---------------------------------------------------------------------------
# IntentMandate -- what the operator wants (AP2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentMandate:
    """A bounded, signed authority describing *what the operator wants*.

    Attributes:
        task_id: The task the authority is scoped to; the spend cap is
            enforced against this task's cumulative ledger spend.
        allowed_tool_calls: The tool calls the intent authorizes. A cart is
            refused unless its calls are a subset of this set.
        spend_cap_usd: Per-task USD ceiling; a settlement that would breach
            it is refused (AC4). ``0`` means "no spend permitted".
        expires_at: Integer timestamp after which the intent authorizes
            nothing. ``0`` means "never expires".
        signature: HMAC signature over the mandate body; populated by
            :meth:`sign`.
    """

    task_id: str
    allowed_tool_calls: tuple[str, ...]
    spend_cap_usd: float
    expires_at: int = 0
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "kind": "intent",
            "task_id": self.task_id,
            "allowed_tool_calls": sorted(set(self.allowed_tool_calls)),
            "spend_cap_usd": self.spend_cap_usd,
            "expires_at": self.expires_at,
        }

    def sign(self, key: bytes) -> IntentMandate:
        """Return a copy carrying the HMAC signature over the body."""
        return IntentMandate(
            task_id=self.task_id,
            allowed_tool_calls=self.allowed_tool_calls,
            spend_cap_usd=self.spend_cap_usd,
            expires_at=self.expires_at,
            signature=_sign(key, self._body()),
        )

    def verify_signature(self, key: bytes) -> bool:
        """Return True when ``signature`` matches the body under ``key``."""
        if not self.signature:
            return False
        return _hmac.compare_digest(self.signature, _sign(key, self._body()))

    def mandate_hash(self) -> str:
        """Return the content hash of the signed mandate."""
        return _sha256(self._body() | {"signature": self.signature})

    def to_dict(self) -> dict[str, Any]:
        return self._body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> IntentMandate:
        return cls(
            task_id=str(row["task_id"]),
            allowed_tool_calls=tuple(str(c) for c in row.get("allowed_tool_calls", [])),
            spend_cap_usd=float(row.get("spend_cap_usd", 0.0)),
            expires_at=int(row.get("expires_at", 0)),
            signature=str(row.get("signature", "")),
        )


# ---------------------------------------------------------------------------
# CartMandate -- the concrete action (AP2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CartMandate:
    """A signed cart describing *the concrete action* under an intent.

    Attributes:
        intent_hash: The :meth:`IntentMandate.mandate_hash` this cart is
            issued under; binds the cart to a single authority.
        tool_calls: The concrete tool calls the run proposes to execute.
        amount_usd: The settlement amount this cart will incur.
        signature: HMAC signature over the cart body; populated by
            :meth:`sign`.
    """

    intent_hash: str
    tool_calls: tuple[str, ...]
    amount_usd: float
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "kind": "cart",
            "intent_hash": self.intent_hash,
            "tool_calls": sorted(set(self.tool_calls)),
            "amount_usd": self.amount_usd,
        }

    def sign(self, key: bytes) -> CartMandate:
        """Return a copy carrying the HMAC signature over the body."""
        return CartMandate(
            intent_hash=self.intent_hash,
            tool_calls=self.tool_calls,
            amount_usd=self.amount_usd,
            signature=_sign(key, self._body()),
        )

    def verify_signature(self, key: bytes) -> bool:
        """Return True when ``signature`` matches the body under ``key``."""
        if not self.signature:
            return False
        return _hmac.compare_digest(self.signature, _sign(key, self._body()))

    def mandate_hash(self) -> str:
        """Return the content hash of the signed cart."""
        return _sha256(self._body() | {"signature": self.signature})

    def authorized_tool_calls_hash(self) -> str:
        """Return the content hash of this cart's tool-call set."""
        return _hash_tool_calls(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return self._body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> CartMandate:
        return cls(
            intent_hash=str(row["intent_hash"]),
            tool_calls=tuple(str(c) for c in row.get("tool_calls", [])),
            amount_usd=float(row.get("amount_usd", 0.0)),
            signature=str(row.get("signature", "")),
        )


# ---------------------------------------------------------------------------
# SettlementRef -- HTTP 402 pay-and-retry (x402 / AP2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementRef:
    """The HTTP 402 pay-and-retry settlement reference.

    Binding these three digests into the consent receipt makes the
    deterministic decision-to-pay and the settlement cross-verifiable: an
    auditor holding the receipt can confirm the challenge that was answered,
    the payment reference used, and the exact request that was retried.

    Attributes:
        challenge_hash: Digest of the ``HTTP 402`` challenge body.
        payment_ref: Opaque payment reference returned on retry. Never a
            secret credential -- only the settlement's public identifier.
        retried_request_hash: Digest of the request replayed after payment.
        amount_usd: The settled amount, echoed into the receipt.
    """

    challenge_hash: str
    payment_ref: str
    retried_request_hash: str
    amount_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_hash": self.challenge_hash,
            "payment_ref": self.payment_ref,
            "retried_request_hash": self.retried_request_hash,
            "amount_usd": self.amount_usd,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> SettlementRef:
        return cls(
            challenge_hash=str(row["challenge_hash"]),
            payment_ref=str(row["payment_ref"]),
            retried_request_hash=str(row["retried_request_hash"]),
            amount_usd=float(row.get("amount_usd", 0.0)),
        )


# ---------------------------------------------------------------------------
# RevocationEntry -- signed, append-only (AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevocationEntry:
    """One signed revocation of a mandate hash."""

    mandate_hash: str
    reason: str
    timestamp: int
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "kind": "revocation",
            "mandate_hash": self.mandate_hash,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }

    def sign(self, key: bytes) -> RevocationEntry:
        return RevocationEntry(
            mandate_hash=self.mandate_hash,
            reason=self.reason,
            timestamp=self.timestamp,
            signature=_sign(key, self._body()),
        )

    def verify_signature(self, key: bytes) -> bool:
        if not self.signature:
            return False
        return _hmac.compare_digest(self.signature, _sign(key, self._body()))

    def to_row(self) -> bytes:
        return (
            json.dumps(
                self._body() | {"signature": self.signature},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RevocationEntry:
        return cls(
            mandate_hash=str(row["mandate_hash"]),
            reason=str(row.get("reason", "")),
            timestamp=int(row.get("timestamp", 0)),
            signature=str(row.get("signature", "")),
        )


# ---------------------------------------------------------------------------
# ConsentReceipt -- the journal-anchored primary artefact (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentReceipt:
    """The record binding a mandate, the actions it authorized, and settlement.

    Attributes:
        mandate_hash: Content hash of the signed cart mandate.
        intent_hash: Content hash of the authorising intent mandate.
        authorized_tool_calls_hash: Content hash of the cart's tool-call set.
        settlement_ref: The bound HTTP 402 settlement reference.
        task_id: Task the settlement was attributed to.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        journal_entry_hash: The lineage-spine entry hash anchoring the
            receipt. Empty until :func:`emit_consent_receipt` records it.
    """

    mandate_hash: str
    intent_hash: str
    authorized_tool_calls_hash: str
    settlement_ref: SettlementRef
    task_id: str
    timestamp: int
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "mandate_hash": self.mandate_hash,
            "intent_hash": self.intent_hash,
            "authorized_tool_calls_hash": self.authorized_tool_calls_hash,
            "settlement_ref": self.settlement_ref.to_dict(),
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_bytes(cls, raw: bytes) -> ConsentReceipt:
        row = json.loads(raw)
        return cls(
            mandate_hash=str(row["mandate_hash"]),
            intent_hash=str(row["intent_hash"]),
            authorized_tool_calls_hash=str(row["authorized_tool_calls_hash"]),
            settlement_ref=SettlementRef.from_dict(row["settlement_ref"]),
            task_id=str(row["task_id"]),
            timestamp=int(row["timestamp"]),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


# ---------------------------------------------------------------------------
# Revocation ledger (AC5)
# ---------------------------------------------------------------------------


def revoke_mandate(
    *,
    workdir: Path,
    hmac_key: bytes,
    mandate_hash: str,
    reason: str,
    timestamp: int,
) -> RevocationEntry:
    """Append a signed revocation for ``mandate_hash``.

    Revocation is non-destructive: it appends an entry rather than mutating
    the mandate or any prior receipt, so the original mandate stays provable
    while subsequent actions under it are refused.

    Args:
        workdir: Project root; the ledger lands at
            ``.sdd/mandates/revocations.jsonl``.
        hmac_key: The audit-chain HMAC key that signs the entry.
        mandate_hash: The mandate (intent or cart) hash being revoked.
        reason: Human-readable revocation reason.
        timestamp: Integer timestamp for the entry.

    Returns:
        The recorded :class:`RevocationEntry`.
    """
    entry = RevocationEntry(
        mandate_hash=mandate_hash,
        reason=reason,
        timestamp=timestamp,
    ).sign(hmac_key)
    path = revocation_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(entry.to_row())
    return entry


def iter_revocations(workdir: Path) -> Iterator[RevocationEntry]:
    """Yield every recorded revocation entry in append order."""
    path = revocation_path(workdir)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            yield RevocationEntry.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.debug("mandate: skipping malformed revocation row in %s", path)
            continue


def is_revoked(workdir: Path, hmac_key: bytes, mandate_hash: str) -> bool:
    """Return True when a valid, signed revocation exists for ``mandate_hash``.

    Only signature-valid revocations count, so a forged revocation line
    cannot suppress a mandate.
    """
    for entry in iter_revocations(workdir):
        if entry.mandate_hash == mandate_hash and entry.verify_signature(hmac_key):
            return True
    return False


# ---------------------------------------------------------------------------
# Deterministic authorized-action projection (AC3)
# ---------------------------------------------------------------------------


def authorized_action_set(
    *,
    intent: IntentMandate,
    cart: CartMandate,
    hmac_key: bytes,
    now: int,
    workdir: Path | None = None,
) -> tuple[str, ...]:
    """Project ``(mandate, state, time)`` onto the authorized action set.

    The result is the sorted, de-duplicated intersection of the cart's
    proposed calls with the intent's allowed calls, gated by signature
    validity, intent-binding, expiry, and revocation. It is a pure function
    of its inputs: two operators with identical intent, cart, clock, and
    revocation state produce the byte-identical tuple (AC3). No LLM, no
    wall-clock read inside -- the caller supplies ``now``.

    Returns the empty tuple (authorizes nothing) when any gate fails:
    a bad signature, a cart bound to a different intent, an expired intent,
    or a revoked mandate.
    """
    if not intent.verify_signature(hmac_key):
        return ()
    if not cart.verify_signature(hmac_key):
        return ()
    if cart.intent_hash != intent.mandate_hash():
        return ()
    if intent.expires_at and now >= intent.expires_at:
        return ()
    if workdir is not None and (
        is_revoked(workdir, hmac_key, intent.mandate_hash()) or is_revoked(workdir, hmac_key, cart.mandate_hash())
    ):
        return ()
    allowed = set(intent.allowed_tool_calls)
    return tuple(sorted(set(cart.tool_calls) & allowed))


# ---------------------------------------------------------------------------
# Emit consent receipt (AC1, AC4)
# ---------------------------------------------------------------------------


class MandateRefused(RuntimeError):
    """Raised when a settlement cannot be authorized under a mandate."""


def emit_consent_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    intent: IntentMandate,
    cart: CartMandate,
    settlement_ref: SettlementRef,
    now: int,
    ledger: SpendLedger | None = None,
) -> ConsentReceipt:
    """Bind a mandate + its actions + settlement into an anchored receipt.

    The receipt's canonical bytes are the artefact the spine hashes, so the
    returned receipt's ``journal_entry_hash`` is the spine entry hash over
    exactly those bytes: the receipt's chain-verifiable identity (AC1).

    The settlement is refused (raising :class:`MandateRefused`) when:

    * the authorized action set is empty -- bad signature, wrong intent
      binding, expiry, or revocation (AC5);
    * the cart's proposed calls are not all authorized by the intent;
    * ``ledger`` is supplied and the cumulative task spend plus the
      settlement amount would breach the intent's ``spend_cap_usd`` (AC4).

    Args:
        workdir: Project root; the receipt lands under
            ``.sdd/mandates/receipts/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        intent: The authorising intent mandate (signed).
        cart: The concrete cart mandate (signed).
        settlement_ref: The HTTP 402 settlement reference to bind.
        now: Integer timestamp; the receipt timestamp and expiry gate.
        ledger: Optional spend ledger consulted for cap enforcement.

    Returns:
        The anchored :class:`ConsentReceipt`.

    Raises:
        MandateRefused: When the settlement cannot be authorized.
    """
    authorized = authorized_action_set(
        intent=intent,
        cart=cart,
        hmac_key=hmac_key,
        now=now,
        workdir=workdir,
    )
    if not authorized:
        raise MandateRefused("no authorized actions: signature, intent-binding, expiry, or revocation gate failed")
    if set(cart.tool_calls) - set(authorized):
        raise MandateRefused("cart proposes tool calls the intent does not authorize")

    _enforce_spend_cap(intent=intent, cart=cart, ledger=ledger)

    receipt = ConsentReceipt(
        mandate_hash=cart.mandate_hash(),
        intent_hash=intent.mandate_hash(),
        authorized_tool_calls_hash=cart.authorized_tool_calls_hash(),
        settlement_ref=settlement_ref,
        task_id=intent.task_id,
        timestamp=now,
    )
    payload = receipt.to_canonical_bytes()
    path = receipt_path(workdir, receipt.mandate_hash)
    path.parent.mkdir(parents=True, exist_ok=True)

    spine = LineageSpine(lineage_root, run_id=MANDATE_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_RECEIPT_SUBPATH, f"{_safe_hash_name(receipt.mandate_hash)}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_MANDATE_ACTOR,
        step_id=receipt.mandate_hash,
        model=_MANDATE_MODEL,
        timestamp=now,
    )
    anchored = ConsentReceipt(
        mandate_hash=receipt.mandate_hash,
        intent_hash=receipt.intent_hash,
        authorized_tool_calls_hash=receipt.authorized_tool_calls_hash,
        settlement_ref=receipt.settlement_ref,
        task_id=receipt.task_id,
        timestamp=receipt.timestamp,
        journal_entry_hash=anchor,
    )
    # Persist the full receipt (binding + anchor) for offline verification.
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


def _enforce_spend_cap(
    *,
    intent: IntentMandate,
    cart: CartMandate,
    ledger: SpendLedger | None,
) -> None:
    """Refuse the settlement when it would breach the intent's spend cap.

    The cost ledger is the single enforcement point: cumulative task spend
    (from the ledger's ``task`` rollup) plus this cart's amount must not
    exceed ``spend_cap_usd``. A cap of ``0`` permits no spend.
    """
    cap = intent.spend_cap_usd
    if cap < 0:
        cap = 0.0
    prior = 0.0
    if ledger is not None:
        prior = ledger.totals_by("task").get(intent.task_id or "unknown", 0.0)
    projected = prior + max(0.0, cart.amount_usd)
    if projected > cap:
        raise MandateRefused(
            f"spend cap breach: task spend ${prior:.4f} + settlement ${cart.amount_usd:.4f} exceeds cap ${cap:.4f}"
        )


# ---------------------------------------------------------------------------
# Verify (AC2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MandateVerifyResult:
    """Outcome of :func:`verify_consent_receipt`."""

    ok: bool
    reason: str
    receipt: ConsentReceipt | None = None
    authorized_tool_calls: tuple[str, ...] = field(default_factory=tuple)


def read_consent_receipt(workdir: Path, mandate_hash: str) -> ConsentReceipt | None:
    """Return the consent receipt for ``mandate_hash`` or ``None`` if absent."""
    path = receipt_path(workdir, mandate_hash)
    if not path.is_file():
        return None
    try:
        return ConsentReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("mandate: malformed consent receipt at %s", path)
        return None


def verify_consent_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    mandate_hash: str,
    intent: IntentMandate,
    cart: CartMandate,
) -> MandateVerifyResult:
    """Prove offline that ``cart``'s action was authorized by ``intent`` (AC2).

    Recomputes, from the recorded receipt and the presented mandates alone:

    * both mandate signatures under ``hmac_key``;
    * the cart is bound to the presented intent;
    * the receipt's ``mandate_hash`` / ``intent_hash`` /
      ``authorized_tool_calls_hash`` recompute from the mandates;
    * the receipt's ``journal_entry_hash`` still equals the spine entry hash
      over the receipt's canonical bytes, and the mandate spine verifies;
    * the mandate has not been revoked (AC5).

    A single-byte edit to the receipt, either mandate, or the spine fails the
    check. ``ok`` is True only when every recomputation matches.
    """
    receipt = read_consent_receipt(workdir, mandate_hash)
    if receipt is None:
        return MandateVerifyResult(ok=False, reason="no consent receipt found")

    if not intent.verify_signature(hmac_key):
        return MandateVerifyResult(ok=False, reason="intent signature invalid", receipt=receipt)
    if not cart.verify_signature(hmac_key):
        return MandateVerifyResult(ok=False, reason="cart signature invalid", receipt=receipt)
    if cart.intent_hash != intent.mandate_hash():
        return MandateVerifyResult(ok=False, reason="cart is not bound to the presented intent", receipt=receipt)

    if receipt.intent_hash != intent.mandate_hash():
        return MandateVerifyResult(
            ok=False, reason="receipt intent_hash does not match presented intent", receipt=receipt
        )
    if receipt.mandate_hash != cart.mandate_hash():
        return MandateVerifyResult(
            ok=False, reason="receipt mandate_hash does not match presented cart", receipt=receipt
        )
    if receipt.authorized_tool_calls_hash != cart.authorized_tool_calls_hash():
        return MandateVerifyResult(
            ok=False,
            reason="receipt authorized_tool_calls_hash does not match the cart's tool calls",
            receipt=receipt,
        )

    if is_revoked(workdir, hmac_key, receipt.mandate_hash) or is_revoked(workdir, hmac_key, receipt.intent_hash):
        return MandateVerifyResult(ok=False, reason="mandate has been revoked", receipt=receipt)

    spine = LineageSpine(lineage_root, run_id=MANDATE_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return MandateVerifyResult(
            ok=False,
            reason=f"mandate spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )

    recomputed_anchor = _recompute_anchor(spine, receipt)
    if recomputed_anchor is None:
        return MandateVerifyResult(
            ok=False,
            reason="receipt is not anchored in the mandate spine",
            receipt=receipt,
        )
    if recomputed_anchor != receipt.journal_entry_hash:
        return MandateVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the receipt bytes",
            receipt=receipt,
        )

    allowed = set(intent.allowed_tool_calls)
    authorized = tuple(sorted(set(cart.tool_calls) & allowed))
    if set(cart.tool_calls) - allowed:
        return MandateVerifyResult(
            ok=False,
            reason="cart proposes tool calls the intent does not authorize",
            receipt=receipt,
            authorized_tool_calls=authorized,
        )

    return MandateVerifyResult(ok=True, reason="", receipt=receipt, authorized_tool_calls=authorized)


def _recompute_anchor(spine: LineageSpine, receipt: ConsentReceipt) -> str | None:
    """Return the spine entry hash whose content matches the receipt bytes.

    The receipt's canonical binding bytes are what the spine hashed at emit
    time, so a verifier recomputes the content hash and finds the matching
    entry. Returns ``None`` when no entry carries that content hash.
    """
    from bernstein.core.lineage.spine import content_hash_of

    want = content_hash_of(receipt.to_canonical_bytes())
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


__all__ = [
    "MANDATE_RUN_ID",
    "MANDATE_SCHEMA_VERSION",
    "CartMandate",
    "ConsentReceipt",
    "IntentMandate",
    "MandateRefused",
    "MandateVerifyResult",
    "RevocationEntry",
    "SettlementRef",
    "authorized_action_set",
    "emit_consent_receipt",
    "is_revoked",
    "iter_revocations",
    "read_consent_receipt",
    "receipt_path",
    "revocation_path",
    "revoke_mandate",
    "verify_consent_receipt",
]
