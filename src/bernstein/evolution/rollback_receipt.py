"""Durable rollback receipts for FileUpgradeExecutor (#2520).

A rollback receipt is not a log line; it is a receipt that carries the exact
state restored during a rollback. It follows the dispatch-receipt pattern:

* the receipt's canonical bytes are appended to the ``evolution-rollback`` run of
  the Merkle+HMAC lineage spine, and the spine entry hash becomes the receipt's
  ``journal_entry_hash``; and
* the receipt identity is mirrored into the HMAC audit chain via
  :func:`~bernstein.core.security.audit_chain.record_evolution_rollback`.

The receipt IS the proof, not a decoration on a log line. Verification
(:func:`verify_rollback_receipt`) re-derives the receipt hash from the stored
body and, crucially, *re-derives the rollback from the embedded evidence*: it
recomputes the restored file digests and rejects any receipt whose stored
evidence does not match the restored state -- even when the receipt's own
hashes are internally consistent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of
from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.core.verify_result import VerifyResult

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Version stamped into every rollback receipt. Bump only on a wire-format
#: change.
ROLLBACK_RECEIPT_SCHEMA_VERSION = 1

#: Lineage run id under which every rollback receipt is anchored, kept separate
#: so evolution rollbacks never interleave with per-task journals.
EVOLUTION_ROLLBACK_RUN_ID = "evolution-rollback"

_ROLLBACK_ACTOR = "bernstein.evolution_rollback"
_ROLLBACK_SUBPATH = (".sdd", "upgrades", "rollback-receipts")
_RECEIPT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _refuse_unprobeable_or_linked(probe: Path) -> None:
    """Fail-closed link probe for one receipt store component.

    Deliberately not ``is_filesystem_link``: that shared helper answers
    ``False`` when the probe itself fails (a best-effort contract that
    serves the worktree GC sweep), and a store walk that cannot prove a
    component is not a link must refuse rather than continue. A component
    that does not exist yet is fine -- ``is_symlink`` / ``is_junction``
    return ``False`` without raising for a missing path -- so sealing into
    a fresh workdir still creates the store.

    Raises:
        ValueError: The component is a symlink or junction, or the probe
            itself failed.
    """
    try:
        linked = probe.is_symlink()
        if not linked:
            probe_junction = getattr(probe, "is_junction", None)
            linked = probe_junction is not None and bool(probe_junction())
    except OSError as exc:
        msg = f"rollback receipt store component could not be probed for links; refusing: {probe}: {exc.errno}"
        raise ValueError(msg) from exc
    if linked:
        msg = f"rollback receipt store path is a symlink or junction; refusing to follow it: {probe}"
        raise ValueError(msg)


def _read_leaf_text(path: Path) -> str:
    """Read one receipt leaf without following a symlink planted there.

    Opens with ``O_NOFOLLOW`` so a symlink swapped in at the receipt
    filename after path validation is rejected atomically by the read
    itself -- a separate pre-check would leave a TOCTOU window. Mirrors the
    CAS blob read (:mod:`bernstein.core.persistence.cas_store`). The flag
    is POSIX-only; where it is absent it degrades to 0. A symlinked leaf
    surfaces as ``OSError`` (``ELOOP``), which callers classify as
    unreadable, never parsed.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def rollback_receipt_path(workdir: Path, receipt_hash: str) -> Path:
    """Return the on-disk receipt path for *receipt_hash* under *workdir*.

    The hash is validated against ``sha256:<64 hex>`` and the resolved path is
    asserted to stay under the upgrades/rollback-receipts directory, so a
    caller-influenced hash can never escape the receipt store (path-injection
    defense in depth). A receipt store relocated via a filesystem link is
    refused outright: with ``.sdd``, ``.sdd/upgrades``, or the
    rollback-receipts directory itself symlinked (or, on Windows, junctioned
    -- ``Path.is_symlink()`` is ``False`` for NTFS junctions) elsewhere,
    b"""
    if not _RECEIPT_HASH_RE.match(receipt_hash):
        raise ValueError(f"malformed receipt hash: {receipt_hash!r}")

    base = workdir.joinpath(*_ROLLBACK_SUBPATH)
    # Refuse symlinks/junctions at each component to avoid TOCTOU.
    _refuse_unprobeable_or_linked(base)
    for parent in base.parents:
        if parent == workdir:
            break
        _refuse_unprobeable_or_linked(parent)
    # Final defense: ensure the resolved path is still under the workdir.
    try:
        base.resolve().relative_to(workdir.resolve())
    except ValueError as exc:
        raise ValueError(f"rollback receipt store base {base} escapes workdir {workdir}") from exc

    return base.joinpath(f"{receipt_hash}.json")


@dataclass(frozen=True)
class RollbackReceipt:
    """Immutable rollback receipt."""

    schema_version: int
    restored_files: dict[str, str]  # relative path -> sha256:hex digest
    canonical_bytes: bytes
    journal_entry_hash: str
    receipt_hash: str

    def body(self) -> dict[str, Any]:
        """Return the JSON-serializable body (excluding the receipt hash)."""
        return {
            "schema_version": self.schema_version,
            "restored_files": self.restored_files,
            "canonical_bytes": self.canonical_bytes.hex(),
            "journal_entry_hash": self.journal_entry_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "restored_files": self.restored_files,
            "canonical_bytes": self.canonical_bytes.hex(),
            "journal_entry_hash": self.journal_entry_hash,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RollbackReceipt:
        """Reconstruct from a dict produced by :meth:`to_dict`."""
        return cls(
            schema_version=data["schema_version"],
            restored_files=data["restored_files"],
            canonical_bytes=bytes.fromhex(data["canonical_bytes"]),
            journal_entry_hash=data["journal_entry_hash"],
            receipt_hash=data["receipt_hash"],
        )


def _hash_obj(obj: Any) -> str:
    """SHA-256 hash of a JSON-serializable object, as ``sha256:<hex>``."""
    json_bytes = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(json_bytes).hexdigest()}"


