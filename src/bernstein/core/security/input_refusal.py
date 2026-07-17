"""Signed input-refusal receipts for the input side of the run boundary.

Issue #2545. Worker terminal payloads are contract enforced with a signed
completion shape; MCP tool calls pass a deny-by-default firewall. The *input*
side had no equivalent: when recipe param validation failed, the outcome was a
bare ``exit 1`` -- nothing landed in the audit chain, so a malformed fire was
invisible to fleet review and schedule audit.

This module is the input-side twin of the worker completion contract. A
validation failure at any input boundary (schedule fire, recipe launch, MCP
``bernstein_run`` / ``bernstein_scenario`` call, task-server claim) produces a
signed :class:`InputRefusalReceipt` instead of a stack trace or bare exit: the
JSONPath of the offending field (same diagnosability convention as
``ContractViolation``), the declared schema hash, and a digest of the rejected
value (raw bytes never stored), Ed25519-signed with the install identity and
anchored in the HMAC audit chain.

Killer shape: the refusal *is* the receipt. Strip the audit chain and the
install identity and a refusal degrades to a logged validation error; with them
a refusal is a tamper-evident, offline-verifiable proof artefact. Tampering with
the receipt bytes changes its content hash so no chain entry matches it;
tampering with the chain entry breaks the HMAC chain. Rejection completes before
any adapter process or model invocation, so a refused fire costs zero spawns and
zero tokens (extends the spawn-time refusal-receipt shape from #2515 to input
validation).

Domain separation: the receipt is signed over ``DOMAIN || canonical_bytes`` so a
signature minted for a different subsystem's JWS can never verify here.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import AgentCard, sign_detached, verify_detached

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "BOUNDARY_MCP_CALL",
    "BOUNDARY_RECIPE_LAUNCH",
    "BOUNDARY_SCHEDULE_FIRE",
    "BOUNDARY_TASK_CLAIM",
    "INPUT_REFUSAL_DOMAIN",
    "INPUT_REFUSAL_KID",
    "INPUT_REFUSAL_VERSION",
    "InputRefusalReceipt",
    "anchor_refusal_receipt",
    "read_refusal_receipt",
    "refuse_input",
    "verify_refusal_against_chain",
    "verify_refusal_receipt",
    "write_refusal_receipt",
]

#: Wire-format version stamped into every receipt preimage.
INPUT_REFUSAL_VERSION = 1

#: Domain-separation tag prefixed to the signed bytes. A signature over
#: ``DOMAIN || canonical`` cannot be replayed as any other subsystem's JWS.
INPUT_REFUSAL_DOMAIN = b"bernstein.input-refusal.v1\x00"

#: Key id carried in the detached JWS protected header; the matching
#: ``AgentCard.kid`` is required at verify time.
INPUT_REFUSAL_KID = "input-refusal"

#: The input boundaries that can emit a refusal receipt.
BOUNDARY_SCHEDULE_FIRE = "schedule.fire"
BOUNDARY_RECIPE_LAUNCH = "recipe.launch"
BOUNDARY_MCP_CALL = "mcp.call"
BOUNDARY_TASK_CLAIM = "task.claim"


@dataclass(frozen=True, slots=True)
class InputRefusalReceipt:
    """A signed, chain-anchorable refusal of a malformed input.

    The signature is detached (RFC 7515 / RFC 7797): the signed body is the
    canonical dict from :meth:`to_canonical_dict`, and the receipt carries the
    signer's public key so a verifier holding only the receipt can check it.

    Attributes:
        v: Wire-format version.
        boundary: The input boundary that refused (one of the ``BOUNDARY_*``).
        resource_id: The refused resource (schedule id, recipe name, task id).
        json_path: JSONPath of the offending field (``$.params.<name>``).
        schema_hash: ``sha256:`` hash of the declared parameter schema.
        value_digest: ``sha256:`` digest of the rejected value (raw bytes never
            stored); empty when the violation is a missing field.
        reason_code: Machine-stable reason (``bad_type`` / ``missing_required``
            / ``unknown_param`` / ``bad_choice``).
        message: One-line human-readable diagnostic.
        refused_at: Integer Unix timestamp the refusal was minted at. Excluded
            from the canonical signed body so two operators refusing the same
            input derive the same ``receipt_hash``.
        signer_public_key_pem: The install's Ed25519 public key (PEM).
        signature: The detached JWS over ``DOMAIN || canonical_bytes``.
    """

    v: int
    boundary: str
    resource_id: str
    json_path: str
    schema_hash: str
    value_digest: str
    reason_code: str
    message: str
    refused_at: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the deterministic signed body (excludes signature + clock).

        ``refused_at`` and the signature are intentionally excluded so the
        canonical bytes -- and thus ``receipt_hash`` -- are a pure function of
        the refusal's content. Two operators refusing the byte-identical input
        against the byte-identical schema derive the same receipt hash.
        """
        return {
            "v": self.v,
            "boundary": self.boundary,
            "resource_id": self.resource_id,
            "json_path": self.json_path,
            "schema_hash": self.schema_hash,
            "value_digest": self.value_digest,
            "reason_code": self.reason_code,
            "message": self.message,
        }

    def canonical_bytes(self) -> bytes:
        """RFC 8785-style canonical bytes of the signed body."""
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def receipt_hash(self) -> str:
        """``sha256:`` content hash of the canonical body (the chain anchor)."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def signing_input(self) -> bytes:
        """Domain-separated preimage that the detached JWS is computed over."""
        return INPUT_REFUSAL_DOMAIN + self.canonical_bytes()

    def to_dict(self) -> dict[str, Any]:
        """Full on-disk record (signed body + signature + clock + hash)."""
        row = self.to_canonical_dict()
        row["refused_at"] = self.refused_at
        row["signer_public_key_pem"] = self.signer_public_key_pem
        row["signature"] = self.signature
        row["receipt_hash"] = self.receipt_hash()
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> InputRefusalReceipt:
        return cls(
            v=int(row.get("v", INPUT_REFUSAL_VERSION)),
            boundary=str(row["boundary"]),
            resource_id=str(row.get("resource_id", "")),
            json_path=str(row.get("json_path", "")),
            schema_hash=str(row.get("schema_hash", "")),
            value_digest=str(row.get("value_digest", "")),
            reason_code=str(row.get("reason_code", "invalid")),
            message=str(row.get("message", "")),
            refused_at=int(row.get("refused_at", 0)),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
        )


def build_refusal_receipt(
    *,
    boundary: str,
    resource_id: str,
    json_path: str,
    schema_hash: str,
    value_digest: str,
    reason_code: str,
    message: str,
    private_key_pem: str,
    public_key_pem: str,
    refused_at: int | None = None,
) -> InputRefusalReceipt:
    """Compile and sign an :class:`InputRefusalReceipt` (never coerces input).

    The signature is a detached Ed25519 JWS over the domain-separated preimage,
    so a mutated receipt body or a foreign-domain signature fails verification.
    """
    unsigned = InputRefusalReceipt(
        v=INPUT_REFUSAL_VERSION,
        boundary=boundary,
        resource_id=resource_id,
        json_path=json_path,
        schema_hash=schema_hash,
        value_digest=value_digest,
        reason_code=reason_code,
        message=message,
        refused_at=int(refused_at if refused_at is not None else time.time()),
        signer_public_key_pem=public_key_pem,
    )
    signature = sign_detached(unsigned.signing_input(), private_key_pem, kid=INPUT_REFUSAL_KID)
    return InputRefusalReceipt(
        v=unsigned.v,
        boundary=unsigned.boundary,
        resource_id=unsigned.resource_id,
        json_path=unsigned.json_path,
        schema_hash=unsigned.schema_hash,
        value_digest=unsigned.value_digest,
        reason_code=unsigned.reason_code,
        message=unsigned.message,
        refused_at=unsigned.refused_at,
        signer_public_key_pem=public_key_pem,
        signature=signature,
    )


def verify_refusal_receipt(receipt: InputRefusalReceipt) -> bool:
    """Verify the receipt's detached signature against its embedded public key.

    Returns False on any tamper: a mutated body changes the signing input, and a
    foreign-domain signature is over a different preimage. Never raises.
    """
    if not receipt.signature or not receipt.signer_public_key_pem:
        return False
    card = AgentCard(agent_id="install", kid=INPUT_REFUSAL_KID, public_key_pem=receipt.signer_public_key_pem)
    return verify_detached(receipt.signing_input(), receipt.signature, card)


# ---------------------------------------------------------------------------
# Chain anchoring + offline verification
# ---------------------------------------------------------------------------


def anchor_refusal_receipt(chain: AuditChainStore, receipt: InputRefusalReceipt) -> AuditEvent:
    """Anchor a receipt's identity into the HMAC audit chain.

    Records the receipt's content hash (plus its diagnostic fields) so an
    ``input.refusal_receipt`` chain entry pins exactly this receipt.
    """
    from bernstein.core.security.audit_chain import record_input_refusal

    return record_input_refusal(
        chain=chain,
        boundary=receipt.boundary,
        json_path=receipt.json_path,
        schema_hash=receipt.schema_hash,
        value_digest=receipt.value_digest,
        receipt_hash=receipt.receipt_hash(),
        resource_id=receipt.resource_id,
        reason_code=receipt.reason_code,
    )


@dataclass(frozen=True)
class RefusalVerifyResult:
    """Outcome of :func:`verify_refusal_against_chain`."""

    ok: bool
    reason: str
    signature_ok: bool = False
    chain_ok: bool = False
    anchored: bool = False
    matched_events: tuple[str, ...] = field(default_factory=tuple)


def verify_refusal_against_chain(chain: AuditChainStore, receipt: InputRefusalReceipt) -> RefusalVerifyResult:
    """Verify a refusal receipt offline against the audit chain.

    Checks, in order:

    * the receipt's detached signature verifies against its embedded key;
    * the HMAC audit chain itself verifies (a tampered chain entry fails here);
    * an ``input.refusal_receipt`` entry exists whose ``receipt_hash`` matches
      the receipt's recomputed content hash (a tampered receipt body fails here,
      because its recomputed hash no longer matches any anchored entry).

    ``ok`` is True only when all three hold.
    """
    from bernstein.core.security.audit_chain import EVENT_INPUT_REFUSAL

    signature_ok = verify_refusal_receipt(receipt)
    if not signature_ok:
        return RefusalVerifyResult(ok=False, reason="receipt signature does not verify (tampered or wrong key)")

    chain_ok, chain_errors = chain.verify()
    if not chain_ok:
        detail = chain_errors[0] if chain_errors else "chain break"
        return RefusalVerifyResult(
            ok=False,
            reason=f"audit chain fails verification ({detail})",
            signature_ok=True,
        )

    recomputed = receipt.receipt_hash()
    matches = [
        e.details.get("receipt_hash", "")
        for e in chain.query(event_type=EVENT_INPUT_REFUSAL)
        if str(e.details.get("receipt_hash", "")) == recomputed
    ]
    if not matches:
        return RefusalVerifyResult(
            ok=False,
            reason="receipt is not anchored in the audit chain (tampered bytes or never recorded)",
            signature_ok=True,
            chain_ok=True,
        )
    return RefusalVerifyResult(
        ok=True,
        reason="",
        signature_ok=True,
        chain_ok=True,
        anchored=True,
        matched_events=tuple(str(m) for m in matches),
    )


# ---------------------------------------------------------------------------
# On-disk persistence
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    if not value or "/" in value or "\\" in value or "\x00" in value or value in {".", ".."}:
        raise ValueError(f"unsafe path component: {value!r}")
    return value


def refusal_dir(sdd_dir: Path) -> Path:
    """Return the directory refusal receipts are written under."""
    return sdd_dir / "input_contracts" / "refusals"


def write_refusal_receipt(sdd_dir: Path, receipt: InputRefusalReceipt) -> Path:
    """Persist a receipt as a content-addressed JSON record.

    The filename is the receipt's content hash (hex, no prefix) so re-writing an
    identical refusal is idempotent and the file name is itself the anchor.
    """
    directory = refusal_dir(sdd_dir)
    directory.mkdir(parents=True, exist_ok=True)
    digest = receipt.receipt_hash().split(":", 1)[-1]
    path = directory / f"{_safe_component(digest)}.json"
    path.write_text(
        json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_refusal_receipt(path: Path) -> InputRefusalReceipt | None:
    """Load a receipt from disk; ``None`` on a missing / malformed file."""
    if not path.is_file():
        return None
    try:
        return InputRefusalReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# One-call boundary helper
# ---------------------------------------------------------------------------


def refuse_input(
    *,
    chain: AuditChainStore,
    sdd_dir: Path | None,
    boundary: str,
    resource_id: str,
    json_path: str,
    schema_hash: str,
    value_digest: str,
    reason_code: str,
    message: str,
    private_key_pem: str,
    public_key_pem: str,
    refused_at: int | None = None,
) -> InputRefusalReceipt:
    """Build, sign, anchor, and (optionally) persist a refusal in one call.

    The single entry point every input boundary uses so a malformed input is
    never a silent skip. Returns the sealed receipt. The chain anchor is written
    before the on-disk record so the audit chain always carries the refusal even
    if the filesystem write fails.
    """
    receipt = build_refusal_receipt(
        boundary=boundary,
        resource_id=resource_id,
        json_path=json_path,
        schema_hash=schema_hash,
        value_digest=value_digest,
        reason_code=reason_code,
        message=message,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        refused_at=refused_at,
    )
    anchor_refusal_receipt(chain, receipt)
    if sdd_dir is not None:
        write_refusal_receipt(sdd_dir, receipt)
    return receipt
