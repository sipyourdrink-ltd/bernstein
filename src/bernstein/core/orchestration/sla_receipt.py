"""Signed, offline-verifiable SLA violation receipts (#2549).

An SLA breach is not a log line; it is a signed, lineage-anchored receipt. This
module mirrors the escalation-receipt discipline of
:mod:`bernstein.core.orchestration.supervisor_receipt`: canonical bytes, an
Ed25519 signature, the ``contract_hash``, the window of chain entries that
evidence the miss, the verdict inputs, the deterministic remediation chosen, and
``prev_chain_digest`` linking the receipt into the tamper-evident audit log.

The receipt IS the SLA report. It verifies offline, byte for byte:
:func:`verify_receipt` reads nothing but the receipt. It

1. recomputes the ``contract_hash`` from the embedded contract body (a tampered
   threshold no longer hashes to the recorded ``contract_hash``),
2. re-runs the pure axis evaluators over the embedded evidence at the embedded
   tick instant and confirms every recorded verdict reproduces (the same
   guarantee :func:`supervisor_receipt.recommend_action` re-derivation makes),
3. re-runs the deterministic remediation selection and budget gate and confirms
   the recorded remediation reproduces,
4. walks the embedded chain slice and confirms its ``prev_chain_digest`` linkage
   is unbroken,
5. checks the Ed25519 signature against the embedded public key (whose keyid is
   bound into the signed body, so a swapped key is self-inconsistent).

Strip the audit chain, the lineage spine, and the deterministic evaluators and
the receipt has nothing to verify against; that coupling is the point. Flipping
any single byte of the receipt fails verification -- either the payload digest,
the re-derivation, the linkage, or the signature.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from bernstein.core.observability.sla_eval import (
    AxisVerdict,
    RemediationDecision,
    RemediationPlan,
    any_breach,
    evaluate_all,
    gate_remediation,
    select_remediation,
)
from bernstein.core.orchestration.supervisor_receipt import IdentityTokens
from bernstein.core.planning.sla_store import (
    SLAContract,
    SLAContractError,
    compute_contract_id,
    contract_from_dict,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Schema version embedded in every SLA receipt. Bump only on a breaking change.
SLA_RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Receipts live under this subpath of ``.sdd``.
_RECEIPT_SUBPATH = ("runtime", "sla", "receipts")


class SLAReceiptError(RuntimeError):
    """Raised when receipt assembly or persistence fails."""


# ---------------------------------------------------------------------------
# Receipt dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SLAViolationReceipt:
    """A portable, signed envelope describing one SLA breach.

    The signed payload is canonical JSON (sorted keys, compact separators), so
    two operators serialising the same envelope produce byte-identical signing
    inputs. Adding a field requires a schema-version bump.
    """

    schema_version: str
    contract_id: str
    contract_hash: str
    contract_body: dict[str, Any]
    tick_instant: int
    verdicts: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]
    caps: dict[str, float]
    remediation: dict[str, Any]
    audit_entries: tuple[dict[str, Any], ...]
    identity: IdentityTokens
    public_key_pem: str
    prev_chain_digest: str
    payload_digest: str
    signature_b64: str = ""
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def is_signed(self) -> bool:
        return bool(self.signature_b64)

    @property
    def receipt_id(self) -> str:
        """The stable id used for on-disk storage: ``<contract_id>-<tick>``."""
        return f"{self.contract_id}-{self.tick_instant}"


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`."""

    ok: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalise(value: Any) -> Any:
    """Force a value through JSON so non-native types raise here, not at signing."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _signing_payload_dict(receipt: SLAViolationReceipt) -> dict[str, Any]:
    """Return the dict that gets signed (excludes signature + payload_digest)."""
    return {
        "schema_version": receipt.schema_version,
        "contract_id": receipt.contract_id,
        "contract_hash": receipt.contract_hash,
        "contract_body": receipt.contract_body,
        "tick_instant": receipt.tick_instant,
        "verdicts": list(receipt.verdicts),
        "evidence": receipt.evidence,
        "caps": receipt.caps,
        "remediation": receipt.remediation,
        "audit_entries": list(receipt.audit_entries),
        "identity": receipt.identity.to_dict(),
        "public_key_pem": receipt.public_key_pem,
        "prev_chain_digest": receipt.prev_chain_digest,
        "details": receipt.details,
    }


def canonical_receipt_bytes(receipt: SLAViolationReceipt) -> bytes:
    """Return the canonical signing bytes for a receipt."""
    return _canonical_json(_signing_payload_dict(receipt))


def _payload_digest(receipt: SLAViolationReceipt) -> str:
    return hashlib.sha256(canonical_receipt_bytes(receipt)).hexdigest()


def receipt_to_dict(receipt: SLAViolationReceipt) -> dict[str, Any]:
    """Return the full canonical dict view of a receipt (for JSON storage)."""
    payload = _signing_payload_dict(receipt)
    payload["payload_digest"] = receipt.payload_digest
    payload["signature_b64"] = receipt.signature_b64
    return payload


def receipt_from_dict(payload: dict[str, Any]) -> SLAViolationReceipt:
    """Build a receipt from its canonical dict view."""
    identity_raw = payload.get("identity")
    identity_dict = cast("dict[str, Any]", identity_raw) if isinstance(identity_raw, dict) else {}
    identity = IdentityTokens(
        install_rev=str(identity_dict.get("install_rev", "")),
        keyid=str(identity_dict.get("keyid", "")),
        run_id=str(identity_dict.get("run_id", "")),
    )
    verdicts_raw = payload.get("verdicts", [])
    verdicts = tuple(cast("dict[str, Any]", v) for v in verdicts_raw if isinstance(v, dict))
    entries_raw = payload.get("audit_entries", [])
    entries = tuple(cast("dict[str, Any]", e) for e in entries_raw if isinstance(e, dict))
    evidence_raw = payload.get("evidence", {})
    evidence = cast("dict[str, Any]", evidence_raw) if isinstance(evidence_raw, dict) else {}
    caps_raw = payload.get("caps", {})
    caps = {str(k): float(v) for k, v in cast("dict[str, Any]", caps_raw).items()} if isinstance(caps_raw, dict) else {}
    remediation_raw = payload.get("remediation", {})
    remediation = cast("dict[str, Any]", remediation_raw) if isinstance(remediation_raw, dict) else {}
    body_raw = payload.get("contract_body", {})
    body = cast("dict[str, Any]", body_raw) if isinstance(body_raw, dict) else {}
    details_raw = payload.get("details", {})
    details = cast("dict[str, Any]", details_raw) if isinstance(details_raw, dict) else {}
    return SLAViolationReceipt(
        schema_version=str(payload.get("schema_version", "")),
        contract_id=str(payload.get("contract_id", "")),
        contract_hash=str(payload.get("contract_hash", "")),
        contract_body=body,
        tick_instant=int(payload.get("tick_instant", 0)),
        verdicts=verdicts,
        evidence=evidence,
        caps=caps,
        remediation=remediation,
        audit_entries=entries,
        identity=identity,
        public_key_pem=str(payload.get("public_key_pem", "")),
        prev_chain_digest=str(payload.get("prev_chain_digest", "")),
        payload_digest=str(payload.get("payload_digest", "")),
        signature_b64=str(payload.get("signature_b64", "")),
        details=details,
    )


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def keyid_for(public_key: Ed25519PublicKey) -> str:
    """Return the sha256 of the public key's DER bytes (the stable key id)."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _public_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    contract: SLAContract,
    evidence: dict[str, Any],
    now: int,
    caps: dict[str, float] | None,
    audit_entries: list[dict[str, Any]],
    identity: IdentityTokens,
    public_key: Ed25519PublicKey,
    prev_chain_digest: str,
    details: dict[str, Any] | None = None,
) -> SLAViolationReceipt | None:
    """Assemble an unsigned violation receipt, or ``None`` when no axis breached.

    Runs the pure evaluators, selects the deterministic remediation, gates a
    spend-more remediation through the budget envelope, and embeds every input
    so the receipt re-derives offline. Returns ``None`` when the contract is met
    (no receipt is written for a green tick).
    """
    verdicts = evaluate_all(contract, evidence, now)
    if not any_breach(verdicts):
        return None
    plan = select_remediation(contract, verdicts)
    spend_rows_raw: Any = evidence.get("spend_rate", [])
    spend_rows = cast("list[dict[str, Any]]", spend_rows_raw) if isinstance(spend_rows_raw, list) else []
    decision = gate_remediation(
        plan,
        spend_rows=spend_rows,
        caps=caps,
        remediation_cost_usd=contract.remediation_cost_usd,
        tick_instant=now,
    )
    normalised_evidence = cast("dict[str, Any]", _normalise(evidence))
    normalised_entries = [cast("dict[str, Any]", _normalise(e)) for e in audit_entries]
    base = SLAViolationReceipt(
        schema_version=SLA_RECEIPT_SCHEMA_VERSION,
        contract_id=contract.id,
        contract_hash=contract.contract_hash,
        contract_body=contract.canonical_body(),
        tick_instant=int(now),
        verdicts=tuple(v.to_dict() for v in verdicts),
        evidence=normalised_evidence,
        caps=dict(caps or {}),
        remediation=decision.to_dict(),
        audit_entries=tuple(normalised_entries),
        identity=identity,
        public_key_pem=_public_pem(public_key),
        prev_chain_digest=prev_chain_digest,
        payload_digest="",
        signature_b64="",
        details=details or {},
    )
    digest = _payload_digest(base)
    return _replace_digest(base, digest)


