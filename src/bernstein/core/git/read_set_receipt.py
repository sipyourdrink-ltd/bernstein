"""Read-set refusal receipt for offline verification of admission rejections.

This module defines the signed, content-addressed refusal receipt emitted when
a run's read-set has changed since the base commit, preventing the merge.

The receipt is deterministic: the same admission refusal always produces
byte-identical canonical bytes. Offline verification checks the receipt against
the audit chain using only the commit hashes embedded in the changed paths,
without requiring repository access.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import AgentCard, sign_detached, verify_detached

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "READ_SET_REFUSAL_DOMAIN",
    "READ_SET_REFUSAL_KID",
    "READ_SET_REFUSAL_VERSION",
    "ChangedPath",
    "ReadSetRefusalReceipt",
    "anchor_refusal_receipt",
    "build_refusal_receipt",
    "read_refusal_receipt",
    "serialize_receipt",
    "verify_receipt_offline",
    "verify_refusal_receipt",
    "write_refusal_receipt",
]

#: Wire-format version stamped into every receipt preimage.
READ_SET_REFUSAL_VERSION = 1

#: Domain-separation tag prefixed to the signed bytes. A signature over
#: ``DOMAIN || canonical`` cannot be replayed as any other subsystem's JWS.
READ_SET_REFUSAL_DOMAIN = b"bernstein.read-set-refusal.v1\x00"

#: Key id carried in the detached JWS protected header; the matching
#: ``AgentCard.kid`` is required at verify time.
READ_SET_REFUSAL_KID = "read-set-refusal"


@dataclass(frozen=True, slots=True)
class ChangedPath:
    """Represents a file path that has changed between two commits.

    Attributes:
        path: The file path relative to the worktree root.
        old_commit: The commit hash of the file at the base commit (or null hash if
            the file did not exist at base commit).
        new_commit: The commit hash of the file at the target branch (or null hash
            if the file does not exist at target branch).
    """

    path: str
    old_commit: str
    new_commit: str


@dataclass(frozen=True, slots=True)
class ReadSetRefusalReceipt:
    """A signed, chain-anchorable refusal of a read-set admission check.

    The signature is detached (RFC 7515 / RFC 7797): the signed body is the
    canonical dict from :meth:`to_canonical_dict`, and the receipt carries the
    signer's public key so a verifier holding only the receipt can check it.

    Attributes:
        v: Wire-format version.
        task_id: The task whose read-set admission was refused.
        base_commit: The commit hash used as the baseline for comparison.
        target_branch: The target branch name or commit hash that was checked.
        changed_paths: List of paths in the read-set that have changed.
        timestamp: Integer Unix timestamp the refusal was minted at. Excluded
            from the canonical signed body so two operators refusing the same
            read-set derive the same ``receipt_hash``.
        signer_public_key_pem: The install's Ed25519 public key (PEM).
        signature: The detached JWS over ``DOMAIN || canonical_bytes``.
    """

    v: int
    task_id: str
    base_commit: str
    target_branch: str
    changed_paths: list[ChangedPath]
    timestamp: int = 0
    signer_public_key_pem: str = ""
    signature: str = ""

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the deterministic signed body (excludes signature + clock).

        ``timestamp`` and the signature are intentionally excluded so the
        canonical bytes -- and thus ``receipt_hash`` -- are a pure function of
        the refusal's content. Two operators refusing the same read-set against
        the same baseline derive the same receipt hash.
        """
        return {
            "v": self.v,
            "task_id": self.task_id,
            "base_commit": self.base_commit,
            "target_branch": self.target_branch,
            "changed_paths": [
                {
                    "path": p.path,
                    "old_commit": p.old_commit,
                    "new_commit": p.new_commit,
                }
                for p in self.changed_paths
            ],
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
        return READ_SET_REFUSAL_DOMAIN + self.canonical_bytes()

    def to_dict(self) -> dict[str, Any]:
        """Full on-disk record (signed body + signature + clock + hash)."""
        row = self.to_canonical_dict()
        row["timestamp"] = self.timestamp
        row["signer_public_key_pem"] = self.signer_public_key_pem
        row["signature"] = self.signature
        row["receipt_hash"] = self.receipt_hash()
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ReadSetRefusalReceipt:
        """Reconstruct a receipt from its on-disk representation."""
        changed_paths = [
            ChangedPath(
                path=p["path"],
                old_commit=p["old_commit"],
                new_commit=p["new_commit"],
            )
            for p in row.get("changed_paths", [])
        ]
        return cls(
            v=int(row.get("v", READ_SET_REFUSAL_VERSION)),
            task_id=str(row.get("task_id", "")),
            base_commit=str(row.get("base_commit", "")),
            target_branch=str(row.get("target_branch", "")),
            changed_paths=changed_paths,
            timestamp=int(row.get("timestamp", 0)),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
        )


def serialize_receipt(receipt: ReadSetRefusalReceipt) -> bytes:
    """Produce deterministic JSON bytes for a receipt.

    The output is canonical: identical inputs produce byte-identical outputs.
    This is required for offline verification so different operators can
    reconstruct the same canonical bytes and compare them.

    Args:
        receipt: The receipt to serialize.

    Returns:
        Canonical JSON bytes of the signed body (excludes signature and timestamp).
    """
    return receipt.canonical_bytes()


def build_refusal_receipt(
    *,
    task_id: str,
    base_commit: str,
    target_branch: str,
    changed_paths: list[ChangedPath],
    private_key_pem: str,
    public_key_pem: str,
    timestamp: int | None = None,
) -> ReadSetRefusalReceipt:
    """Compile and sign a :class:`ReadSetRefusalReceipt`.

    The signature is a detached Ed25519 JWS over the domain-separated preimage,
    so a mutated receipt body or a foreign-domain signature fails verification.

    Args:
        task_id: The task whose read-set admission was refused.
        base_commit: The commit hash used as the baseline.
        target_branch: The target branch that was checked.
        changed_paths: Paths in the read-set that have changed.
        private_key_pem: The install's Ed25519 private key in PEM format.
        public_key_pem: The install's Ed25519 public key in PEM format.
        timestamp: Optional Unix timestamp; defaults to current time.

    Returns:
        The sealed receipt with signature and public key attached.
    """
    unsigned = ReadSetRefusalReceipt(
        v=READ_SET_REFUSAL_VERSION,
        task_id=task_id,
        base_commit=base_commit,
        target_branch=target_branch,
        changed_paths=changed_paths,
        timestamp=int(timestamp if timestamp is not None else time.time()),
        signer_public_key_pem=public_key_pem,
    )
    signature = sign_detached(unsigned.signing_input(), private_key_pem, kid=READ_SET_REFUSAL_KID)
    return ReadSetRefusalReceipt(
        v=unsigned.v,
        task_id=unsigned.task_id,
        base_commit=unsigned.base_commit,
        target_branch=unsigned.target_branch,
        changed_paths=unsigned.changed_paths,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
    )


def verify_refusal_receipt(receipt: ReadSetRefusalReceipt) -> bool:
    """Verify the receipt's detached signature against its embedded public key.

    Returns False on any tamper: a mutated body changes the signing input, and a
    foreign-domain signature is over a different preimage. Never raises.

    Args:
        receipt: The receipt to verify.

    Returns:
        True if the signature is valid; False otherwise.
    """
    if not receipt.signature or not receipt.signer_public_key_pem:
        return False
    card = AgentCard(
        agent_id="install",
        kid=READ_SET_REFUSAL_KID,
        public_key_pem=receipt.signer_public_key_pem,
    )
    return verify_detached(receipt.signing_input(), receipt.signature, card)


# ---------------------------------------------------------------------------
# Chain anchoring
# ---------------------------------------------------------------------------


def anchor_refusal_receipt(chain: AuditChainStore, receipt: ReadSetRefusalReceipt) -> AuditEvent:
    """Anchor a receipt's identity into the HMAC audit chain.

    Records the receipt's content hash (plus diagnostic fields) so an
    ``read_set.refusal_receipt`` chain entry pins exactly this refusal.

    Args:
        chain: The audit chain store accepting the entry.
        receipt: The sealed refusal receipt to anchor.

    Returns:
        The recorded :class:`AuditEvent` with ``prev_chain_digest`` embedded.
    """
    from bernstein.core.security.audit_chain import record_read_set_refusal

    return record_read_set_refusal(
        chain=chain,
        task_id=receipt.task_id,
        base_commit=receipt.base_commit,
        target_branch=receipt.target_branch,
        receipt_hash=receipt.receipt_hash(),
    )


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyReceiptResult:
    """Outcome of :func:`verify_receipt_offline`."""

    ok: bool
    reason: str
    signature_ok: bool = False
    chain_ok: bool = False
    anchored: bool = False
    changed_paths_verified: int = 0


def verify_receipt_offline(receipt_bytes: bytes, chain_path: str) -> bool:
    """Verify a receipt against the audit chain without repository access.

    This is the offline counterpart to :func:`verify_refusal_receipt`. Given
    the canonical bytes of a refusal receipt and a path to an audit chain
    database, this function verifies:

    1. The receipt's detached signature verifies against its embedded key.
    2. The audit chain itself is valid (HMAC chain integrity).
    3. An ``read_set.refusal_receipt`` entry exists in the chain whose
       ``receipt_hash`` matches the receipt's recomputed content hash.

    The commit hashes embedded in ``changed_paths`` can be used to reconstruct
    the change evidence offline without needing the repository.

    Args:
        receipt_bytes: The canonical JSON bytes of the refusal receipt.
        chain_path: Path to the audit chain database file.

    Returns:
        True if all verification checks pass; False otherwise.

    Raises:
        ValueError: If the receipt bytes are malformed or the chain path is
            invalid.
    """
    # Step 1: Parse and reconstruct the receipt
    try:
        receipt_data = json.loads(receipt_bytes.decode("utf-8"))
        receipt = ReadSetRefusalReceipt.from_dict(receipt_data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed receipt bytes: {exc}") from exc

    # Step 2: Verify the signature
    signature_ok = verify_refusal_receipt(receipt)
    if not signature_ok:
        return False

    # Step 3: Load and verify the audit chain
    try:
        from bernstein.core.security.audit import AuditLog
        from bernstein.core.security.audit_chain import AuditChainStore

        audit_log = AuditLog.load_from_file(chain_path)
        chain = AuditChainStore(audit_log)
        chain_ok, _ = chain.verify()
        if not chain_ok:
            return False
    except Exception:
        return False

    # Step 4: Verify the receipt is anchored in the chain
    from bernstein.core.security.audit_chain import EVENT_READ_SET_REFUSAL

    recomputed = receipt.receipt_hash()
    matches = [
        e.details.get("receipt_hash", "")
        for e in chain.query(event_type=EVENT_READ_SET_REFUSAL)
        if str(e.details.get("receipt_hash", "")) == recomputed
    ]
    return bool(matches)


# ---------------------------------------------------------------------------
# On-disk persistence
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    """Validate a path component for safe use in file paths."""
    if not value or "/" in value or "\\" in value or "\x00" in value or value in {".", ".."}:
        raise ValueError(f"unsafe path component: {value!r}")
    return value


def refusal_dir(sdd_dir: Path) -> Path:
    """Return the directory refusal receipts are written under."""
    return sdd_dir / "read_set_admission" / "refusals"


def write_refusal_receipt(sdd_dir: Path, receipt: ReadSetRefusalReceipt) -> Path:
    """Persist a receipt as a content-addressed JSON record.

    The filename is the receipt's content hash (hex, no prefix) so re-writing
    an identical refusal is idempotent and the file name is itself the anchor.

    Args:
        sdd_dir: The ``.sdd`` directory root.
        receipt: The receipt to persist.

    Returns:
        The path to the written file.
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


def read_refusal_receipt(path: Path) -> ReadSetRefusalReceipt | None:
    """Load a receipt from disk; ``None`` on a missing / malformed file."""
    if not path.is_file():
        return None
    try:
        return ReadSetRefusalReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# One-call boundary helper
# ---------------------------------------------------------------------------


def refuse_read_set(
    *,
    chain: AuditChainStore,
    sdd_dir: Path | None,
    task_id: str,
    base_commit: str,
    target_branch: str,
    changed_paths: list[ChangedPath],
    private_key_pem: str,
    public_key_pem: str,
    timestamp: int | None = None,
) -> ReadSetRefusalReceipt:
    """Build, sign, anchor, and (optionally) persist a read-set refusal in one call.

    The single entry point every admission boundary uses so a read-set mismatch
    is never a silent skip. Returns the sealed receipt. The chain anchor is
    written before the on-disk record so the audit chain always carries the
    refusal even if the filesystem write fails.

    Args:
        chain: The audit chain store for anchoring.
        sdd_dir: The ``.sdd`` directory root for persistence, or ``None`` to
            skip filesystem writes.
        task_id: The task whose read-set admission was refused.
        base_commit: The commit hash used as the baseline.
        target_branch: The target branch that was checked.
        changed_paths: Paths in the read-set that have changed.
        private_key_pem: The install's Ed25519 private key in PEM format.
        public_key_pem: The install's Ed25519 public key in PEM format.
        timestamp: Optional Unix timestamp; defaults to current time.

    Returns:
        The sealed receipt.
    """
    receipt = build_refusal_receipt(
        task_id=task_id,
        base_commit=base_commit,
        target_branch=target_branch,
        changed_paths=changed_paths,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        timestamp=timestamp,
    )
    anchor_refusal_receipt(chain, receipt)
    if sdd_dir is not None:
        write_refusal_receipt(sdd_dir, receipt)
    return receipt