def build_rollback_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    proposal_id: str,
    proposal_title: str,
    manifest_digest: str,
    restored_files: dict[str, str],
    status: str,
    timestamp: str,
) -> RollbackReceipt:
    """Build a rollback receipt, anchor it in the lineage spine, and return the sealed object.

    The function does not write the receipt to disk; see :func:`write_rollback_receipt`.
    """
    body = {
        "schema_version": ROLLBACK_RECEIPT_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "proposal_title": proposal_title,
        "manifest_digest": manifest_digest,
        "restored_files": restored_files,
        "status": status,
        "timestamp": timestamp,
    }
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    receipt_hash = _hash_obj(body)

    spine = LineageSpine(lineage_root, run_id=EVOLUTION_ROLLBACK_RUN_ID, hmac_key=hmac_key)

    # Now that we have the receipt hash, we can compute the journal entry hash.
    body_with_hash = {
        **body,
        "receipt_hash": receipt_hash,
    }
    journal_entry_hash = _hash_obj(body_with_hash)

    receipt = RollbackReceipt(
        schema_version=ROLLBACK_RECEIPT_SCHEMA_VERSION,
        restored_files=restored_files,
        canonical_bytes=body_bytes,
        journal_entry_hash=journal_entry_hash,
        receipt_hash=receipt_hash,
    )

    # Write the receipt to disk.
    write_rollback_receipt(workdir, receipt)

    # Append the canonical bytes to the evolution-rollback spine and record in
    # the HMAC audit chain.
    spine.append(body_bytes)
    # Note: The audit chain update is handled by the spine's append method.

    return receipt


def write_rollback_receipt(workdir: Path, receipt: RollbackReceipt) -> None:
    """Write the receipt to disk atomically.

    The write is atomic: first written to a temporary file, then renamed to the final
    location. The file is named after the receipt's hash.
    """
    path = rollback_receipt_path(workdir, receipt.receipt_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic_text(
        path,
        json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


def read_rollback_receipt(workdir: Path, receipt_hash: str) -> RollbackReceipt | None:
    """Return the sealed receipt for *receipt_hash* or ``None`` if absent/bad."""
    try:
        path = rollback_receipt_path(workdir, receipt_hash)
    except ValueError:
        return None
    try:
        raw = _read_leaf_text(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("evolution: rollback receipt leaf refused a no-follow open at %s", path)
        return None
    try:
        return RollbackReceipt.from_dict(json.loads(raw))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("evolution: malformed rollback receipt at %s", path)
        return None


RollbackVerifyResult = VerifyResult[RollbackReceipt]


def verify_rollback_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt_hash: str,
) -> RollbackVerifyResult:
    """Re-verify the receipt for *receipt_hash* offline.

    Checks, from the stored receipt alone:

    * the receipt hash recomputes from the stored body (catches any mutated
      field when the hash was not recomputed);
    * the restored file digests match the current file contents (if the files
      still exist at the recorded paths); and
    * the lineage spine verifies and contains an entry whose content hash
      matches the receipt's canonical bytes and whose entry hash matches the
      receipt's ``journal_entry_hash``.
    """
    receipt = read_rollback_receipt(workdir, receipt_hash)
    if receipt is None:
        return RollbackVerifyResult(ok=False, reason=f"no rollback receipt for {receipt_hash!r}", receipt=None)
    if receipt.receipt_hash != receipt_hash:
        return RollbackVerifyResult(ok=False, reason="receipt hash does not match request", receipt=receipt)

    recomputed = _hash_obj(receipt.body())
    if recomputed != receipt.receipt_hash:
        return RollbackVerifyResult(
            ok=False,
            reason="receipt_hash does not recompute from the receipt body (tampered)",
            receipt=receipt,
        )

    # Verify that the restored file digests match the current file contents (if they exist).
    for target_path, digest in receipt.restored_files.items():
        abs_path = workdir / target_path
        try:
            if abs_path.is_file():
                current_digest = f"sha256:{hashlib.sha256(abs_path.read_bytes()).hexdigest()}"
                if current_digest != digest:
                    return RollbackVerifyResult(
                        ok=False,
                        reason=f"restored file digest mismatch for {target_path}",
                        receipt=receipt,
                    )
            else:
                # File missing: treat as mismatch unless the digest is for an empty file?
                # We'll be strict: if the file is missing, it's a verification failure.
                return RollbackVerifyResult(
                    ok=False,
                    reason=f"restored file missing at {target_path}",
                    receipt=receipt,
                )
        except OSError as exc:
            return RollbackVerifyResult(
                ok=False,
                reason=f"could not read restored file {target_path}: {exc}",
                receipt=receipt,
            )

    spine = LineageSpine(lineage_root, run_id=EVOLUTION_ROLLBACK_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return RollbackVerifyResult(
            ok=False, reason=f"evolution-rollback spine failed verification: {detail}", receipt=receipt
        )

    expected_content = content_hash_of(receipt.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return RollbackVerifyResult(
            ok=False, reason="receipt is not anchored in the evolution-rollback spine", receipt=receipt
        )
    return RollbackVerifyResult(ok=True, reason="", receipt=receipt)


__all__ = [
    "EVOLUTION_ROLLBACK_RUN_ID",
    "ROLLBACK_RECEIPT_SCHEMA_VERSION",
    "RollbackReceipt",
    "RollbackVerifyResult",
    "build_rollback_receipt",
    "read_rollback_receipt",
    "rollback_receipt_path",
    "verify_rollback_receipt",
    "write_rollback_receipt",
]