def _replace_digest(receipt: SLAViolationReceipt, digest: str) -> SLAViolationReceipt:
    return SLAViolationReceipt(
        schema_version=receipt.schema_version,
        contract_id=receipt.contract_id,
        contract_hash=receipt.contract_hash,
        contract_body=receipt.contract_body,
        tick_instant=receipt.tick_instant,
        verdicts=receipt.verdicts,
        evidence=receipt.evidence,
        caps=receipt.caps,
        remediation=receipt.remediation,
        audit_entries=receipt.audit_entries,
        identity=receipt.identity,
        public_key_pem=receipt.public_key_pem,
        prev_chain_digest=receipt.prev_chain_digest,
        payload_digest=digest,
        signature_b64=receipt.signature_b64,
        details=receipt.details,
    )


def sign_receipt(receipt: SLAViolationReceipt, *, signing_key: Ed25519PrivateKey) -> SLAViolationReceipt:
    """Attach an Ed25519 signature. Ed25519 is deterministic (RFC 8032)."""
    sig = signing_key.sign(canonical_receipt_bytes(receipt))
    sig_b64 = base64.b64encode(sig).decode("ascii")
    return SLAViolationReceipt(
        schema_version=receipt.schema_version,
        contract_id=receipt.contract_id,
        contract_hash=receipt.contract_hash,
        contract_body=receipt.contract_body,
        tick_instant=receipt.tick_instant,
        verdicts=receipt.verdicts,
        evidence=receipt.evidence,
        caps=receipt.caps,
        remediation=receipt.remediation,
        audit_entries=receipt.audit_entries,
        identity=receipt.identity,
        public_key_pem=receipt.public_key_pem,
        prev_chain_digest=receipt.prev_chain_digest,
        payload_digest=receipt.payload_digest,
        signature_b64=sig_b64,
        details=receipt.details,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify_chain_linkage(entries: tuple[dict[str, Any], ...]) -> list[str]:
    """Confirm the embedded chain slice links each entry to its predecessor.

    Each audit entry carries its own ``hmac`` and the ``prev_hmac`` of the entry
    before it (the same linkage :class:`AuditLog` writes on disk). On consecutive
    entries the later entry's ``prev_hmac`` must equal the earlier entry's
    ``hmac``; a tampered ``hmac`` inside the slice breaks the link and is
    reported. Entries also mirror ``prev_chain_digest`` inside ``details`` for
    events written via ``log_with_prev_digest``; that is used as a fallback.
    """
    errors: list[str] = []
    prev_hmac: str | None = None
    for idx, entry in enumerate(entries):
        details_raw = entry.get("details")
        details = cast("dict[str, Any]", details_raw) if isinstance(details_raw, dict) else {}
        link = str(entry.get("prev_hmac", "") or details.get("prev_chain_digest", ""))
        if prev_hmac is not None and link and link != prev_hmac:
            errors.append(f"chain slice linkage break at entry {idx}: prev_hmac != preceding hmac")
        this_hmac = str(entry.get("hmac", ""))
        if this_hmac:
            prev_hmac = this_hmac
    return errors


def verify_receipt(receipt: SLAViolationReceipt) -> ReceiptVerification:
    """Verify a receipt offline, using nothing but the receipt itself.

    Order: payload digest, contract-hash recomputation, verdict re-derivation,
    breach-presence, remediation re-derivation, chain-slice linkage, then the
    Ed25519 signature (with keyid self-consistency). Any single-byte mutation of
    the receipt trips at least one check.
    """
    errors: list[str] = []

    expected_digest = _payload_digest(receipt)
    if expected_digest != receipt.payload_digest:
        errors.append(
            f"payload_digest mismatch (expected {expected_digest[:16]}..., got {receipt.payload_digest[:16]}...)"
        )

    # Contract-hash recomputation: the body is inside the signed envelope, so a
    # tampered threshold no longer hashes to the recorded contract_hash.
    _, recomputed_hash = compute_contract_id(receipt.contract_body)
    if recomputed_hash != receipt.contract_hash:
        errors.append("contract_hash does not match the embedded contract body")

    try:
        contract = contract_from_dict(receipt.contract_body)
    except SLAContractError as exc:
        errors.append(f"embedded contract body is invalid: {exc}")
        return ReceiptVerification(ok=False, errors=tuple(errors))

    # Verdict re-derivation from the embedded evidence alone.
    recomputed_verdicts = [v.to_dict() for v in evaluate_all(contract, receipt.evidence, receipt.tick_instant)]
    if recomputed_verdicts != list(receipt.verdicts):
        errors.append("verdicts do not re-derive from the embedded evidence")
    recomputed_objs = [AxisVerdict.from_dict(v) for v in recomputed_verdicts]
    if not any_breach(recomputed_objs):
        errors.append("receipt records no breach; a violation receipt must embed at least one breach")

    # Remediation re-derivation (selection + budget gate).
    plan: RemediationPlan = select_remediation(contract, recomputed_objs)
    spend_rows_raw: Any = receipt.evidence.get("spend_rate", [])
    spend_rows = cast("list[dict[str, Any]]", spend_rows_raw) if isinstance(spend_rows_raw, list) else []
    decision: RemediationDecision = gate_remediation(
        plan,
        spend_rows=spend_rows,
        caps=receipt.caps,
        remediation_cost_usd=contract.remediation_cost_usd,
        tick_instant=receipt.tick_instant,
    )
    if decision.to_dict() != receipt.remediation:
        errors.append("remediation does not re-derive from the embedded verdicts and caps")

    errors.extend(_verify_chain_linkage(receipt.audit_entries))

    # Signature.
    if not receipt.signature_b64:
        errors.append("receipt is unsigned")
    else:
        try:
            public_key = serialization.load_pem_public_key(receipt.public_key_pem.encode("ascii"))
        except (ValueError, TypeError) as exc:
            errors.append(f"embedded public key is unreadable: {exc}")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        if not isinstance(public_key, Ed25519PublicKey):
            errors.append("embedded public key is not an Ed25519 key")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        if receipt.identity.keyid and keyid_for(public_key) != receipt.identity.keyid:
            errors.append("identity keyid does not match the embedded public key")
        try:
            sig = base64.b64decode(receipt.signature_b64)
        except (ValueError, binascii.Error) as exc:
            errors.append(f"signature_b64 not valid base64: {exc}")
            return ReceiptVerification(ok=False, errors=tuple(errors))
        try:
            public_key.verify(sig, canonical_receipt_bytes(receipt))
        except InvalidSignature:
            errors.append("Ed25519 signature does not verify")

    return ReceiptVerification(ok=not errors, errors=tuple(errors))


# ---------------------------------------------------------------------------
# Operator projection
# ---------------------------------------------------------------------------


def project_receipt(receipt: SLAViolationReceipt) -> dict[str, Any]:
    """Return the operator-facing projection (no signature, no raw evidence)."""
    breached = [v.get("axis") for v in receipt.verdicts if v.get("breached")]
    return {
        "receipt_id": receipt.receipt_id,
        "contract_id": receipt.contract_id,
        "contract_hash": receipt.contract_hash,
        "subject_type": receipt.contract_body.get("subject_type", ""),
        "subject_id": receipt.contract_body.get("subject_id", ""),
        "tick_instant": receipt.tick_instant,
        "breached_axes": breached,
        "remediation": receipt.remediation,
        "prev_chain_digest": receipt.prev_chain_digest,
        "signed": receipt.is_signed,
    }


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------


def _safe_receipt_name(receipt_id: str) -> str:
    if not receipt_id:
        raise SLAReceiptError("empty receipt_id")
    if "/" in receipt_id or "\\" in receipt_id or "\x00" in receipt_id:
        raise SLAReceiptError(f"receipt_id contains an unsafe character: {receipt_id!r}")
    return receipt_id


def receipt_path(sdd_dir: Path, receipt_id: str) -> Path:
    """Return the on-disk path for ``receipt_id`` under ``.sdd``."""
    return sdd_dir.joinpath(*_RECEIPT_SUBPATH, f"{_safe_receipt_name(receipt_id)}.json")


def write_receipt(sdd_dir: Path, receipt: SLAViolationReceipt) -> Path:
    """Persist a receipt to ``.sdd/runtime/sla/receipts/<id>.json``; return path."""
    path = receipt_path(sdd_dir, receipt.receipt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt_to_dict(receipt), sort_keys=True, indent=2))
    tmp.replace(path)
    return path


