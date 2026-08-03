"""Signed, content-addressed diagnosis receipts for ``audit diagnose`` (#2928).

The primary artefact of a diagnosis is not terminal output; it is a receipt
that names the culprit step and can be re-checked offline. The receipt reuses
the machinery the repo's other receipts already trust:

* canonical bytes via the audit-receipt JSON canonicalisation
  (:func:`bernstein.core.security.audit_receipt._canonical_json_bytes`);
* a detached Ed25519 signature over those bytes
  (:mod:`bernstein.core.persistence.lineage_signer`), RFC 8032 deterministic,
  so identical inputs sign to identical bytes;
* an operator HMAC-SHA256 under the audit-chain key, plus the current audit
  chain head embedded as ``chain_head_hmac``, anchoring the finding beside
  the HMAC audit chain.

Verification contract (:func:`verify_diagnosis_receipt`): the verdict is
*re-derived, not trusted*. Beyond checking the receipt hash, the Ed25519
signature, and (when the key is available) the operator HMAC, the verifier
reconstructs the predicate from the receipt's embedded ``signal`` block,
re-runs :func:`~bernstein.core.replay.diagnose.diagnose_run` over the
journal, and asserts the culprit index, step hash, journal head, and reason
code match byte-for-byte. The receipt also pins the exact journal bytes
(``journal_file_sha256``), so a journal mutated anywhere -- at or after the
culprit step included -- fails verification.

Determinism: no wall-clock value enters the receipt, so two independent
invocations over the same journal and key produce byte-identical receipts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.persistence.lineage_signer import (
    Ed25519FileKeySigner,
    Ed25519PublicKeyVerifier,
    LineageSignerError,
)
from bernstein.core.replay.diagnose import DiagnoseError, diagnose_run
from bernstein.core.replay.diagnose_signals import predicate_from_params
from bernstein.core.security.audit_receipt import (
    _canonical_json_bytes,  # pyright: ignore[reportPrivateUsage] - shared receipt canonicalisation
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.lineage_signer import LineageSigner
    from bernstein.core.replay.diagnose import DiagnosisResult

#: Receipt schema version. Bump only on a wire-format change.
DIAGNOSIS_RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Receipt type URL, versioned so a future v2 can co-exist.
DIAGNOSIS_RECEIPT_TYPE: str = "https://bernstein.run/attestations/diagnosis-receipt/v1"

#: Ordered body fields covered by ``receipt_hash`` (everything except the
#: hash itself and the signature envelope).
_BODY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "receipt_type",
    "run_id",
    "journal_head",
    "journal_file_sha256",
    "event_count",
    "chain_head_hmac",
    "culprit_index",
    "culprit_step_hash",
    "reason_code",
    "reason",
    "predicate_id",
    "predicate_hash",
    "signal",
    "lineage_path",
)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class DiagnosisReceiptError(RuntimeError):
    """Raised for receipt build failures (fail closed: no partial writes)."""


@dataclass(frozen=True, slots=True)
class DiagnosisReceipt:
    """A built diagnosis receipt.

    Attributes:
        receipt: The serialisable receipt dict (body + hash + signature).
        receipt_bytes: Canonical JSON bytes (byte-deterministic).
        receipt_path: On-disk path when written, else ``None``.
    """

    receipt: dict[str, Any]
    receipt_bytes: bytes
    receipt_path: Path | None = field(default=None)

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical receipt bytes."""
        return hashlib.sha256(self.receipt_bytes).hexdigest()

    @property
    def receipt_hash(self) -> str:
        """The content address of the receipt body."""
        return str(self.receipt.get("receipt_hash", ""))

    @property
    def culprit_index(self) -> int:
        """The named culprit step index."""
        return int(self.receipt["culprit_index"])


@dataclass(frozen=True, slots=True)
class DiagnosisVerifyResult:
    """Outcome of an offline diagnosis-receipt verification."""

    ok: bool
    reason: str
    hmac_checked: bool = False
    receipt: dict[str, Any] | None = None


def _body_of(result: DiagnosisResult, *, chain_head_hmac: str) -> dict[str, Any]:
    return {
        "schema_version": DIAGNOSIS_RECEIPT_SCHEMA_VERSION,
        "receipt_type": DIAGNOSIS_RECEIPT_TYPE,
        "run_id": result.run_id,
        "journal_head": result.journal_head,
        "journal_file_sha256": result.journal_file_sha256,
        "event_count": result.event_count,
        "chain_head_hmac": chain_head_hmac,
        "culprit_index": result.culprit_index,
        "culprit_step_hash": result.culprit_step_hash,
        "reason_code": result.reason_code,
        "reason": result.reason,
        "predicate_id": result.predicate_id,
        "predicate_hash": result.predicate_hash,
        "signal": dict(result.signal_params),
        "lineage_path": list(result.lineage_path),
    }


def _receipt_hash_of(body: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(body)).hexdigest()


