"""Merge admission receipts (issue #3754).

Binds the decision to merge a PR to main into a signed, SHA-anchored,
spine-anchored artefact -- mirroring the pattern already shipped for
adapter admission (``adapters/admission.py``) and parallel-execution
admission (``core/parallel_admission.py``), and the review-receipt
spines in ``core/review/receipt.py``.

A merge admission receipt binds ``{head_sha, merge_base_sha,
required_context_ids, gate_results_hash, ruleset_hash,
review_receipt_id, journal_head, decision, authority}``, signs the
canonical binding with the install's Ed25519 identity, and anchors
the signature in the audit chain. After a merge lands there is a
single artefact that proves which gates it satisfied, at what chain
head, under whose authority -- something the four merge-gate layers
in ``docs/operations/merge-gate.md`` collectively prevent from being
wrong but none of which records on their own.

Separation of admission vs. advisory:
    The ``decision`` field is a pure function of the hashed inputs. An
    ``escalate`` advisory (recorded as a sibling annotation) can never
    turn a ``refuse`` into an ``admit``. The advisory carries its own
    hash and is never a term in the admission decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

logger = logging.getLogger(__name__)

#: Run id under which every merge-admission receipt is anchored.
MERGE_RUN_ID = "merges"

#: Actor recorded on spine entries for merge admissions.
_MERGE_ACTOR = "bernstein.merge_admission"

#: Model string recorded on spine entries (the decision is a pure function
#: of hashed inputs; no LLM participates in admission).
_MERGE_MODEL = "admission"

#: Version stamped into every receipt binding preimage. Bump only on a
#: wire-format change.
MERGE_SCHEMA_VERSION = 1

#: Admission decision: every gate satisfied, merge permitted.
DECISION_ADMIT = "admit"

#: Admission decision: at least one gate failed, merge refused.
DECISION_REFUSE = "refuse"

#: Advisory only: the decision is admitted but a reviewer escalated an
#: observation.  This is a sibling annotation, not a decision.
DECISION_ESCALATE = "escalate"


__all__ = [
    "DECISION_ADMIT",
    "DECISION_ESCALATE",
    "DECISION_REFUSE",
    "MERGE_RECEIPT_DIR",
    "MERGE_RUN_ID",
    "MERGE_SCHEMA_VERSION",
    "MergeAdmissionReceipt",
    "MergeVerifyResult",
    "_canonical_bytes",
    "_sha256_hex",
    "compute_gate_results_hash",
    "compute_ruleset_hash",
    "emit_merge_receipt",
    "load_or_create_merge_identity",
    "merge_receipt_path",
    "read_merge_receipt",
    "verify_merge_receipt",
]

#: Sub-path (relative to workdir) where merge receipts are stored on disk.
MERGE_RECEIPT_DIR = (".sdd", "merges", "receipts")

#: Sub-path (relative to workdir) where the merge-identity Ed25519 keys live.
_MERGE_IDENTITY_DIR = (".sdd", "identity")
_PRIVATE_KEY_NAME = "merge-identity-key.pem"
_PUBLIC_KEY_NAME = "merge-identity-public.pem"


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes: sorted keys, minimal separators, UTF-8."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Input hashes
# ---------------------------------------------------------------------------


def compute_gate_results_hash(
    *,
    blast_radius: dict[str, Any] | None = None,
    review_verdict: str = "",
    required_contexts: tuple[str, ...] = (),
) -> str:
    """Hash the gate-result inputs into a single digest.

    Folds the blast-radius report, the review gate verdict, and the set of
    required GitHub contexts into one content-addressed digest so that two
    operators replaying the same head against the same ruleset derive an
    identical ``gate_results_hash`` -- and therefore an identical admission
    decision.
    """
    payload: dict[str, Any] = {
        "blast_radius": blast_radius or {},
        "review_verdict": review_verdict,
        "required_contexts": sorted(required_contexts),
    }
    return _sha256_hex(_canonical_bytes(payload))


def compute_ruleset_hash(
    *,
    required_contexts: tuple[str, ...] = (),
    ruleset_bytes: bytes = b"",
) -> str:
    """Hash the merge ruleset into a stable digest.

    The ruleset is everything that determines the admission inputs: the set
    of required GitHub status contexts and the raw ruleset document (if any).
    """
    payload = {
        "required_contexts": sorted(required_contexts),
        "ruleset": _sha256_hex(ruleset_bytes) if ruleset_bytes else "",
    }
    return _sha256_hex(_canonical_bytes(payload))


# ---------------------------------------------------------------------------
# Identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def load_or_create_merge_identity(workdir: Path) -> tuple[str, str]:
    """Load or create the install's Ed25519 merge-admission identity.

    Keys are persisted under ``.sdd/identity/`` so the same install signs
    every merge receipt and a verifier can check signatures offline against
    the embedded public key.  The private key file is written with
    ``0600`` mode.

    Args:
        workdir: Project root.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    identity_dir = workdir.joinpath(*_MERGE_IDENTITY_DIR)
    private_path = identity_dir / _PRIVATE_KEY_NAME
    public_path = identity_dir / _PUBLIC_KEY_NAME
    if private_path.is_file() and public_path.is_file():
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(private_path.suffix + ".tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeAdmissionReceipt:
    """Signed, spine-anchored record of one merge admission decision.

    Attributes:
        head_sha: The exact commit SHA that merged to ``main``.
        merge_base_sha: The merge-base SHA the head was evaluated from.
        required_context_ids: Required GitHub status contexts the merge
            satisfied, sorted for a deterministic projection.
        gate_results_hash: Hash of ``(blast_radius, review_verdict,
            required_contexts)`` -- the gate outputs that drove the
            decision.
        ruleset_hash: Hash of the ruleset the decision ran under.
        review_receipt_id: The spine entry hash of the review receipt this
            merge was covered by, when one exists; empty otherwise.
        journal_head: The run journal Merkle head at decision time.
        decision: :data:`DECISION_ADMIT`, :data:`DECISION_REFUSE`, or
            :data:`DECISION_ESCALATE`.
        authority: ``autonomous`` or ``operator_review`` -- which mode was
            active, recorded rather than inferred.
        timestamp: Integer timestamp for the receipt.
        signer_public_key_pem: Install Ed25519 public key (embedded so
            verify is offline).
        signature: Ed25519 detached signature over the canonical binding.
        journal_entry_hash: The merge spine entry hash anchoring the
            receipt.
        advisory: Optional sibling annotation (escalation rationale).
            Never part of the admission decision.
    """

    head_sha: str
    merge_base_sha: str
    required_context_ids: tuple[str, ...]
    gate_results_hash: str
    ruleset_hash: str
    review_receipt_id: str
    journal_head: str
    decision: str
    authority: str
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    advisory: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the signed + anchored binding (no signature / anchor).

        The ``advisory`` field is deliberately excluded -- it is a sibling
        annotation and must never be a term in the admission decision.
        """
        return {
            "v": MERGE_SCHEMA_VERSION,
            "head_sha": self.head_sha,
            "merge_base_sha": self.merge_base_sha,
            "required_context_ids": list(self.required_context_ids),
            "gate_results_hash": self.gate_results_hash,
            "ruleset_hash": self.ruleset_hash,
            "review_receipt_id": self.review_receipt_id,
            "journal_head": self.journal_head,
            "decision": self.decision,
            "authority": self.authority,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
            "advisory": self.advisory,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> MergeAdmissionReceipt:
        return cls(
            head_sha=str(row["head_sha"]),
            merge_base_sha=str(row["merge_base_sha"]),
            required_context_ids=tuple(row.get("required_context_ids", [])),
            gate_results_hash=str(row["gate_results_hash"]),
            ruleset_hash=str(row["ruleset_hash"]),
            review_receipt_id=str(row.get("review_receipt_id", "")),
            journal_head=str(row["journal_head"]),
            decision=str(row["decision"]),
            authority=str(row["authority"]),
            timestamp=int(row.get("timestamp", 0)),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
            advisory=str(row.get("advisory", "")),
        )


# ---------------------------------------------------------------------------
# On-disk paths
# ---------------------------------------------------------------------------


def merge_receipt_path(workdir: Path, head_sha: str) -> Path:
    """Return the on-disk path for the merge receipt covering ``head_sha``.

    The SHA is content-hashed into the filename so the receipt lands at a
    stable, collision-safe path derived from the SHA it covers.
    """
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    return workdir.joinpath(*MERGE_RECEIPT_DIR, f"{safe}.json")


def read_merge_receipt(workdir: Path, head_sha: str) -> MergeAdmissionReceipt | None:
    """Return the merge receipt for ``head_sha`` or ``None`` if absent."""
    path = merge_receipt_path(workdir, head_sha)
    if not path.is_file():
        return None
    try:
        return MergeAdmissionReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("merge: malformed receipt at %s", path)
        return None


# ---------------------------------------------------------------------------
# Emit (AC1)
# ---------------------------------------------------------------------------


def emit_merge_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    head_sha: str,
    merge_base_sha: str,
    required_context_ids: tuple[str, ...] = (),
    blast_radius: dict[str, Any] | None = None,
    review_verdict: str = "",
    ruleset_bytes: bytes = b"",
    review_receipt_id: str = "",
    journal_head: str = "",
    decision: str = DECISION_ADMIT,
    authority: str = "autonomous",
    advisory: str = "",
    timestamp: int = 0,
) -> MergeAdmissionReceipt:
    """Emit a signed, spine-anchored merge admission receipt.

    The receipt's canonical binding bytes are signed with the install's
    Ed25519 identity and are exactly the bytes the merge spine hashes, so
    the returned receipt's ``signature`` and ``journal_entry_hash`` are its
    chain-verifiable identity.  Re-emitting against the same inputs yields
    a byte-identical binding (only the signature is stable across runs
    because it signs the same bytes with the same key).

    Args:
        workdir: Project root; receipt lands under ``.sdd/merges/receipts/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: Audit-chain HMAC key.
        private_key_pem: Install Ed25519 private key (PEM).
        public_key_pem: Matching public key, embedded on the receipt.
        head_sha: The exact commit SHA that merged to ``main``.
        merge_base_sha: Merge-base SHA the head was evaluated from.
        required_context_ids: Required contexts the merge satisfied.
        blast_radius: Blast-radius report dict from
            :func:`blast_radius.BlastRadiusReport.to_dict`.
        review_verdict: ``pass`` / ``fail`` / ``questions``.
        ruleset_bytes: Raw bytes of the configured merge ruleset, if any.
        review_receipt_id: Spine entry hash of the covered review receipt.
        journal_head: Run journal Merkle head at decision time.
        decision: :data:`DECISION_ADMIT`, :data:`DECISION_REFUSE`, or
            :data:`DECISION_ESCALATE`.
        authority: ``autonomous`` or ``operator_review``.
        advisory: Optional escalation rationale (sibling annotation;
            never a term in the admission decision).
        timestamp: Integer timestamp; caller-chosen but stable.

    Returns:
        The signed, anchored :class:`MergeAdmissionReceipt`.
    """
    gate_results_hash = compute_gate_results_hash(
        blast_radius=blast_radius,
        review_verdict=review_verdict,
        required_contexts=required_context_ids,
    )
    ruleset_hash = compute_ruleset_hash(
        required_contexts=required_context_ids,
        ruleset_bytes=ruleset_bytes,
    )

    unsigned = MergeAdmissionReceipt(
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        required_context_ids=tuple(sorted(required_context_ids)),
        gate_results_hash=gate_results_hash,
        ruleset_hash=ruleset_hash,
        review_receipt_id=review_receipt_id,
        journal_head=journal_head,
        decision=decision,
        authority=authority,
        timestamp=timestamp,
        advisory=advisory,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    spine = LineageSpine(lineage_root, run_id=MERGE_RUN_ID, hmac_key=hmac_key)
    path = merge_receipt_path(workdir, head_sha)
    artifact_path = "/".join((*MERGE_RECEIPT_DIR, path.name))
    step_id = head_sha
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_MERGE_ACTOR,
        step_id=step_id,
        model=_MERGE_MODEL,
        timestamp=timestamp,
    )
    anchored = MergeAdmissionReceipt(
        head_sha=unsigned.head_sha,
        merge_base_sha=unsigned.merge_base_sha,
        required_context_ids=unsigned.required_context_ids,
        gate_results_hash=unsigned.gate_results_hash,
        ruleset_hash=unsigned.ruleset_hash,
        review_receipt_id=unsigned.review_receipt_id,
        journal_head=unsigned.journal_head,
        decision=unsigned.decision,
        authority=unsigned.authority,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
        advisory=unsigned.advisory,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


# ---------------------------------------------------------------------------
# Verify (mirrors review_receipt verify)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeVerifyResult:
    """Outcome of :func:`verify_merge_receipt`.

    Exit-code contract mirrors ``bernstein review-receipt verify``:
        0 = verified
        1 = no receipt / bad input
        2 = mismatch (tamper)
    """

    ok: bool
    decision: str
    authority: str
    head_sha: str
    reason: str = ""
    receipt: MergeAdmissionReceipt | None = None


def verify_merge_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    head_sha: str,
) -> MergeVerifyResult:
    """Prove offline that the merge receipt for ``head_sha`` holds.

    Recomputes the spine anchor over the receipt's canonical binding bytes,
    checks the Ed25519 signature against the receipt's embedded public key,
    and verifies the merge spine.  A single-byte edit to the receipt body,
    the signature, or the spine fails the check.

    Exit-code contract:
        0 = verified  (ok=True)
        1 = no receipt / bad input
        2 = mismatch (tamper)
    """
    receipt = read_merge_receipt(workdir, head_sha)
    if receipt is None:
        return MergeVerifyResult(
            ok=False,
            decision="",
            authority="",
            head_sha=head_sha,
            reason="no merge receipt found",
        )

    if not receipt.signature or not receipt.signer_public_key_pem:
        return MergeVerifyResult(
            ok=False,
            decision=receipt.decision,
            authority=receipt.authority,
            head_sha=receipt.head_sha,
            reason="receipt is unsigned",
        )

    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return MergeVerifyResult(
            ok=False,
            decision=receipt.decision,
            authority=receipt.authority,
            head_sha=receipt.head_sha,
            reason=f"signature does not verify ({outcome.reason})",
            receipt=receipt,
        )

    spine = LineageSpine(lineage_root, run_id=MERGE_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return MergeVerifyResult(
            ok=False,
            decision=receipt.decision,
            authority=receipt.authority,
            head_sha=receipt.head_sha,
            reason=f"merge spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )

    # Recompute the spine anchor over the receipt's canonical binding bytes
    # and compare to the recorded journal_entry_hash.
    want = content_hash_of(receipt.to_canonical_bytes())
    recomputed = None
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            recomputed = entry.entry_hash
            break

    if recomputed is None:
        return MergeVerifyResult(
            ok=False,
            decision=receipt.decision,
            authority=receipt.authority,
            head_sha=receipt.head_sha,
            reason="receipt is not anchored in the merge spine",
            receipt=receipt,
        )
    if recomputed != receipt.journal_entry_hash:
        return MergeVerifyResult(
            ok=False,
            decision=receipt.decision,
            authority=receipt.authority,
            head_sha=receipt.head_sha,
            reason="recorded journal_entry_hash does not match the spine anchor over the receipt bytes",
            receipt=receipt,
        )

    return MergeVerifyResult(
        ok=True,
        decision=receipt.decision,
        authority=receipt.authority,
        head_sha=receipt.head_sha,
        reason="",
        receipt=receipt,
    )