def read_receipt(sdd_dir: Path, receipt_id: str) -> SLAViolationReceipt | None:
    """Load a receipt by id, or ``None`` when absent."""
    path = receipt_path(sdd_dir, receipt_id)
    return read_receipt_file(path)


def read_receipt_file(path: Path) -> SLAViolationReceipt | None:
    """Load a receipt from an explicit file path (used by ``sla verify <file>``)."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load SLA receipt %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return receipt_from_dict(cast("dict[str, Any]", raw))


def load_receipts(sdd_dir: Path) -> list[SLAViolationReceipt]:
    """Load every persisted receipt in ``(tick_instant, contract_id)`` order."""
    root = sdd_dir.joinpath(*_RECEIPT_SUBPATH)
    if not root.exists():
        return []
    out: list[SLAViolationReceipt] = []
    for path in sorted(root.glob("*.json")):
        receipt = read_receipt_file(path)
        if receipt is not None:
            out.append(receipt)
    out.sort(key=lambda r: (r.tick_instant, r.contract_id))
    return out


__all__ = [
    "SLA_RECEIPT_SCHEMA_VERSION",
    "IdentityTokens",
    "ReceiptVerification",
    "SLAReceiptError",
    "SLAViolationReceipt",
    "build_receipt",
    "canonical_receipt_bytes",
    "keyid_for",
    "load_receipts",
    "project_receipt",
    "read_receipt",
    "read_receipt_file",
    "receipt_from_dict",
    "receipt_path",
    "receipt_to_dict",
    "sign_receipt",
    "verify_receipt",
    "write_receipt",
]