def _signed_payload(body: dict[str, Any], receipt_hash: str) -> bytes:
    payload = dict(body)
    payload["receipt_hash"] = receipt_hash
    return _canonical_json_bytes(payload)


def build_diagnosis_receipt(
    result: DiagnosisResult,
    *,
    chain_head_hmac: str,
    signer: LineageSigner,
    audit_key: bytes,
    output_dir: Path | None = None,
    write: bool = True,
) -> DiagnosisReceipt:
    """Seal a located diagnosis into a signed, content-addressed receipt.

    Args:
        result: A located :class:`DiagnosisResult` (``located`` must be
            ``True`` -- a clean diagnosis carries no culprit and no receipt
            is emitted for it).
        chain_head_hmac: The current HMAC audit-chain head, read (never
            appended) at build time, anchoring the receipt beside the chain.
        signer: Ed25519 signer (the lineage customer-signing machinery).
        audit_key: Operator audit HMAC key; the receipt carries an
            HMAC-SHA256 over the same canonical bytes the signature covers.
        output_dir: Where to write the receipt JSON (required when *write*).
        write: When ``False`` build in-memory only (tests / dry-run).

    Returns:
        The built :class:`DiagnosisReceipt`.

    Raises:
        DiagnosisReceiptError: The result carries no culprit, the run id is
            not a safe path segment, or no output directory was supplied for
            a write.
    """
    if not result.located or result.culprit_index is None:
        raise DiagnosisReceiptError("a clean diagnosis carries no culprit; no receipt is emitted for it")
    if not _RUN_ID_RE.match(result.run_id):
        raise DiagnosisReceiptError(f"unsafe run id for receipt filename: {result.run_id!r}")

    body = _body_of(result, chain_head_hmac=chain_head_hmac)
    receipt_hash = _receipt_hash_of(body)
    payload = _signed_payload(body, receipt_hash)
    signature = signer.sign(payload)
    operator_hmac = _hmac.new(audit_key, payload, hashlib.sha256).hexdigest()

    signature_block: dict[str, Any] = {
        "alg": "Ed25519",
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    if isinstance(signer, Ed25519FileKeySigner):
        signature_block["public_key_b64"] = base64.b64encode(signer.public_key_bytes()).decode("ascii")

    receipt: dict[str, Any] = dict(body)
    receipt["receipt_hash"] = receipt_hash
    receipt["signature"] = signature_block
    receipt["operator_hmac"] = operator_hmac
    receipt_bytes = _canonical_json_bytes(receipt) + b"\n"

    receipt_path: Path | None = None
    if write:
        if output_dir is None:
            raise DiagnosisReceiptError("output_dir is required to write a diagnosis receipt")
        output_dir.mkdir(parents=True, exist_ok=True)
        digest = receipt_hash.split(":", 1)[-1]
        receipt_path = output_dir / f"diagnosis-{result.run_id}-{digest[:16]}.json"
        receipt_path.write_bytes(receipt_bytes)

    return DiagnosisReceipt(receipt=receipt, receipt_bytes=receipt_bytes, receipt_path=receipt_path)


def _load_verifier(
    receipt: dict[str, Any],
    pinned: Ed25519PublicKeyVerifier | None,
) -> tuple[Ed25519PublicKeyVerifier | None, str]:
    """Return the verifier to use (pinned wins) or an error reason."""
    if pinned is not None:
        return pinned, ""
    raw_block = receipt.get("signature")
    if not isinstance(raw_block, dict):
        return None, "receipt carries no signature block"
    signature_block = cast("dict[str, Any]", raw_block)
    embedded = signature_block.get("public_key_b64")
    if not embedded:
        return None, "receipt embeds no public key and none was pinned; pass --public-key"
    try:
        return Ed25519PublicKeyVerifier.from_raw(base64.b64decode(str(embedded))), ""
    except (LineageSignerError, ValueError) as exc:
        return None, f"embedded public key is invalid: {exc}"


def verify_diagnosis_receipt(
    receipt_path: Path,
    *,
    journal_path: Path,
    verifier: Ed25519PublicKeyVerifier | None = None,
    audit_key: bytes | None = None,
) -> DiagnosisVerifyResult:
    """Re-derive a diagnosis receipt offline and assert byte-identity.

    Checks, in order: the receipt hash recomputes from the body; the Ed25519
    signature verifies (against the pinned key when given, else the embedded
    one); the operator HMAC verifies when *audit_key* is supplied; the
    journal bytes match ``journal_file_sha256`` exactly; and the diagnosis
    re-derived from the embedded ``signal`` block matches the receipt's
    culprit index, step hash, journal head, event count, reason code, and
    predicate hash byte-for-byte.

    Args:
        receipt_path: Path to the receipt JSON.
        journal_path: Path to the diagnosed run's ``journal.jsonl``.
        verifier: Optional pinned Ed25519 public key.
        audit_key: Optional operator audit HMAC key; when omitted the HMAC
            check is skipped and reported via ``hmac_checked=False``.

    Returns:
        A :class:`DiagnosisVerifyResult` with the first failing reason.
    """
    try:
        receipt_raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return DiagnosisVerifyResult(ok=False, reason=f"malformed diagnosis receipt: {exc}")
    if not isinstance(receipt_raw, dict):
        return DiagnosisVerifyResult(ok=False, reason="malformed diagnosis receipt: not a JSON object")
    receipt = cast("dict[str, Any]", receipt_raw)

    if receipt.get("receipt_type") != DIAGNOSIS_RECEIPT_TYPE:
        return DiagnosisVerifyResult(
            ok=False,
            reason=f"not a diagnosis receipt (receipt_type={receipt.get('receipt_type')!r})",
            receipt=receipt,
        )
    missing = [k for k in _BODY_FIELDS if k not in receipt]
    if missing:
        return DiagnosisVerifyResult(ok=False, reason=f"receipt is missing body field(s): {missing}", receipt=receipt)

    body = {k: receipt[k] for k in _BODY_FIELDS}
    stored_hash = str(receipt.get("receipt_hash", ""))
    if _receipt_hash_of(body) != stored_hash:
        return DiagnosisVerifyResult(
            ok=False,
            reason="receipt_hash does not recompute from the receipt body (tampered)",
            receipt=receipt,
        )

    payload = _signed_payload(body, stored_hash)
    resolved_verifier, verifier_error = _load_verifier(receipt, verifier)
    if resolved_verifier is None:
        return DiagnosisVerifyResult(ok=False, reason=verifier_error, receipt=receipt)
    sig_raw = receipt.get("signature")
    sig_block = cast("dict[str, Any]", sig_raw) if isinstance(sig_raw, dict) else {}
    signature_b64 = str(sig_block.get("signature_b64", ""))
    try:
        signature = base64.b64decode(str(signature_b64))
    except ValueError:
        return DiagnosisVerifyResult(ok=False, reason="signature is not valid base64", receipt=receipt)
    if not resolved_verifier.verify(payload, signature):
        return DiagnosisVerifyResult(
            ok=False, reason="Ed25519 signature does not verify over the receipt bytes", receipt=receipt
        )

    hmac_checked = False
    if audit_key is not None:
        expected_hmac = _hmac.new(audit_key, payload, hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected_hmac, str(receipt.get("operator_hmac", ""))):
            return DiagnosisVerifyResult(
                ok=False, reason="operator HMAC does not verify under the audit key", receipt=receipt
            )
        hmac_checked = True

    if not journal_path.exists():
        return DiagnosisVerifyResult(
            ok=False,
            reason=f"journal {journal_path} is absent; cannot re-derive the diagnosis",
            hmac_checked=hmac_checked,
            receipt=receipt,
        )
    actual_file_sha = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    if actual_file_sha != str(receipt["journal_file_sha256"]):
        return DiagnosisVerifyResult(
            ok=False,
            reason="journal bytes differ from the diagnosed journal (mutated or different run)",
            hmac_checked=hmac_checked,
            receipt=receipt,
        )

    try:
        predicate = predicate_from_params(receipt["signal"])
        rederived = diagnose_run(journal_path, predicate, run_id=str(receipt["run_id"]))
    except DiagnoseError as exc:
        return DiagnosisVerifyResult(
            ok=False, reason=f"re-derivation failed: {exc}", hmac_checked=hmac_checked, receipt=receipt
        )

    checks: list[tuple[str, Any, Any]] = [
        ("located", True, rederived.located),
        ("culprit_index", receipt["culprit_index"], rederived.culprit_index),
        ("culprit_step_hash", receipt["culprit_step_hash"], rederived.culprit_step_hash),
        ("journal_head", receipt["journal_head"], rederived.journal_head),
        ("event_count", receipt["event_count"], rederived.event_count),
        ("reason_code", receipt["reason_code"], rederived.reason_code),
        ("reason", receipt["reason"], rederived.reason),
        ("predicate_id", receipt["predicate_id"], rederived.predicate_id),
        ("predicate_hash", receipt["predicate_hash"], rederived.predicate_hash),
    ]
    for name, stored, derived in checks:
        if stored != derived:
            return DiagnosisVerifyResult(
                ok=False,
                reason=f"re-derived diagnosis does not match the receipt: {name} {derived!r} != {stored!r}",
                hmac_checked=hmac_checked,
                receipt=receipt,
            )

    return DiagnosisVerifyResult(ok=True, reason="", hmac_checked=hmac_checked, receipt=receipt)


__all__ = [
    "DIAGNOSIS_RECEIPT_SCHEMA_VERSION",
    "DIAGNOSIS_RECEIPT_TYPE",
    "DiagnosisReceipt",
    "DiagnosisReceiptError",
    "DiagnosisVerifyResult",
    "build_diagnosis_receipt",
    "verify_diagnosis_receipt",
]
